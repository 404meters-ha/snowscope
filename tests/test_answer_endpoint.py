"""Story 1.3 应答端点与状态机收尾直跑断言（纯断言脚本，无 pytest 依赖、不跑真 LLM）。

用法：项目根执行 .venv/Scripts/python.exe tests/test_answer_endpoint.py
覆盖 I/O 矩阵：answer/cancel 全链（含工具线程收结果）／404／409×4（非 waiting、
无桥、id 不匹配、重复投递）／400 形状／终态三路清场／孤儿清扫（running+waiting）。
端点直调 api.answer_task（async）+ asyncio.run，HTTPException 捕获断言 code——不起服务。
"""
import asyncio
import contextlib
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

import main as main_app  # noqa: E402  孤儿清扫直调真函数（import 仅模块级建目录/挂静态，无 lifespan；别名避开本文件 main()）
from app import api  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.models import TaskRun  # noqa: E402
from app.services import askuser  # noqa: E402
from app.services.askuser import AskUserQuestionTool  # noqa: E402
from app.tasks import _append_log, _clear_pending_question, _sync_update, manager  # noqa: E402

PASS, FAIL, SKIP = [], [], []

# 路由级契约用：不带 context manager 即不触发 lifespan（不跑孤儿清扫，不打扰真实库）
CLIENT = TestClient(main_app.app)

CANCEL_TEXT = "User cancelled the question."
ANSWERED_TEXT = "User answered:\nQ1 先做哪个方向？ => A. 后端接口\nQ2 要不要写测试？ => 要"

QUESTIONS = [
    {
        "question": "Q1 先做哪个方向？",
        "options": [
            {"label": "A. 后端接口", "description": "先搭 API"},
            {"label": "B. 前端页面", "description": "先出界面"},
        ],
        "multiSelect": False,
    },
    {
        "question": "Q2 要不要写测试？",
        "options": [
            {"label": "要", "description": "补单测"},
            {"label": "不要", "description": "先跑通再说"},
        ],
        "multiSelect": False,
    },
]
ANSWERS = [
    {"question": "Q1 先做哪个方向？", "answer": "A. 后端接口"},
    {"question": "Q2 要不要写测试？", "answer": "要"},
]
# ✅ 留痕逐字（Design Notes：{q} => {a} 逐条 join 后截 120，join 口径同 ❓ preview）
ANSWER_SUMMARY = "；".join(f"{e['question']} => {e['answer']}" for e in ANSWERS)

PROBE_TIDS: list[str] = []  # manager.submit 产生的 uuid 行，末尾统一清理


def check(name: str, ok: bool):
    (PASS if ok else FAIL).append(name)
    print(f"{'✓' if ok else '✗'} {name}")


def skip(note: str):
    """跳过注记：不进 FAIL，但计入末尾汇总——环境不满足时宁缺勿误杀。"""
    SKIP.append(note)
    print(f"○ SKIP {note}")


def _new_task(task_id: str) -> None:
    with SessionLocal() as s:
        s.add(TaskRun(id=task_id, type="skill", status="running", payload={"probe": True}))
        s.commit()


def _row(task_id: str) -> TaskRun:
    with SessionLocal() as s:
        return s.get(TaskRun, task_id)


def _wait_status(task_id: str, status: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = _row(task_id)
        if row is not None and row.status == status:
            return True
        time.sleep(0.05)
    return False


def _run_in_thread(tool, out: dict) -> threading.Thread:
    def _call():
        out["res"] = tool.execute(questions=QUESTIONS)

    th = threading.Thread(target=_call, daemon=True)
    th.start()
    return th


def _pending_of(task_id: str) -> dict:
    return ((_row(task_id).payload or {}).get("pending_question")) or {}


def _post_answer(task_id: str, **body):
    """直调端点函数（sync def）：成功返回 (200, {"ok": True})，HTTPException 返回 (code, detail)。"""
    try:
        result = api.answer_task(task_id, api.AnswerRequest(**body))
        return 200, result
    except HTTPException as e:
        return e.status_code, e.detail


def _stage_waiting(task_id: str, pq_id: str, with_bridge: bool = True,
                   deadline: float | None = None):
    """手工布置「行 waiting + pending_question + 可选注册桥」——无工具线程 owning，
    状态不会被异步清走，409/400 分支得以确定性命中（重启僵尸即 with_bridge=False；
    deadline 传过去时刻即得「已过期桥」）。"""
    _new_task(task_id)
    _sync_update(task_id, status="waiting", payload={"probe": True, "pending_question": {
        "id": pq_id,
        "questions": [{"question": "Q？", "options": [
            {"label": "A", "description": "a"}, {"label": "B", "description": "b"}],
            "multiSelect": False}],
        "asked_at": "2026-01-01T00:00:00.000+00:00",
    }})
    if with_bridge:
        bridge = askuser.Bridge(
            task_id, time.monotonic() + 60 if deadline is None else deadline)
        with askuser._REGISTRY_LOCK:
            askuser._REGISTRY[task_id] = bridge
        return bridge
    return None


def _unstage(task_id: str, bridge) -> None:
    if bridge is not None:
        askuser._unregister(task_id, bridge)


# ── answer 全链：端点投递 → 工具线程收到含答案的 ToolResult ──────────────────


def scenario_answer(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("answer: 任务进入 waiting", _wait_status(task_id, "waiting"))
    pq_id = _pending_of(task_id).get("id")
    code, body = _post_answer(task_id, id=pq_id, answers=ANSWERS)
    check("answer: 200 且 ok:true", code == 200 and body == {"ok": True})
    th.join(timeout=5)
    res = out.get("res")
    check("answer: 工具线程收到 answered ToolResult（文案逐字、非错误）",
          res is not None and res.is_error is False and res.content == ANSWERED_TEXT)
    row = _row(task_id)
    logs = [e.get("msg", "") for e in (row.logs or [])]
    check("answer: 时间线 ✅ 留痕逐字", f"✅ 应答：{ANSWER_SUMMARY}" in logs)
    payload = row.payload or {}
    check("answer: 行被条件清场回 running 且 pq 清除（端点没碰，工具侧单写者）",
          row.status == "running" and "pending_question" not in payload)
    check("answer: 桥已注销", askuser.get_bridge(task_id) is None)


# ── cancel 全链：端点取消 → 工具线程收到 is_error ────────────────────────────


def scenario_cancel(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("cancel: 任务进入 waiting", _wait_status(task_id, "waiting"))
    pq_id = _pending_of(task_id).get("id")
    code, body = _post_answer(task_id, id=pq_id, cancel=True)
    check("cancel: 200 且 ok:true", code == 200 and body == {"ok": True})
    th.join(timeout=5)
    res = out.get("res")
    check("cancel: 工具线程收到取消 is_error",
          res is not None and res.is_error is True and res.content == CANCEL_TEXT)
    row = _row(task_id)
    logs = [e.get("msg", "") for e in (row.logs or [])]
    check("cancel: 时间线 ⛔ 留痕逐字", "⛔ 用户取消了应答" in logs)
    payload = row.payload or {}
    check("cancel: 行被条件清场回 running 且 pq 清除",
          row.status == "running" and "pending_question" not in payload)


# ── 404 / 409 非 waiting（running 与 success 各验一态）──────────────────────


def scenario_not_found_and_not_waiting() -> None:
    code, detail = _post_answer("anstest-no-such", id="whatever", answers=ANSWERS)
    check("404: 无此任务", code == 404)

    tid = "anstest-not-waiting"
    _new_task(tid)  # running
    code, detail = _post_answer(tid, id="whatever", answers=ANSWERS)
    check("409 非 waiting[running]: 文案可区分", code == 409 and "不在等待应答状态" in detail)
    _sync_update(tid, status="success", finished_at=datetime.now(timezone.utc))
    code, detail = _post_answer(tid, id="whatever", answers=ANSWERS)
    check("409 非 waiting[success]: 同样拒绝", code == 409 and "不在等待应答状态" in detail)


# ── 409 无桥：行 waiting 但注册表空（重启僵尸/竞态窗口）─────────────────────


def scenario_no_bridge(task_id: str) -> None:
    _stage_waiting(task_id, "pq-zombie", with_bridge=False)
    code, detail = _post_answer(task_id, id="pq-zombie", answers=ANSWERS)
    check("409 无桥: 文案可区分", code == 409 and "桥" in detail)
    row = _row(task_id)
    check("409 无桥: 端点不动行（仍 waiting、pq 原样——不抢工具侧的单写者）",
          row.status == "waiting" and _pending_of(task_id).get("id") == "pq-zombie")


# ── 409 id 不匹配：真工具挂起，req.id 对不上 ────────────────────────────────


def scenario_id_mismatch(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("id 不匹配: 任务进入 waiting", _wait_status(task_id, "waiting"))
    code, detail = _post_answer(task_id, id="wrong-id", answers=ANSWERS)
    check("id 不匹配: 409 文案可区分", code == 409 and "不匹配" in detail)
    check("id 不匹配: 行未被扰动（仍 waiting、真 pq 原样）",
          _row(task_id).status == "waiting" and _pending_of(task_id).get("id") is not None)
    askuser.abort_bridge(task_id)  # 收尾：叫醒工具线程走超时清场
    th.join(timeout=5)
    check("id 不匹配: 工具线程经 abort 收尾退出", not th.is_alive())


# ── 409 问题轮替（TOCTOU）：读行与取桥之间 pq 已换新，旧 id 不得 latch 进新问 ─


def scenario_rotation(task_id: str) -> None:
    bridge = _stage_waiting(task_id, "pq-old")
    orig = askuser.get_bridge

    def rotating_get_bridge(tid):
        got = orig(tid)
        payload = dict(_row(tid).payload or {})  # 模拟超时→重问：行已换下一问的 pq id
        if isinstance(payload.get("pending_question"), dict):
            payload["pending_question"] = {**payload["pending_question"], "id": "pq-next"}
            _sync_update(tid, payload=payload)
        return got

    askuser.get_bridge = rotating_get_bridge
    try:
        code, detail = _post_answer(task_id, id="pq-old", answers=ANSWERS)
        check("问题轮替: TOCTOU 复核拦下（fresh pq.id 已换新）且不 latch",
              code == 409 and "轮替" in detail and bridge.result is None)
    finally:
        askuser.get_bridge = orig
        _unstage(task_id, bridge)


# ── 409 重复投递（已 latch）+ ✅ 摘要 120 截断 ──────────────────────────────


def scenario_duplicate(task_id: str) -> None:
    long_q, long_a = "长问" * 80, "长答" * 80
    long_answers = [{"question": long_q, "answer": long_a}]
    long_summary = "；".join(f"{e['question']} => {e['answer']}" for e in long_answers)[:120]
    bridge = _stage_waiting(task_id, "pq-latch")
    try:
        code, body = _post_answer(task_id, id="pq-latch", answers=long_answers)
        check("重复投递: 首投 200", code == 200 and body == {"ok": True})
        logs = [e.get("msg", "") for e in (_row(task_id).logs or [])]
        expected = f"✅ 应答：{long_summary}"
        check("重复投递: ✅ 摘要截 120 逐字",
              expected in logs and len(long_summary) == 120)
        code, detail = _post_answer(task_id, id="pq-latch", answers=ANSWERS)
        check("重复投递: 二投 409（deliver False）且文案涵盖已应答/已关闭/已过期",
              code == 409 and "已关闭" in detail and "已过期" in detail)
        check("重复投递: latch 首写不被覆盖", bridge.result[0] == "answered"
              and bridge.result[1] == long_answers)
    finally:
        _unstage(task_id, bridge)


# ── 400 形状：缺 id / 两无 / 两有 / answers 空 / 元素非 {question, answer:str} ─


def scenario_bad_shape(task_id: str) -> None:
    bridge = _stage_waiting(task_id, "pq-shape")
    try:
        five = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(5)]
        cases = {
            "缺 id": dict(answers=ANSWERS),
            "id 非字符串": dict(id=123, answers=ANSWERS),
            "answers 与 cancel 两无": dict(id="pq-shape"),
            "answers 与 cancel 两有": dict(id="pq-shape", answers=ANSWERS, cancel=True),
            "cancel:false 且无 answers（视同缺省→两无）": dict(id="pq-shape", cancel=False),
            "cancel:false 且带 answers（false 也算传了 cancel→两有）":
                dict(id="pq-shape", cancel=False, answers=ANSWERS),
            "cancel 非布尔": dict(id="pq-shape", cancel="true"),
            "answers 非数组": dict(id="pq-shape", answers="裸字符串"),
            "answers 空": dict(id="pq-shape", answers=[]),
            "answers 超 4 条": dict(id="pq-shape", answers=five),
            "元素缺 answer": dict(id="pq-shape", answers=[{"question": "Q"}]),
            "元素 question 空串": dict(id="pq-shape", answers=[{"question": "  ", "answer": "A"}]),
            "元素 answer 空串": dict(id="pq-shape", answers=[{"question": "Q", "answer": ""}]),
            "元素 answer 非字符串": dict(id="pq-shape", answers=[{"question": "Q", "answer": 3}]),
            "元素非对象": dict(id="pq-shape", answers=["裸字符串"]),
        }
        for name, body in cases.items():
            code, detail = _post_answer(task_id, **body)
            check(f"400 形状[{name}]: 拒绝", code == 400)
        row = _row(task_id)
        check("400 形状: 全程不动行、不 latch（仍 waiting、pq 原样、桥空转）",
              row.status == "waiting" and _pending_of(task_id).get("id") == "pq-shape"
              and bridge.result is None)
    finally:
        _unstage(task_id, bridge)


# ── 409 无等待问题：行 waiting、桥在，但 payload 里没有 pending_question ─────


def scenario_no_pending_question(task_id: str) -> None:
    bridge = _stage_waiting(task_id, "pq-stale")
    try:
        payload = dict(_row(task_id).payload or {})
        payload.pop("pending_question", None)  # 模拟轮替竞态：行 waiting 但 pq 已被清
        _sync_update(task_id, payload=payload)
        code, detail = _post_answer(task_id, id="pq-stale", answers=ANSWERS)
        check("无等待问题: 409 报「当前没有等待中的问题」而非 id 不匹配",
              code == 409 and "没有等待中的问题" in detail and "不匹配" not in detail)
    finally:
        _unstage(task_id, bridge)


# ── 409 已过期桥：deadline 已过，deliver 拒收（端点层覆盖）──────────────────


def scenario_expired_bridge(task_id: str) -> None:
    bridge = _stage_waiting(task_id, "pq-expired", deadline=time.monotonic() - 1)
    try:
        code, detail = _post_answer(task_id, id="pq-expired", answers=ANSWERS)
        check("已过期桥: 409 且文案涵盖过期（不误报 200）",
              code == 409 and "已过期" in detail)
    finally:
        _unstage(task_id, bridge)


# ── 路由级契约：TestClient 真回包（不进 context manager，不跑 lifespan）────


def scenario_http_route() -> None:
    tid = "anstest-http"
    bridge = _stage_waiting(tid, "pq-http")
    try:
        r = CLIENT.post(f"/api/tasks/{tid}/answer", json={"id": "pq-http", "cancel": True})
        check("路由级: 真路由 200 且 ok:true", r.status_code == 200 and r.json() == {"ok": True})
        logs = [e.get("msg", "") for e in (_row(tid).logs or [])]
        check("路由级: ⛔ 留痕经真路由落库", "⛔ 用户取消了应答" in logs)
        r = CLIENT.post("/api/tasks/no-such-task/answer",
                        json={"id": "x", "answers": [{"question": "q", "answer": "a"}]})
        check("路由级: 不存在 task_id 真回 404", r.status_code == 404)
        r = CLIENT.post(f"/api/tasks/{tid}/answer", json={"id": 123})
        check("路由级: id 传 int 真回 400 非 422", r.status_code == 400)
        r = CLIENT.post(f"/api/tasks/{tid}/answer",
                        json={"answers": [{"question": "q", "answer": "a"}]})
        check("路由级: 缺 id 真回 400 非 422", r.status_code == 400)
    finally:
        _unstage(tid, bridge)


# ── 终态三路清场：manager 真路径（success/failed/cancelled）+ 幂等 ───────────


def scenario_terminal_cleanup() -> None:
    bridges: dict[str, askuser.Bridge] = {}

    def _plant(task_id: str) -> askuser.Bridge:
        """布置 waiting + pq + 注册桥，模拟「问询挂起期间任务层写终态」。"""
        _sync_update(task_id, status="waiting", payload={"pending_question": {
            "id": f"pq-{task_id}", "questions": [], "asked_at": "2026-01-01T00:00:00"}})
        bridge = askuser.Bridge(task_id, time.monotonic() + 60)
        with askuser._REGISTRY_LOCK:
            askuser._REGISTRY[task_id] = bridge
        return bridge

    async def probe_ok(task_id, progress, log):
        bridges["ok"] = _plant(task_id)

    async def probe_fail(task_id, progress, log):
        bridges["fail"] = _plant(task_id)
        raise RuntimeError("probe-boom")

    async def probe_cancel(task_id, progress, log):
        bridges["cancel"] = _plant(task_id)
        await asyncio.sleep(60)

    async def scene() -> None:
        tid = manager.submit("probe", probe_ok)
        PROBE_TIDS.append(tid)
        await manager._tasks[tid]  # _runner 跑完 = 终态写入 + 清场已执行
        row = _row(tid)
        check("终态[success]: pq 被清且 status 照终态",
              row.status == "success" and "pending_question" not in (row.payload or {}))
        check("终态[success]: 桥被 abort（幂等注销，投递被拒）",
              bridges["ok"].deliver("answered", ANSWERS) is False)

        tid = manager.submit("probe", probe_fail)
        PROBE_TIDS.append(tid)
        await manager._tasks[tid]
        row = _row(tid)
        check("终态[failed]: pq 被清且 status 照终态",
              row.status == "failed" and "probe-boom" in (row.error or "")
              and "pending_question" not in (row.payload or {}))
        check("终态[failed]: 桥被 abort", bridges["fail"].deliver("answered", ANSWERS) is False)

        tid = manager.submit("probe", probe_cancel)
        PROBE_TIDS.append(tid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "cancel" not in bridges:
            await asyncio.sleep(0.02)
        manager._tasks[tid].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manager._tasks[tid]
        row = _row(tid)
        check("终态[cancelled]: pq 被清且 status 照终态",
              row.status == "failed" and row.error == "已取消"
              and "pending_question" not in (row.payload or {}))
        check("终态[cancelled]: 桥被 abort", bridges["cancel"].deliver("answered", ANSWERS) is False)
        return tid

    last_tid = asyncio.run(scene())

    try:  # 幂等与静默：重复调、行不存在都不外抛
        _clear_pending_question(last_tid)
        _clear_pending_question("anstest-no-such-row")
        ok = True
    except Exception:
        ok = False
    check("终态: 清场幂等且无异常外抛", ok)

    for key, tid in zip(("ok", "fail", "cancel"), PROBE_TIDS):
        _unstage(tid, bridges[key])


# ── 孤儿清扫：重启时 running 与 waiting（带 pq）都置 failed 并清 pq ─────────


def scenario_orphan_sweep() -> None:
    # 前置守卫：真清扫打全库，服务器在跑真实任务时执行本场景会误杀——检测到非
    # anstest- 前缀的 running/waiting 行就 SKIP（不进 FAIL），环境干净才照跑
    with SessionLocal() as s:
        live = s.execute(
            select(TaskRun.id).where(
                TaskRun.status.in_(("running", "waiting")),
                ~TaskRun.id.like("anstest-%"),
            )
        ).scalars().all()
    if live:
        skip(f"孤儿清扫: 检测到 {len(live)} 行真实 running/waiting 任务，跳过以免误杀")
        return

    tid_run, tid_wait, tid_done = "anstest-orphan-run", "anstest-orphan-wait", "anstest-orphan-done"
    pq = {"id": "pq-orphan", "questions": [], "asked_at": "2026-01-01T00:00:00"}
    _new_task(tid_run)
    _sync_update(tid_run, payload={"probe": True, "pending_question": pq})  # running 带 pq
    _new_task(tid_wait)
    _sync_update(tid_wait, status="waiting", payload={"probe": True, "pending_question": pq})
    _new_task(tid_done)
    _sync_update(tid_done, status="success", finished_at=datetime.now(timezone.utc))  # 终态不在清扫范围

    main_app._fail_orphan_tasks()

    for label, tid in (("running", tid_run), ("waiting", tid_wait)):
        row = _row(tid)
        check(f"孤儿[{label}]: 置 failed「服务重启，任务中断」且有 finished_at",
              row.status == "failed" and row.error == "服务重启，任务中断"
              and row.finished_at is not None)
        check(f"孤儿[{label}]: pq 清除", "pending_question" not in (row.payload or {}))
    row = _row(tid_done)
    check("孤儿[success]: 终态行不受清扫影响",
          row.status == "success" and row.error == "")


def main() -> None:
    TaskRun.__table__.create(engine, checkfirst=True)  # 全新环境也能跑（已存在则跳过）
    with SessionLocal() as s:  # 预清残留（上次硬杀兜底）：anstest- 行 + 本测试的 probe 类型行
        s.execute(delete(TaskRun).where(TaskRun.id.like("anstest-%")))
        s.execute(delete(TaskRun).where(TaskRun.type == "probe"))
        s.commit()
    try:
        scenario_answer("anstest-answer")
        scenario_cancel("anstest-cancel")
        scenario_not_found_and_not_waiting()
        scenario_no_bridge("anstest-no-bridge")
        scenario_no_pending_question("anstest-no-pq")
        scenario_id_mismatch("anstest-mismatch")
        scenario_rotation("anstest-rotation")
        scenario_duplicate("anstest-duplicate")
        scenario_bad_shape("anstest-shape")
        scenario_expired_bridge("anstest-expired")
        scenario_http_route()
        scenario_terminal_cleanup()
        scenario_orphan_sweep()
    finally:
        with SessionLocal() as s:  # 清理探针行，不污染真实任务列表
            s.execute(delete(TaskRun).where(TaskRun.id.like("anstest-%")))
            s.execute(delete(TaskRun).where(TaskRun.type == "probe"))
            s.commit()

    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 条 SKIP 注记")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
