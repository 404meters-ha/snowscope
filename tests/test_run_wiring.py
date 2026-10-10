"""Story 1.2 run() 接线与挂载矩阵直跑断言（纯断言脚本，无 pytest 依赖、不动 DB）。

用法：项目根执行 .venv/Scripts/python.exe tests/test_run_wiring.py
覆盖 I/O 矩阵：挂载正反例与 task_id 缺省、提示词两态（False 与基线逐字一致）、
run_prompt 传参（spy agent_sdk.run：技能 invoke 与 AI 命令栏共用汇点）、
run()→Engine 组装与超时/取消路径（stub Engine captor，全程无网络、确定性）、
真 LLM 超时组（可选附加：LLM 未配置或端点快于超时先完成时打 SKIP 注记，不进 FAIL）、
scaffold 调用形状回归（默认参 + 源码级零改动）。
"""
import asyncio
import inspect
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from core.engine import AbortedError  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import agent_sdk, askuser, skill_runner  # noqa: E402
from app.services.agent_sdk import _build_tools, _system_prompt  # noqa: E402
from app.services.askuser import AskUserQuestionTool  # noqa: E402

PASS, FAIL, SKIP = [], [], []

FIVE_NAMES = ["WebFetch", "PlatformAPI", "ReadFile", "ListDir", "WriteFile"]


def check(name: str, ok: bool):
    (PASS if ok else FAIL).append(name)
    print(f"{'✓' if ok else '✗'} {name}")


def skip(note: str):
    """跳过注记：不进 FAIL，但计入末尾汇总——「全绿」不等于「run() 未执行」。"""
    SKIP.append(note)
    print(f"○ SKIP {note}")


def _names(tools) -> list[str]:
    return [t.name for t in tools]


# ── 挂载矩阵：_build_tools 正反例与 task_id 缺省 ─────────────────────────────


def scenario_mounting() -> None:
    log_cb = lambda msg: None  # noqa: E731
    deadline = time.monotonic() + 3600
    tools = _build_tools("asktest-mount", log_cb, deadline, interactive=True)
    check("挂载: interactive=True 共六件、末件为 AskUserQuestionTool",
          _names(tools) == FIVE_NAMES + ["AskUserQuestion"]
          and isinstance(tools[-1], AskUserQuestionTool))
    check("挂载: task_id 注入正确", tools[-1]._task_id == "asktest-mount")
    check("挂载: deadline 注入正确（run 起点算的绝对时刻）", tools[-1]._deadline_at == deadline)
    check("挂载: log_cb 注入正确（时间线回调）", tools[-1]._log is log_cb)

    plain = _build_tools("asktest-mount", log_cb, deadline, interactive=False)
    check("不挂: interactive=False 恰为原五件",
          _names(plain) == FIVE_NAMES
          and not any(isinstance(t, AskUserQuestionTool) for t in plain))
    check("不挂: interactive 缺省（scaffold 调用形状）同为五件",
          _names(_build_tools(None, None, 0.0)) == FIVE_NAMES)
    no_tid = _build_tools(None, log_cb, deadline, interactive=True)
    check("缺 task_id: interactive=True 不挂工具、不报错",
          _names(no_tid) == FIVE_NAMES
          and not any(isinstance(t, AskUserQuestionTool) for t in no_tid))
    empty_tid = _build_tools("", log_cb, deadline, interactive=True)
    check("缺 task_id: 空串视同缺失，不挂工具", _names(empty_tid) == FIVE_NAMES)


# ── 提示词两态：False 与 1.1 基线逐字一致，True 增「向用户提问」段 ───────────


def _legacy_system_prompt(s) -> str:
    """Story 1.1 交付时点的 _system_prompt() 原文（逐字拷贝，钉死 False 分支不漂移）。"""
    return f"""你是 celestial-snow 平台内置的开源项目分析 agent，运行在云服务进程内。

## 环境与能力
- 你运行在云服务进程内，没有 Shell。工具四个：WebFetch（抓远程网页/GitHub）、PlatformAPI（调本平台 API）、
  ReadFile/ListDir（读 .claude/skills/ 技能资产与 courses/、scaffolds/ 产物文件）、WriteFile（只能写 courses/ 或 scaffolds/ 下）。
- 分析对象是 GitHub 上的开源项目代码与 issue：一切信息通过网络获取，禁止凭空编造。

## 平台工具手册（生成课程一类任务用）
- 平台 API 一律走 PlatformAPI，不要用 WebFetch 打本机地址：取学习上下文 GET /api/learning-context/{{issue_id}}；
  注册课程 POST /api/courses（body 含 issue_id/title/lessons）；发布课程 POST /api/courses/{{id}}/publish（重生成后加 body {{"prune": true}}）。
- 课程文件写到 courses/{{course_id}}/ 下（WriteFile 只认这个范围）；assets/style.css 与 assets/quiz.js 等共享模板
  在 .claude/skills/tech/assets/，用 ReadFile 读出后原样 WriteFile 到课程目录，不要自己重写。
- 写完用 ListDir 核对 courses/{{course_id}}/ 文件齐全、与注册的 lessons 一致。

## GitHub 数据获取手册（用 WebFetch 抓）
- 仓库元数据：https://api.github.com/repos/{{owner}}/{{repo}}（star/主语言/默认分支/描述）
- 目录浏览：https://api.github.com/repos/{{owner}}/{{repo}}/contents/{{path}}（JSON，含各文件 download_url）
- 源码原文：https://raw.githubusercontent.com/{{owner}}/{{repo}}/{{分支}}/{{路径}}
- README：直接抓 https://raw.githubusercontent.com/{{owner}}/{{repo}}/{{分支}}/README.md
- issue 列表：https://api.github.com/repos/{{owner}}/{{repo}}/issues?state=open&per_page=100
- issue 详情/评论：…/issues/{{编号}} 与 …/issues/{{编号}}/comments
- api.github.com 的凭据由平台自动附加（若配置了 token）；未配置时限额 60 次/小时，珍惜调用。

## 用户画像（评估「与用户的匹配度」时使用）
- 背景：{s.user_profile}
- 技能：{s.user_skills}

## 输出约定
- 结论先行（值不值得投入 / 适不适合上手），再给证据：引用真实抓到的路径、代码片段、issue 号、数据。
- 精炼的结构化中文 Markdown；抓取失败或信息不足时如实说明，不要猜测填充。
- 当任务是分析某个具体项目时，报告最后附一个 ```json 代码块给出六维评分（0-100 整数 + 一句话理由），
  键固定为：enterprise_potential（企业落地潜力）、match（与用户画像匹配度）、learning_value（学习价值）、
  star_momentum（star 增势，可由 star 数与仓库年龄估算）、activity（活跃度）、maintenance（维护健康度）。
  格式：{{"维度键": {{"score": 78, "reason": "…"}}}}。平台会解析它并算入榜单总分。
"""


def scenario_prompt() -> None:
    import app.config as config_mod

    stub = types.SimpleNamespace(user_profile="PROFILE-X", user_skills="SKILLS-Y")
    orig = config_mod.get_settings
    config_mod.get_settings = lambda: stub  # _system_prompt 调用时才 from ..config import，patch 生效
    try:
        baseline = _legacy_system_prompt(stub)
        off = _system_prompt(False)
        check("提示词: False 与 1.1 基线逐字一致", off == baseline)
        check("提示词: 默认参等价 False", _system_prompt() == baseline)
        on = _system_prompt(True)
        check("提示词: True 与 False 输出不同", on != baseline)
        check("提示词: True 含「## 向用户提问」段", "## 向用户提问" in on)
        check("提示词: True「环境与能力」如实列出六件（含 AskUserQuestion）",
              "AskUserQuestion" in on.split("## 向用户提问")[0] and "工具六件" in on)
        check("提示词: True 含何时该问约束（依赖用户偏好/授权的分叉）",
              "何时该问" in on and "用户偏好" in on and "授权" in on)
        check("提示词: True 含何时不该问约束（可查资料不问）",
              "不许问" in on and "查资料" in on)
        check("提示词: True 其余段落原样保留（手册/画像/输出约定）",
              "## 平台工具手册" in on and "## GitHub 数据获取手册" in on
              and "PROFILE-X" in on and "SKILLS-Y" in on and "## 输出约定" in on)
        check("提示词: False 无提问段、无 AskUserQuestion 字样",
              "向用户提问" not in off and "AskUserQuestion" not in off and "工具四个" in off)
    finally:
        config_mod.get_settings = orig


# ── 汇点接线：run_prompt / run_skill 补传 task_id + interactive=True ─────────


def scenario_run_prompt_wiring() -> None:
    captured: dict = {}

    async def fake_run(prompt, progress, log=None, timeout=3600, max_turns=60,
                       system_prompt=None, max_tokens=None, task_id=None,
                       interactive=False):
        captured.clear()
        captured.update(prompt=prompt, progress=progress, log=log, timeout=timeout,
                        task_id=task_id, interactive=interactive)
        return {"result": "ok", "cost_usd": 0.0, "duration_ms": 1, "num_turns": 0}

    def progress_cb(msg: str) -> None:
        pass

    logs: list[str] = []

    def log_cb(msg: str) -> None:
        logs.append(msg)

    def boom_resolve(name, args):
        raise AssertionError("自由文本不应触发技能解析")

    orig_run, orig_resolve = agent_sdk.run, skill_runner.resolve_skill
    agent_sdk.run = fake_run
    skill_runner.resolve_skill = boom_resolve
    try:
        # 自由文本（AI 命令栏形状）：不解析技能，原样透传
        result = asyncio.run(skill_runner.run_prompt(
            "  分析 fastapi 的优缺点  ", "tid-freetext", progress_cb, log=log_cb, timeout=77))
        check("汇点: 自由文本 run 收到 task_id", captured["task_id"] == "tid-freetext")
        check("汇点: 自由文本 run 收到 interactive=True", captured["interactive"] is True)
        check("汇点: 自由文本 prompt 原样（仅 strip）", captured["prompt"] == "分析 fastapi 的优缺点")
        check("汇点: timeout/log/progress 透传不变",
              captured["timeout"] == 77 and captured["log"] is log_cb
              and captured["progress"] is progress_cb)
        check("汇点: 返回值原样上抛", result == {"result": "ok", "cost_usd": 0.0,
                                                "duration_ms": 1, "num_turns": 0})

        # /技能 形状（技能 invoke）：解析后正文传给 run
        def fake_resolve(name, args):
            return f"SKILL[{name}] ARGS[{args}]"

        skill_runner.resolve_skill = fake_resolve
        asyncio.run(skill_runner.run_prompt("/tech 462", "tid-skill", progress_cb, log=log_cb))
        check("汇点: /技能 run 收到 task_id 与 interactive=True",
              captured["task_id"] == "tid-skill" and captured["interactive"] is True)
        check("汇点: /技能 传的是解析后的技能正文",
              captured["prompt"] == "SKILL[tech] ARGS[462]")
        check("汇点: 技能解析有时间线留痕", any("解析技能 /tech" in m for m in logs))

        # run_skill（技能 invoke 入口）→ run_prompt → run 同样接线
        asyncio.run(skill_runner.run_skill("ping", "hello", "tid-rskill", progress_cb))
        check("汇点: run_skill 经 run_prompt 同样收到 task_id + interactive=True",
              captured["task_id"] == "tid-rskill" and captured["interactive"] is True
              and captured["prompt"] == "SKILL[ping] ARGS[hello]")
    finally:
        agent_sdk.run = orig_run
        skill_runner.resolve_skill = orig_resolve


# ── run()→Engine 组装与超时/取消路径：stub Engine captor（确定性、无网络） ───


def scenario_run_assembly_and_abort() -> None:
    """直查 run() 给 Engine 的构造 kwargs 与中止路径。

    stub Engine 记录构造参数；submit() 阻塞至 abort() 置位后按 AbortedError 退出——
    阻塞必触发超时/取消路径，全程不碰网络。get_settings 打桩为 llm_configured=True
    （沿用 scenario_prompt 的 patch 手法，调用时才 from ..config import）。
    """
    import app.config as config_mod

    settings_stub = types.SimpleNamespace(
        llm_configured=True, llm_api_key="stub", llm_base_url="http://stub",
        llm_model="stub", user_profile="PROFILE-X", user_skills="SKILLS-Y")
    order: list = []
    engines: list = []

    class StubEngine:
        """captor：记录构造 kwargs；submit 阻塞到 abort 后抛 AbortedError（镜像真引擎中止语义）。"""

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._stop = threading.Event()
            engines.append(self)

        def abort(self):
            order.append(("engine.abort",))
            self._stop.set()

        def last_assistant_text(self):
            return ""

        def submit(self, prompt):
            while not self._stop.is_set():
                time.sleep(0.02)
            raise AbortedError()
            yield  # noqa: B012 不可达 yield：仅使本函数成为生成器，阻塞语义靠上面的循环+异常

    orig_settings = config_mod.get_settings
    orig_engine = agent_sdk.Engine
    orig_abort_bridge = askuser.abort_bridge

    def spy_abort_bridge(task_id):
        order.append(("abort_bridge", task_id))
        orig_abort_bridge(task_id)

    config_mod.get_settings = lambda: settings_stub
    agent_sdk.Engine = StubEngine
    askuser.abort_bridge = spy_abort_bridge
    try:
        # ① interactive=True + task_id：Engine 收到六件工具（末件 AskUser，注入正确）+ 提问段
        tid = "asktest-wiring-stub"
        log_cb = lambda msg: None  # noqa: E731
        t_before = time.monotonic()
        raised = None
        try:
            asyncio.run(agent_sdk.run("stub 指令", lambda m: None, log=log_cb,
                                      timeout=1, task_id=tid, interactive=True))
        except RuntimeError as e:
            raised = e
        t_after = time.monotonic()
        eng = engines[-1]
        tools = eng.kwargs["tools"]
        check("组装: interactive=True+task_id Engine 收到六件、末件 AskUserQuestionTool",
              _names(tools) == FIVE_NAMES + ["AskUserQuestion"]
              and isinstance(tools[-1], AskUserQuestionTool)
              and tools[-1]._task_id == tid)
        check("组装: AskUser 的 log_cb 即 run 的 emit（log 优先）", tools[-1]._log is log_cb)
        check("组装: deadline≈起点+timeout（run 起点算）",
              abs(tools[-1]._deadline_at - (t_before + 1)) <= 1.0
              and t_before + 1 <= tools[-1]._deadline_at <= t_after + 1)
        check("组装: 默认 system_prompt 含「## 向用户提问」段",
              "## 向用户提问" in eng.kwargs["system_prompt"])
        check("超时(stub): RuntimeError 照旧抛出（含超时文案）",
              isinstance(raised, RuntimeError) and "超时" in str(raised))
        check("超时(stub): abort_bridge(task_id) 在 engine.abort() 之后",
              ("engine.abort",) in order and ("abort_bridge", tid) in order
              and order.index(("engine.abort",)) < order.index(("abort_bridge", tid)))

        # ② interactive=False（scaffold 调用形状）：五件工具、无提问段、无 task_id 则不叫桥
        order.clear()
        raised2 = None
        try:
            asyncio.run(agent_sdk.run("stub 指令", lambda m: None,
                                      system_prompt="CUSTOM SYSTEM", timeout=1))
        except RuntimeError as e:
            raised2 = e
        eng2 = engines[-1]
        check("组装: 非交互五件且无 AskUserQuestionTool",
              _names(eng2.kwargs["tools"]) == FIVE_NAMES
              and not any(isinstance(t, AskUserQuestionTool) for t in eng2.kwargs["tools"]))
        check("组装: 自定义 system_prompt 原样透传（提问段不追加）",
              eng2.kwargs["system_prompt"] == "CUSTOM SYSTEM")
        check("超时(stub): task_id 为空不调 abort_bridge（仅 engine.abort）",
              order == [("engine.abort",)] and isinstance(raised2, RuntimeError))

        # ②b 非交互默认提示词：Engine 收到的 system_prompt 无提问段（旗标与挂载同条件）
        try:
            asyncio.run(agent_sdk.run("stub 指令", lambda m: None, timeout=1))
        except RuntimeError:
            pass
        eng2b = engines[-1]
        check("组装: 非交互默认提示词无「## 向用户提问」段",
              "## 向用户提问" not in eng2b.kwargs["system_prompt"]
              and _names(eng2b.kwargs["tools"]) == FIVE_NAMES)

        # ③ 取消路径：CancelledError 重抛，engine.abort 后同样叫醒问询
        order.clear()
        tid3 = "asktest-wiring-stub-cancel"

        async def _cancel_case():
            task = asyncio.create_task(agent_sdk.run(
                "stub 取消", lambda m: None, timeout=30, task_id=tid3, interactive=True))
            await asyncio.sleep(0.3)  # 让 run 进入 wait_for（worker 阻塞在 submit）
            task.cancel()
            try:
                await task
                return False
            except asyncio.CancelledError:
                return True

        check("取消(stub): CancelledError 重抛", asyncio.run(_cancel_case()) is True)
        check("取消(stub): abort_bridge(task_id) 在 engine.abort() 之后",
              ("engine.abort",) in order and ("abort_bridge", tid3) in order
              and order.index(("engine.abort",)) < order.index(("abort_bridge", tid3)))
    finally:
        config_mod.get_settings = orig_settings
        agent_sdk.Engine = orig_engine
        askuser.abort_bridge = orig_abort_bridge


# ── 真 LLM 超时组（可选附加：不进 FAIL，未触发即 SKIP 注记）──────────────────


def scenario_timeout_real() -> None:
    if not get_settings().llm_configured:
        skip("真 LLM 超时组：LLM 未配置（.env 的 LLM_BASE_URL / LLM_API_KEY），3 条断言跳过——确定性覆盖见 stub 组")
        return
    order: list = []

    async def _inner() -> Exception | None:
        orig_abort_bridge = askuser.abort_bridge
        orig_engine_abort = agent_sdk.Engine.abort

        def spy_abort_bridge(task_id):
            order.append(("abort_bridge", task_id))
            orig_abort_bridge(task_id)

        def spy_engine_abort(self):
            order.append(("engine.abort",))
            orig_engine_abort(self)

        askuser.abort_bridge = spy_abort_bridge
        agent_sdk.Engine.abort = spy_engine_abort
        try:
            try:
                await agent_sdk.run("从 1 数到 100，逐行输出", lambda m: None, timeout=2,
                                    task_id="asktest-wiring-timeout", interactive=True)
            except RuntimeError as e:
                return e
            return None
        finally:
            askuser.abort_bridge = orig_abort_bridge
            agent_sdk.Engine.abort = orig_engine_abort

    raised = asyncio.run(_inner())
    if raised is None:
        skip("真 LLM 超时组：端点快于 2s 先完成（未触发超时路径），3 条断言跳过——确定性覆盖见 stub 组")
        return
    tid = "asktest-wiring-timeout"
    check("超时(真LLM): RuntimeError 照旧抛出（含超时文案）",
          "超时" in str(raised))
    check("超时(真LLM): abort_bridge(task_id) 被调", ("abort_bridge", tid) in order)
    check("超时(真LLM): abort_bridge 在 engine.abort() 之后",
          ("engine.abort",) in order
          and order.index(("engine.abort",)) < order.index(("abort_bridge", tid)))


# ── scaffold 回归：run 签名默认参 + scaffold_builder 调用形状零改动 ──────────


def scenario_scaffold_regression() -> None:
    params = inspect.signature(agent_sdk.run).parameters
    check("scaffold: 既有参数与默认值不回归",
          params["timeout"].default == 3600 and params["max_turns"].default == 60
          and params["system_prompt"].default is None and params["max_tokens"].default is None)
    check("scaffold: 新参默认无人值守（task_id=None / interactive=False）",
          params["task_id"].default is None and params["interactive"].default is False)
    src = (ROOT / "app" / "services" / "scaffold_builder.py").read_text(encoding="utf-8")
    check("scaffold: scaffold_builder 仅一处直调 run 且整文件不出现 interactive",
          src.count("agent_sdk.run(") == 1 and "interactive" not in src
          and "system_prompt=SCAFFOLD_SYSTEM" in src)
    check("scaffold: 非交互工具组装即原五件（其 Engine 构造等价现状）",
          _names(_build_tools(None, None, 0.0, False)) == FIVE_NAMES)


def main() -> None:
    scenario_mounting()
    scenario_prompt()
    scenario_run_prompt_wiring()
    scenario_run_assembly_and_abort()
    scenario_timeout_real()
    scenario_scaffold_regression()
    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 条 SKIP 注记")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
