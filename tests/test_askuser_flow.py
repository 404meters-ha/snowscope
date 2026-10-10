"""AskUser 问题桥六情形直跑断言（纯断言脚本，无 pytest 依赖、不跑真 LLM）。

用法：项目根执行 .venv/Scripts/python.exe tests/test_askuser_flow.py
覆盖 I/O 矩阵：answered / cancelled / timed-out / 内部异常清场 / latch 拒二写 /
abort 分片退出 + 参数不合法（不写 DB 不注册）+ schema 对齐 vendor。
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import delete  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.models import TaskRun  # noqa: E402
from app.services import askuser  # noqa: E402
from app.services.askuser import AskUserQuestionTool  # noqa: E402
from app.tasks import _append_log, _sync_update  # noqa: E402

PASS, FAIL = [], []

# 退出四路文案逐字（answered 为两问形式，钉死 join 格式）
ANSWERED_TEXT = "User answered:\nQ1 先做哪个方向？ => A. 后端接口\nQ2 要不要写测试？ => 要"
CANCEL_TEXT = "User cancelled the question."
TIMEOUT_TEXT = "User did not answer in time; the question timed out."

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


def check(name: str, ok: bool):
    (PASS if ok else FAIL).append(name)
    print(f"{'✓' if ok else '✗'} {name}")


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


def _check_cleanup(label: str, task_id: str, want_trace: bool = True) -> None:
    row = _row(task_id)
    payload = row.payload or {}
    check(f"{label}: 条件清场回 running 且 pending_question 清除",
          row.status == "running" and "pending_question" not in payload)
    check(f"{label}: 桥已注销", askuser.get_bridge(task_id) is None)
    if want_trace:
        logs = [e.get("msg", "") for e in (row.logs or [])]
        check(f"{label}: 时间线含 ❓ 留痕", any("❓" in m for m in logs))


def _run_in_thread(tool, out: dict) -> threading.Thread:
    def _call():
        out["res"] = tool.execute(questions=QUESTIONS)

    th = threading.Thread(target=_call, daemon=True)
    th.start()
    return th


def _pending_of(task_id: str) -> dict:
    return ((_row(task_id).payload or {}).get("pending_question")) or {}


# ── 静态：工具名 / schema 对齐 vendor / is_read_only ─────────────────────────


def scenario_static() -> None:
    tool = AskUserQuestionTool("asktest-static", None, time.monotonic() + 30)
    check("工具名与 vendor 同名同义", tool.name == "AskUserQuestion")
    check("is_read_only=False（借引擎非只读单跑）", tool.is_read_only() is False)
    schema = tool.input_schema
    q = schema["properties"]["questions"]
    check("schema: questions 1-4 问", q.get("minItems") == 1 and q.get("maxItems") == 4)
    item = q["items"]
    check("schema: 每问必填 question/options", set(item.get("required", [])) == {"question", "options"})
    opts = item["properties"]["options"]
    check("schema: 选项 2-4 个", opts.get("minItems") == 2 and opts.get("maxItems") == 4)
    check("schema: 选项必填 label/description", set(opts["items"].get("required", [])) == {"label", "description"})
    ms = item["properties"]["multiSelect"]
    check("schema: multiSelect 布尔默认 false", ms.get("type") == "boolean" and ms.get("default") is False)
    check("schema: 顶层必填 questions", schema.get("required") == ["questions"])


# ── latch：答案槽首写生效 ────────────────────────────────────────────────────


def scenario_latch() -> None:
    b = askuser.Bridge("asktest-latch", time.monotonic() + 10)
    check("latch: 非法 kind 被拒且不 latch", b.deliver("bogus", None) is False and b.result is None)
    check("latch: 首写生效", b.deliver("answered", ANSWERS) is True)
    check("latch: 二写被拒（端点据此转 409）", b.deliver("cancelled", None) is False)
    check("latch: 首写结果不被覆盖", b.result == ("answered", ANSWERS))
    check("latch: 已 latch 的 wait 立即取到结果", b.wait() == ("answered", ANSWERS))
    expired = askuser.Bridge("asktest-latch-exp", time.monotonic() - 1)
    check("latch: deadline 已过的 wait 立即超时返回 None", expired.wait() is None)
    check("latch: 已关闭的桥拒投递（超时后答案不误报 200）", expired.deliver("answered", ANSWERS) is False)
    aborted = askuser.Bridge("asktest-latch-abort", time.monotonic() + 10)
    aborted.abort()
    check("latch: 已中止的桥拒投递", aborted.deliver("answered", ANSWERS) is False)
    check("latch: 已中止的 wait 立即返回 None", aborted.wait() is None)


# ── answered：另线程投递答案 ─────────────────────────────────────────────────


def scenario_answered(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("answered: 任务进入 waiting", _wait_status(task_id, "waiting"))
    bridge = askuser.get_bridge(task_id)
    check("answered: 桥已注册", bridge is not None)
    pending = _pending_of(task_id)
    q1 = (pending.get("questions") or [{}])[0]
    shape_ok = (
        isinstance(pending.get("id"), str) and pending["id"]
        and isinstance(pending.get("asked_at"), str)
        and len(pending.get("questions") or []) == 2
        and q1.get("question") == "Q1 先做哪个方向？"
        and q1.get("multiSelect") is False
        and q1.get("options") == [
            {"label": "A. 后端接口", "description": "先搭 API"},
            {"label": "B. 前端页面", "description": "先出界面"},
        ]
    )
    check("answered: pending_question 形状（id/questions/asked_at）", shape_ok)
    check("answered: 投递被接受", bridge.deliver("answered", ANSWERS) is True)
    th.join(timeout=5)
    res = out.get("res")
    check("answered: 文案逐字且非错误",
          res is not None and res.content == ANSWERED_TEXT and res.is_error is False)
    _check_cleanup("answered", task_id)


# ── cancelled：投递取消 ──────────────────────────────────────────────────────


def scenario_cancelled(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("cancelled: 任务进入 waiting", _wait_status(task_id, "waiting"))
    bridge = askuser.get_bridge(task_id)
    check("cancelled: 投递取消被接受", bridge.deliver("cancelled", None) is True)
    th.join(timeout=5)
    res = out.get("res")
    check("cancelled: 文案逐字且 is_error",
          res is not None and res.content == CANCEL_TEXT and res.is_error is True)
    _check_cleanup("cancelled", task_id)


# ── timed-out：deadline 到期 ─────────────────────────────────────────────────


def scenario_timed_out(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 1.5)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("timed-out: 曾进入 waiting", _wait_status(task_id, "waiting"))
    th.join(timeout=5)
    res = out.get("res")
    check("timed-out: 超时文案且 is_error",
          res is not None and res.content == TIMEOUT_TEXT and res.is_error is True)
    _check_cleanup("timed-out", task_id)


# ── abort：abort_bridge 置位+set，分片内退出 ─────────────────────────────────


def scenario_abort(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 60)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("abort: 曾进入 waiting", _wait_status(task_id, "waiting"))
    t0 = time.monotonic()
    askuser.abort_bridge(task_id)
    th.join(timeout=3)
    elapsed = time.monotonic() - t0
    res = out.get("res")
    check("abort: ≤2s 退出且走 timed-out 文案",
          (not th.is_alive()) and elapsed <= 2.0
          and res is not None and res.content == TIMEOUT_TEXT and res.is_error is True)
    _check_cleanup("abort", task_id)
    try:
        askuser.abort_bridge("asktest-no-such-bridge")
        silent = True
    except Exception:
        silent = False
    check("abort: 无桥 task_id 静默幂等", silent)


# ── 预终态：任务先 failed 再 execute（_mark_waiting 条件落空真路径）──────────


def scenario_prefailed(task_id: str) -> None:
    _new_task(task_id)
    _sync_update(task_id, status="failed")  # 模拟 run() 已写终态
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    res = tool.execute(questions=QUESTIONS)  # 同步：_mark_waiting 落空即抛错走异常路
    check("预终态: is_error 且含 no longer running", res.is_error and "no longer running" in res.content)
    row = _row(task_id)
    check("预终态: 行仍 failed、无 pending_question",
          row.status == "failed" and "pending_question" not in (row.payload or {}))
    check("预终态: 桥未注册（已注销）", askuser.get_bridge(task_id) is None)


# ── 等待中外部写终态再投递（AD-1：清场不得复活终态）─────────────────────────


def scenario_terminal_during_wait(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("终态竞态: 任务进入 waiting", _wait_status(task_id, "waiting"))
    _sync_update(task_id, status="failed")  # 模拟 run() 超时收尾写终态
    bridge = askuser.get_bridge(task_id)
    check("终态竞态: 投递仍被消费", bridge is not None and bridge.deliver("answered", ANSWERS) is True)
    th.join(timeout=5)
    res = out.get("res")
    check("终态竞态: 文案逐字", res is not None and res.content == ANSWERED_TEXT and res.is_error is False)
    row = _row(task_id)
    check("终态竞态: 清场不复活终态（仍 failed）且 pending_question 兜底清除",
          row.status == "failed" and "pending_question" not in (row.payload or {}))
    check("终态竞态: 桥已注销", askuser.get_bridge(task_id) is None)


# ── 同任务二次 execute：注册表守卫 ───────────────────────────────────────────


def scenario_double_ask(task_id: str) -> None:
    _new_task(task_id)
    tool1 = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out1: dict = {}
    th1 = _run_in_thread(tool1, out1)
    check("二次问: 首问进入 waiting", _wait_status(task_id, "waiting"))
    bridge1 = askuser.get_bridge(task_id)
    first_id = _pending_of(task_id).get("id")
    tool2 = AskUserQuestionTool(task_id, lambda msg: None, time.monotonic() + 30)
    res2 = tool2.execute(questions=QUESTIONS)
    check("二次问: is_error 且含 already waiting", res2.is_error and "already waiting" in res2.content)
    check("二次问: 首问行不受影响（仍 waiting、pending 原样）",
          _row(task_id).status == "waiting" and _pending_of(task_id).get("id") == first_id)
    check("二次问: 桥仍是首问那座", askuser.get_bridge(task_id) is bridge1)
    check("二次问: 首问投递收尾被接受", bridge1 is not None and bridge1.deliver("answered", ANSWERS) is True)
    th1.join(timeout=5)
    check("二次问: 首问文案逐字", out1.get("res") is not None and out1["res"].content == ANSWERED_TEXT)
    _check_cleanup("二次问", task_id)


# ── 乱序 answers：倒序投递仍按提问顺序出文案 ────────────────────────────────


def scenario_reversed_answers(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    out: dict = {}
    th = _run_in_thread(tool, out)
    check("乱序答: 任务进入 waiting", _wait_status(task_id, "waiting"))
    bridge = askuser.get_bridge(task_id)
    check("乱序答: 倒序投递被接受",
          bridge is not None and bridge.deliver("answered", list(reversed(ANSWERS))) is True)
    th.join(timeout=5)
    res = out.get("res")
    check("乱序答: 文案仍按提问顺序", res is not None and res.content == ANSWERED_TEXT)
    _check_cleanup("乱序答", task_id)


# ── deadline 已过预检：不注册不写 DB（无幻影问题）───────────────────────────


def scenario_expired_precheck(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() - 1)
    res = tool.execute(questions=QUESTIONS)
    check("已过期预检: 直接超时文案 is_error", res.content == TIMEOUT_TEXT and res.is_error is True)
    row = _row(task_id)
    check("已过期预检: 不注册不写 DB（无幻影问题、无 ❓ 闪跳）",
          row.status == "running" and "pending_question" not in (row.payload or {}))
    check("已过期预检: 桥未注册", askuser.get_bridge(task_id) is None)


# ── 清场自身抛错：异常不外抛、桥仍注销 ──────────────────────────────────────


def scenario_cleanup_failure(task_id: str) -> None:
    _new_task(task_id)
    orig = askuser._restore_running

    def boom_restore(tid):
        raise RuntimeError("cleanup-boom")

    askuser._restore_running = boom_restore
    out: dict = {}
    try:
        tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
        th = _run_in_thread(tool, out)
        check("清场失败: 曾进入 waiting", _wait_status(task_id, "waiting"))
        bridge = askuser.get_bridge(task_id)
        check("清场失败: 投递被接受", bridge is not None and bridge.deliver("answered", ANSWERS) is True)
        th.join(timeout=5)
    finally:
        askuser._restore_running = orig
    res = out.get("res")
    check("清场失败: 异常不外抛、就地转错误文案",
          res is not None and res.is_error and "cleanup failed" in res.content and "cleanup-boom" in res.content)
    check("清场失败: 桥仍被注销（无泄漏，不阻塞后续提问）", askuser.get_bridge(task_id) is None)


# ── 内部异常：转 is_error 不上抛，finally 清场 ───────────────────────────────


def scenario_exception(task_id: str) -> None:
    _new_task(task_id)
    seen: list[str] = []
    orig = askuser._mark_waiting

    def boom(tid, pending):
        orig(tid, pending)  # 真 DB 写（waiting + pending_question）
        seen.append(_row(tid).status)
        raise RuntimeError("boom")

    askuser._mark_waiting = boom
    try:
        tool = AskUserQuestionTool(task_id, lambda msg: None, time.monotonic() + 30)
        res = tool.execute(questions=QUESTIONS)
    finally:
        askuser._mark_waiting = orig
    check("异常: 转 is_error ToolResult 且不上抛", res.is_error and "boom" in res.content)
    check("异常: 异常前确已写入 waiting（清场非空转）", seen == ["waiting"])
    _check_cleanup("异常", task_id, want_trace=False)  # 该场景 log_cb 为空，无 ❓ 断言


# ── 参数不合法：不等待、不写 DB、不注册 ──────────────────────────────────────


def scenario_invalid(task_id: str) -> None:
    _new_task(task_id)
    tool = AskUserQuestionTool(task_id, lambda msg: _append_log(task_id, msg), time.monotonic() + 30)
    five_opts = [{"label": f"L{i}", "description": "d"} for i in range(5)]
    no_desc = {"label": "X", "description": ""}
    cases = {
        "空 questions": lambda: tool.execute(questions=[]),
        "缺 questions 键": lambda: tool.execute(),
        "超过 4 问": lambda: tool.execute(
            questions=[{**QUESTIONS[0], "question": f"Q{i}"} for i in range(5)]),
        "选项不足 2": lambda: tool.execute(
            questions=[{**QUESTIONS[0], "options": QUESTIONS[0]["options"][:1]}]),
        "选项超过 4": lambda: tool.execute(
            questions=[{**QUESTIONS[0], "options": five_opts}]),
        "description 缺失": lambda: tool.execute(
            questions=[{**QUESTIONS[0], "options": [no_desc, {"label": "Y", "description": "d"}]}]),
        "question 文本重复": lambda: tool.execute(
            questions=[QUESTIONS[0], {**QUESTIONS[1], "question": QUESTIONS[0]["question"]}]),
    }
    for name, call in cases.items():
        res = call()
        check(f"参数不合法[{name}]: 直接参数错误 is_error",
              res.is_error and any(k in res.content.lower() for k in ("question", "option")))
    row = _row(task_id)
    check("参数不合法: 不写 DB（仍 running、无 pending_question、payload 原样）",
          row.status == "running" and "pending_question" not in (row.payload or {})
          and (row.payload or {}).get("probe") is True)
    check("参数不合法: 不注册桥", askuser.get_bridge(task_id) is None)


def main() -> None:
    TaskRun.__table__.create(engine, checkfirst=True)  # 全新环境也能跑（已存在则跳过）
    with SessionLocal() as s:  # 预清残留（上次硬杀兜底），asktest- 前缀不碰真实任务
        s.execute(delete(TaskRun).where(TaskRun.id.like("asktest-%")))
        s.commit()
    try:
        scenario_static()
        scenario_latch()
        scenario_answered("asktest-answered")
        scenario_cancelled("asktest-cancelled")
        scenario_timed_out("asktest-timeout")
        scenario_abort("asktest-abort")
        scenario_prefailed("asktest-prefailed")
        scenario_terminal_during_wait("asktest-terminal")
        scenario_double_ask("asktest-double")
        scenario_reversed_answers("asktest-reversed")
        scenario_expired_precheck("asktest-expired")
        scenario_cleanup_failure("asktest-cleanupfail")
        scenario_exception("asktest-exception")
        scenario_invalid("asktest-invalid")
    finally:
        with SessionLocal() as s:  # 清理探针行，不污染真实任务列表
            s.execute(delete(TaskRun).where(TaskRun.id.like("asktest-%")))
            s.commit()

    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
