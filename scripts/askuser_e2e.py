# -*- coding: utf-8 -*-
"""Story 1.4 全链 E2E：一句话 → AskUserQuestion 提问 → HTTP 应答/取消/超时 → 答案回模型 → 终态。

用法：项目根执行 .venv/Scripts/python.exe scripts/askuser_e2e.py

链路（全程 httpx 打 8101 测试实例，等价 curl——日志里每步都留 curl 回放行）：
- 前置守卫：LLM 配置 / celestial.db 无 running/waiting 行（只读打开，读不了即中止）/ 8101 端口空闲
- 起 8101 测试实例（注入 SCHED_HOUR 避开每日刷新 cron），轮询 /api/config 200 即就绪，结束杀进程
- 应答链：POST /api/agent/run → waiting → POST /api/tasks/{id}/answer（答案「猫」）
  → 断言 success + payload.result.result 含「猫」+ 时间线 ❓→✅→完成 三段齐全
- 取消链：同 prompt 再跑一任务 → waiting → POST {id, cancel:true}
  → 断言任务正常收尾（success/failed 均可）+ 时间线含 ❓ 与 ⛔ 且 ⛔ 之后不再有 ❓
- 超时链：短命实例（SKILL_RUN_TIMEOUT 压小）再跑一任务、不投递，等问询自然过期
  → 断言时间线含 ⏳ 且任务达终态（success/failed 均可）
- 每条链收尾后补 wire 级负路径：终态任务再答→409、answers+cancel 同传→400、乱 task_id→404
- 收场清理：杀实例前把残留 running/waiting 行标 failed（守卫保证起点干净，残留皆本脚本所致）
- 归档 _bmad-output/implementation-artifacts/e2e-askuser/run-{YYYYMMDD-HHMMSS}/：
  e2e.log（脚本全输出）、server-8101.log / server-8101-short.log（实例日志）、
  task-answer.json / task-cancel.json / task-timeout.json（最终任务视图含 logs）；
  另复制最新一份为 e2e-askuser/latest/（重跑不毁前次证据）
- E2E 产生的任务行（type=agent，prompt 带【AskUser-E2E】标记）留在库中作留痕，不清理

exit 码：0 全部断言通过；1 断言失败；2 前置条件不满足（守卫触发/端口被占/LLM 未配置/归档被占）。
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # .env 与 sqlite 相对路径都以项目根为基准（从任意 cwd 调用都一致）
sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台防 GBK 乱码

PORT = 8101
BASE = f"http://127.0.0.1:{PORT}"
ARCHIVE_ROOT = ROOT / "_bmad-output" / "implementation-artifacts" / "e2e-askuser"
ARCHIVE = ARCHIVE_ROOT / f"run-{time.strftime('%Y%m%d-%H%M%S')}"  # 时间戳子目录，重跑不毁前次证据
LATEST = ARCHIVE_ROOT / "latest"

SERVER_READY_TIMEOUT = 60  # 起实例后等 /api/config 就绪的上限（秒）
POLL_INTERVAL = 1.0        # GET /api/tasks/{id} 轮询节奏（Design Notes：1s）
CHAIN_TIMEOUT = 300        # 单链上限（真 LLM 一问一答 + 总结，2-4 轮内完成）
MAX_DELIVERIES = 5         # 模型反复提问时的 2xx 投递上限（防问询风暴卡死驱动）
TIMEOUT_CHAIN_RUN = 45     # 超时链实例的 SKILL_RUN_TIMEOUT（秒）：首轮实测 5-8s 出问询，留 5x 余量
ANSWER_TOKEN = "猫"        # 预定义答案：断言 result 文本必须包含

# prompt 预定义（问题与答案都确定）：先问二选一偏好（猫/狗），拿到答案后总结中写出选择。
# 前缀标记让留在库里的任务行可识别（E2E 留痕），末句防模型自问自答跳过工具。
PROMPT = (
    "【AskUser-E2E】请先调用 AskUserQuestion 问我一个问题：'你更喜欢猫还是狗？'，"
    "选项：猫（推荐）/狗。不要自问自答，必须先调用工具等我的应答。"
    "拿到我的回答后，用一句话总结并明确写出我选择了什么。"
)

PASS, FAIL = [], []
_log_fh = None
client: httpx.Client | None = None


class Abort(Exception):
    """前置条件不满足 / 实例失联（exit 2）。"""


def say(msg: str = "") -> None:
    print(msg, flush=True)
    if _log_fh is not None:
        _log_fh.write(msg + "\n")
        _log_fh.flush()


def check(name: str, ok: bool) -> None:
    (PASS if ok else FAIL).append(name)
    say(f"{'✓' if ok else '✗'} {name}")


# ── 前置守卫 ──────────────────────────────────────────────────────────────────


def _db_path() -> Path:
    """从配置解析 sqlite 文件路径（相对路径锚到项目根，与实例 cwd 一致）。"""
    from app.config import get_settings

    url = get_settings().db_url
    raw = url.split("///", 1)[1] if url.startswith("sqlite:///") else "./celestial.db"
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / raw).resolve()


def guard_llm() -> None:
    from app.config import get_settings

    if not get_settings().llm_configured:
        raise Abort("LLM 未配置（.env 的 LLM_BASE_URL / LLM_API_KEY）——E2E 需要真模型")
    say("守卫通过：LLM 已配置（llm_configured=true）")


def guard_db() -> None:
    """库里有 running/waiting 行说明 8100 生产实例可能有任务在跑——起 8101 会触发
    启动孤儿清扫把它们标失败，必须中止并打印原因，不静默。

    只读打开（mode=ro）：写模式 connect 会无中生有建空库并在中止后残留；只读下
    locked/corrupt/缺文件都变成可探测的 sqlite3.Error——除「no such table」（库未
    初始化，实例启动会建表）放行外，其余一律 Abort：读不了库就不许起杀手实例。
    """
    path = _db_path()
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select id, type, status, created_at from task_runs "
                "where status in ('running','waiting')"
            ).fetchall()
            total = con.execute("select count(*) from task_runs").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        if "no such table" in str(e):
            say(f"守卫通过：{path.name} 尚无任务表（库未初始化，实例启动会建表）")
            return
        raise Abort(f"守卫读不了 {path}（locked/corrupt/缺文件？）：{type(e).__name__}: {e}") from e
    if rows:
        say(f"中止：{path.name} 存在 {len(rows)} 条 running/waiting 任务行——")
        say("      8100 生产实例可能有任务在跑，起 8101 会孤儿清扫误杀它们。")
        say("      等任务自然结束（或人工确认是僵尸行清掉）后再重跑本脚本。")
        for r in rows:
            say(f"      id={r[0]}  type={r[1]}  status={r[2]}  created_at={r[3]}")
        raise Abort("数据库存在 running/waiting 任务行")
    say(f"守卫通过：{path.name} 无 running/waiting 行（历史任务共 {total} 条）")


def guard_port() -> None:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", PORT))
    except OSError:
        raise Abort(f"端口 {PORT} 已被占用（可能有别的实例在跑），不硬抢") from None
    finally:
        sock.close()
    say(f"守卫通过：端口 {PORT} 空闲")


# ── HTTP（留 curl 等价行，全程可回放） ────────────────────────────────────────


def http(method: str, path: str, body: dict | None = None, quiet: bool = False) -> httpx.Response:
    if not quiet:
        line = f"curl -s -X {method} '{BASE}{path}'"
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False)
            line += f" -H 'Content-Type: application/json' -d '{payload}'"
        say(f"$ {line}")
    assert client is not None
    try:
        r = client.request(method, path, json=body)
    except httpx.HTTPError as e:
        # 链中传输异常（实例失联等）不能裸崩——转 Abort 让原因进归档日志后干净退出
        raise Abort(f"{method} {path} 传输失败（实例失联？）：{type(e).__name__}: {e}") from e
    if not quiet:
        say(f"  -> {r.status_code} {r.text[:200]}")
    return r


def get_task(task_id: str) -> dict:
    r = http("GET", f"/api/tasks/{task_id}", quiet=True)
    return r.json().get("task") if r.status_code == 200 else {}


# ── 测试实例 ──────────────────────────────────────────────────────────────────


def _sched_hour_env() -> dict:
    """实例存活窗口可能撞上每日刷新 cron（8101 也跑同一条抓取流水线）——
    注入一个不等于当前小时的 SCHED_HOUR，把触发点挪到实例必然已死的时刻。"""
    return {"SCHED_HOUR": "4" if time.localtime().tm_hour != 4 else "5"}


def start_server(server_fh, extra_env: dict | None = None) -> subprocess.Popen:
    say(f"起测试实例：uvicorn main:app --port {PORT}"
        + (f"（附加 env：{extra_env}）" if extra_env else ""))
    env = {**os.environ, "PLATFORM_API_BASE": BASE,  # Agent 平台工具回调指向测试实例自身
           **_sched_hour_env(), **(extra_env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT)],
        cwd=ROOT, stdout=server_fh, stderr=subprocess.STDOUT, env=env,
    )
    t0 = time.monotonic()
    while time.monotonic() - t0 < SERVER_READY_TIMEOUT:
        if proc.poll() is not None:
            raise Abort(f"测试实例进程提前退出（code={proc.returncode}，详见实例日志）")
        try:
            r = client.get("/api/config")
            if r.status_code == 200:
                cfg = r.json()
                if not cfg.get("llm"):
                    stop_server(proc)
                    raise Abort("实例报告 LLM 未配置（GET /api/config → llm=false）")
                say(f"实例就绪（{time.monotonic() - t0:.0f}s）：/api/config 200，llm={cfg.get('llm')}")
                return proc
        except httpx.HTTPError:
            pass  # 未就绪，继续轮询
        time.sleep(1)
    stop_server(proc)
    raise Abort(f"实例 {SERVER_READY_TIMEOUT}s 内未就绪（/api/config 不通）")


def stop_server(proc: subprocess.Popen) -> None:
    say("停测试实例（terminate + wait）")
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        say("terminate 未退出，kill 兜底")
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass  # kill 后仍不退就放弃等待，不裸崩（句柄随脚本退出回收）


def fail_residual_tasks() -> None:
    """杀实例前把库里残留的 running/waiting 行标 failed——链超时强杀会留孤儿行，
    下次守卫看到就自锁。守卫保证起点无外人行，此刻残留皆本脚本实例所致，
    直接 sqlite 写（绕开 ORM/实例，实例马上要死也无从抗议）。"""
    try:
        con = sqlite3.connect(_db_path())
        try:
            rows = con.execute(
                "select id from task_runs where status in ('running','waiting')"
            ).fetchall()
            if rows:
                con.execute(
                    "update task_runs set status='failed', error='E2E 收场清理', "
                    "finished_at=datetime('now') where status in ('running','waiting')"
                )
                con.commit()
                say(f"收场清理：{len(rows)} 条残留 running/waiting 行标 failed"
                    f"（{', '.join(r[0] for r in rows)}）")
        finally:
            con.close()
    except sqlite3.Error as e:
        say(f"收场清理失败（不影响退出）：{type(e).__name__}: {e}")


# ── 链路驱动 ──────────────────────────────────────────────────────────────────


def pick_answers(pq: dict) -> list[dict]:
    """按 pending_question 构造应答：含「猫」的选项 label 优先，否则回退字面「猫」。

    题目文本用实例回传的原句（端点按题目文本匹配优先）；答案恒含「猫」——
    prompt 只约定问一个二选一，若模型多问也逐条应答，不悬空。
    """
    answers = []
    for q in pq.get("questions") or []:
        labels = [o.get("label", "") for o in (q.get("options") or []) if isinstance(o, dict)]
        cat = next((l for l in labels if ANSWER_TOKEN in l), ANSWER_TOKEN)
        answers.append({"question": q.get("question", ""), "answer": cat})
    return answers


def pq_shape_ok(pq: dict) -> bool:
    """pending_question 契约形状：id 非空 str、questions 非空且每问有 question/options、
    asked_at 存在——畸形 pq 从静默 300s 空转变具名失败。"""
    qs = pq.get("questions")
    return (isinstance(pq.get("id"), str) and bool(pq["id"].strip())
            and isinstance(qs, list) and bool(qs)
            and all(isinstance(q, dict)
                    and isinstance(q.get("question"), str) and bool(q["question"].strip())
                    and isinstance(q.get("options"), list) and bool(q["options"])
                    for q in qs)
            and bool(pq.get("asked_at")))


def deliver(mode: str, task_id: str, pq: dict) -> bool:
    """投递一次应答/取消；仅 2xx 算成功（409 等不吃预算）。"""
    body = ({"id": pq.get("id"), "answers": pick_answers(pq)} if mode == "answer"
            else {"id": pq.get("id"), "cancel": True})
    r = http("POST", f"/api/tasks/{task_id}/answer", body)
    if 200 <= r.status_code < 300:
        return True
    say(f"  投递未成功（{r.status_code} {r.text[:120]}），下轮轮询再看任务状态")
    return False


def drive(mode: str, tag: str) -> tuple[dict, int]:
    """跑一条链：agent/run → 每秒轮询；见 waiting 即按 mode 投递 → 终态返回 (任务视图, 2xx 投递数)。

    - 投递仅 2xx 计数；已成功投过的 pq id 不再 POST（工具清场 0.5s 窗内同 pq 不重发）
    - mode="timeout"：不投递，等问询自然过期（实例以小 SKILL_RUN_TIMEOUT 起动，额度即问询 deadline）
    """
    r = http("POST", "/api/agent/run", {"prompt": PROMPT})
    if r.status_code != 200:
        raise Abort(f"POST /api/agent/run 失败：{r.status_code} {r.text[:200]}")
    task_id = r.json()["task_id"]
    say(f"[{tag}] task_id={task_id}，轮询（{POLL_INTERVAL:.0f}s 一次，上限 {CHAIN_TIMEOUT}s，模式 {mode}）")
    t0 = time.monotonic()
    deliveries, budget_logged, shape_checked = 0, False, False
    delivered_ids: set[str] = set()
    last = ""
    task: dict = {}
    while time.monotonic() - t0 < CHAIN_TIMEOUT:
        task = get_task(task_id)
        status = task.get("status", "?")
        if status != last:
            say(f"  [{tag}] {time.monotonic() - t0:6.1f}s  status: {last or '-'} -> {status}")
            last = status
        if status in ("success", "failed"):
            return task, deliveries
        if status == "waiting":
            pq = (task.get("payload") or {}).get("pending_question")
            if isinstance(pq, dict):
                if not shape_checked:  # 首个 pq 到手即验形状
                    check(f"[{tag}] pending_question 形状合法（id/questions/options/asked_at）",
                          pq_shape_ok(pq))
                    shape_checked = True
                pq_id = pq.get("id")
                if (mode != "timeout" and isinstance(pq_id, str) and pq_id
                        and pq_id not in delivered_ids):
                    if deliveries >= MAX_DELIVERIES:
                        if not budget_logged:
                            say(f"  [{tag}] 问询投递预算触顶（{MAX_DELIVERIES} 次），不再投递")
                            budget_logged = True
                    elif deliver(mode, task_id, pq):
                        delivered_ids.add(pq_id)
                        deliveries += 1
        time.sleep(POLL_INTERVAL)
    check(f"[{tag}] {CHAIN_TIMEOUT}s 内到达终态", False)
    return task, deliveries


def _msgs(task: dict) -> list[str]:
    return [e.get("msg", "") for e in (task.get("logs") or [])]


def negative_checks(tag: str, task_id: str) -> None:
    """链收尾后的 wire 级负路径：真实 POST 打端点的错误契约（409/400/404）。"""
    r1 = http("POST", f"/api/tasks/{task_id}/answer",
              {"id": "neg-probe", "answers": [{"question": "负路径", "answer": "x"}]})
    check(f"[{tag}] 终态任务再应答 → 409", r1.status_code == 409)
    r2 = http("POST", f"/api/tasks/{task_id}/answer",
              {"id": "neg-probe", "answers": [{"question": "负路径", "answer": "x"}], "cancel": True})
    check(f"[{tag}] answers 与 cancel 同传 → 400", r2.status_code == 400)
    r3 = http("POST", "/api/tasks/e2e-no-such-task/answer", {"id": "neg-probe", "cancel": True})
    check(f"[{tag}] 不存在的任务 → 404", r3.status_code == 404)


def assert_answer_chain(task: dict, deliveries: int) -> None:
    msgs = _msgs(task)
    check("应答链：任务终态 success", task.get("status") == "success")
    result_text = (((task.get("payload") or {}).get("result")) or {}).get("result") or ""
    check(f"应答链：payload.result.result 含预定义答案「{ANSWER_TOKEN}」", ANSWER_TOKEN in result_text)
    check("应答链：应答投递成功（2xx）", deliveries >= 1)

    def idx(pred) -> int:
        return next((i for i, m in enumerate(msgs) if pred(m)), -1)

    i_ask = idx(lambda m: m.startswith("❓ 等待用户应答"))
    i_ans = idx(lambda m: "✅ 应答：" in m)
    i_done = idx(lambda m: m.strip() == "完成")
    check("应答链：时间线含「❓ 等待用户应答」", i_ask >= 0)
    check("应答链：时间线含「✅ 应答：」", i_ans >= 0)
    check("应答链：时间线含「完成」", i_done >= 0)
    check("应答链：时间线顺序 ❓→✅→完成", 0 <= i_ask < i_ans < i_done)


def assert_cancel_chain(task: dict, deliveries: int) -> None:
    msgs = _msgs(task)
    check("取消链：任务正常收尾（success/failed 均可）", task.get("status") in ("success", "failed"))
    check("取消链：取消投递成功（2xx）", deliveries >= 1)
    check("取消链：时间线含「❓」", any("❓" in m for m in msgs))
    i_block = next((i for i, m in enumerate(msgs) if "⛔" in m), -1)
    check("取消链：时间线含「⛔」", i_block >= 0)
    check("取消链：⛔ 之后不再出现 ❓（取消终结问询循环）",
          i_block >= 0 and not any("❓" in m for m in msgs[i_block + 1:]))


def assert_timeout_chain(task: dict) -> None:
    msgs = _msgs(task)
    check("超时链：任务达终态（success/failed 均可）", task.get("status") in ("success", "failed"))
    check("超时链：时间线含「❓」（确实发起过问询）", any("❓" in m for m in msgs))
    check("超时链：时间线含「⏳」（问询过期留痕）", any("⏳" in m for m in msgs))


def archive_json(name: str, obj) -> None:
    p = ARCHIVE / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    say(f"已归档 {p.relative_to(ROOT)}")


def _refresh_latest() -> None:
    """把本次 run 目录复制为 latest/（Windows 符号链接要特权，复制目录更稳）；
    失败只提示，不影响退出码。"""
    tmp = LATEST.with_name("latest-tmp")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(ARCHIVE, tmp)
        shutil.rmtree(LATEST, ignore_errors=True)
        tmp.rename(LATEST)
    except OSError as e:
        print(f"（latest 指针刷新失败，不影响结果：{e}）", flush=True)


def run_instance(server_log_name: str, extra_env: dict | None, body) -> None:
    """起一个测试实例跑 body；收尾先清残留任务行再杀进程（孤儿行会让下次守卫自锁）。"""
    try:
        server_fh = open(ARCHIVE / server_log_name, "w", encoding="utf-8", newline="\n")
    except PermissionError as e:
        raise Abort(f"实例日志 {server_log_name} 打不开（被占用？并发 E2E？）：{e}") from e
    proc = None
    try:
        proc = start_server(server_fh, extra_env)
        body()
    finally:
        if proc is not None:
            fail_residual_tasks()  # 先标 failed 再杀：此刻残留皆本脚本实例所致
            stop_server(proc)
        server_fh.close()


# ── 主流程 ────────────────────────────────────────────────────────────────────


def main() -> int:
    global client, _log_fh
    try:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        log_fh = open(ARCHIVE / "e2e.log", "w", encoding="utf-8", newline="\n")
    except PermissionError as e:
        print(f"中止：归档日志打不开（被占用？并发 E2E？）：{e}", flush=True)
        return 2
    _log_fh = log_fh
    try:
        say(f"== AskUser 全链 E2E  {time.strftime('%Y-%m-%d %H:%M:%S')} ==")
        say(f"归档目录：{ARCHIVE.relative_to(ROOT)}")

        client = httpx.Client(base_url=BASE, timeout=30)
        try:
            guard_llm()
            guard_db()
            guard_port()

            def main_chains() -> None:
                t_answer, d_answer = drive("answer", "应答")
                archive_json("task-answer.json", {"task": t_answer})
                assert_answer_chain(t_answer, d_answer)
                negative_checks("应答", t_answer.get("id", ""))

                t_cancel, d_cancel = drive("cancel", "取消")
                archive_json("task-cancel.json", {"task": t_cancel})
                assert_cancel_chain(t_cancel, d_cancel)
                negative_checks("取消", t_cancel.get("id", ""))

            run_instance("server-8101.log", None, main_chains)

            def timeout_chain() -> None:
                t_to, _ = drive("timeout", "超时")
                archive_json("task-timeout.json", {"task": t_to})
                assert_timeout_chain(t_to)
                negative_checks("超时", t_to.get("id", ""))

            # 超时链要压小 run 额度（问询 deadline 与 run(timeout) 同源），只能另起短命实例注入 env
            run_instance("server-8101-short.log", {"SKILL_RUN_TIMEOUT": str(TIMEOUT_CHAIN_RUN)},
                         timeout_chain)
        except Abort as e:
            say(f"中止：{e}")
            return 2
        finally:
            client.close()

        say("")
        say(f"== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 ==")
        for f in FAIL:
            say(f"  ✗ {f}")
        return 0 if not FAIL else 1
    finally:
        _log_fh = None
        log_fh.close()
        _refresh_latest()


if __name__ == "__main__":
    sys.exit(main())
