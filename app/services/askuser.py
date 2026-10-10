"""AskUserQuestion 工具与跨线程问题桥（「执行中可问可答」的引擎侧核心）。

范式：可见性走 DB，唤醒走 Event——pending_question + waiting 只服务 UI 与留痕；
答案向工具的传递只走进程内桥（Event + latch 答案槽），绝不经 DB 回读。

本模块只做引擎侧核心，接线由后续故事消费：
- 1.2 run()：构造 AskUserQuestionTool(task_id, log_cb, deadline_at) 挂载（interactive 运行），
  超时/异常路径在 engine.abort() 后调 abort_bridge(task_id) 叫醒阻塞中的问询
- 1.3 应答端点：get_bridge() 查表（无桥 409）→ 校验 pending_question.id → deliver() 写槽
  （返回 False = 已 latch/已关闭/已中止/已过期/kind 非法，转 409——超时后迟到的答案不误报 200）

不 import vendor 的 AskUserQuestionTool（prompt_toolkit TTY 阻塞，无头线程永久挂起），
仅参照其 schema 与返回文案；本工具 is_read_only()=False——与 vendor 版相反，
正是为借引擎「非只读工具独立成批串行单跑」机制保证同任务同时至多一个问题。
"""
import threading
import time
import uuid
from collections.abc import Callable

from sqlalchemy import update

from core.tool import Tool, ToolResult

from ..db import SessionLocal
from ..models import TaskRun
from ..tasks import _now

WAIT_SLICE = 0.5  # Event.wait 分片（秒）：既轮询 deadline，也让 abort 置位后最多一片内退出

_KIND_ANSWERED = "answered"
_KIND_CANCELLED = "cancelled"

_CANCEL_TEXT = "User cancelled the question."
_TIMEOUT_TEXT = "User did not answer in time; the question timed out."


# ── 桥与注册表 ────────────────────────────────────────────────────────────────


class Bridge:
    """跨线程问答桥：Event 唤醒 + 答案槽（latch 首写生效）+ deadline + 关闭标志。

    槽存元组 (kind, answers)：kind ∈ {answered, cancelled}；投递函数返回 bool，
    False = 已 latch/已关闭/已中止/已过期/kind 非法（调用方据此转 409——尤其超时后
    迟到的答案不得误报 200）。cancel/answer 锁内先到者生效；超时/中止退出即关闭。
    """

    def __init__(self, task_id: str, deadline_at: float) -> None:
        self.task_id = task_id
        self.deadline_at = deadline_at  # time.monotonic 基的绝对时刻（run() 注入，必传）
        self.event = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._result: tuple[str, list | None] | None = None

    @property
    def result(self) -> tuple[str, list | None] | None:
        return self._result

    def deliver(self, kind: str, answers: list | None = None) -> bool:
        """投递答案或取消；latch 首写生效，再投递被拒且不覆盖首写结果。"""
        with self._lock:
            if self._closed or self._result is not None:
                return False
            if kind not in (_KIND_ANSWERED, _KIND_CANCELLED):
                return False
            if time.monotonic() >= self.deadline_at:  # 已过期：顺手关闭，wait 醒来即超时
                self._closed = True
                self.event.set()
                return False
            self._result = (kind, answers)
            self.event.set()
            return True

    def abort(self) -> None:
        """关闭并唤醒（与投递竞态时，已 latch 的结果先到者生效）。"""
        with self._lock:
            self._closed = True
        self.event.set()

    def wait(self) -> tuple[str, list | None] | None:
        """阻塞等待：0.5s 分片轮询 deadline；Event 置位即醒。

        返回 (kind, answers)；None = 超时/中止（deadline 到期或 abort_bridge 置位），
        两条路共用 timed-out 文案，退出前经 _resolve 关闭桥（此后投递一律被拒）。
        """
        while True:
            remaining = self.deadline_at - time.monotonic()
            if remaining <= 0:
                return self._resolve()
            if self.event.wait(min(WAIT_SLICE, remaining)):
                return self._resolve()

    def _resolve(self) -> tuple[str, list | None] | None:
        """锁内定夺：已 latch 返回结果（先到者生效）；否则关闭并返回 None。"""
        with self._lock:
            if self._result is not None:
                return self._result
            self._closed = True
            return None


_REGISTRY: dict[str, Bridge] = {}
_REGISTRY_LOCK = threading.Lock()


def get_bridge(task_id: str) -> Bridge | None:
    """查注册表（1.3 应答端点用：无桥即 409）。"""
    with _REGISTRY_LOCK:
        return _REGISTRY.get(task_id)


def _unregister(task_id: str, bridge: Bridge) -> None:
    with _REGISTRY_LOCK:
        if _REGISTRY.get(task_id) is bridge:  # 只注销自己那座，防串号
            del _REGISTRY[task_id]


def abort_bridge(task_id: str) -> None:
    """run() 超时/异常路径的叫醒通道：关闭桥（拒后续投递）+ Event.set()。

    无桥 task_id 静默返回（幂等）——abort 只关 HTTP 流叫不醒 Event.wait，
    必须走这里；阻塞中的 wait 立即退出，走 timed-out 文案并清场。
    """
    bridge = get_bridge(task_id)
    if bridge is not None:
        bridge.abort()


# ── DB 读写（沿用 tasks.py 模式：每调用独立 SessionLocal、JSON 列整体重赋值） ──


def _mark_waiting(task_id: str, pending: dict) -> None:
    """发起提问的 DB 写：running→waiting + pending_question 入 payload。

    条件更新（WHERE status='running'）：任务若已被 run() 写成终态（超时抢先一步），
    这里落空即抛错走异常路，不把终态改回 waiting。
    """
    with SessionLocal() as s:
        row = s.get(TaskRun, task_id)
        if row is None:
            raise RuntimeError(f"task {task_id} not found")
        payload = dict(row.payload or {})
        payload["pending_question"] = pending
        res = s.execute(
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "running")
            .values(status="waiting", payload=payload)
            .execution_options(synchronize_session=False)
        )
        if not res.rowcount:
            raise RuntimeError(f"task {task_id} is no longer running; question dropped")
        s.commit()


def _restore_running(task_id: str) -> None:
    """条件清场：waiting→running；条件未命中（终态）不动 status，但仍清 pending_question。

    AD-1：终态（success/failed）不可被工具覆盖——条件更新天然空操作；
    但残留的 pending_question 会让 UI 挂僵尸问题，故兜底仅清键不改状态。
    清键在同一事务内整体重赋值（JSON 列不留脏键）。
    """
    with SessionLocal() as s:
        s.execute(
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "waiting")
            .values(status="running")
            .execution_options(synchronize_session=False)
        )
        row = s.get(TaskRun, task_id)
        if row is not None:
            payload = dict(row.payload or {})
            if payload.pop("pending_question", None) is not None:
                row.payload = payload
        s.commit()


# ── 工具 ─────────────────────────────────────────────────────────────────────


def _validate_questions(questions) -> str | None:
    """参数校验：不合法直接返回错误文案（不等待、不写 DB、不注册）。"""
    if not isinstance(questions, list) or not questions:
        return "questions must be a non-empty array of 1-4 questions."
    if len(questions) > 4:
        return "questions must contain at most 4 questions."
    seen: set[str] = set()  # 重复 question 文本会让 _format_answered 的按题匹配折叠出同答案
    for q in questions:
        if not isinstance(q, dict):
            return "each question must be an object."
        text = q.get("question")
        if not isinstance(text, str) or not text.strip():
            return "each question must have a non-empty 'question' string."
        options = q.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            return "each question must have 2-4 options."
        for o in options:
            if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o.get("label", "").strip():
                return "each option must have a non-empty 'label' string."
            desc = o.get("description")  # schema 声明必填，校验器须一致
            if not isinstance(desc, str) or not desc.strip():
                return "each option must have a non-empty 'description' string."
        if text.strip() in seen:
            return "question texts must be unique within one call."
        seen.add(text.strip())
    return None


def _normalize_questions(questions) -> list[dict]:
    """整理成 pending_question 的 questions 形状（multiSelect 缺省 false）。"""
    out = []
    for q in questions:
        multi = q.get("multiSelect", False)
        out.append({
            "question": str(q.get("question", "")),
            "options": [
                {"label": str(o.get("label", "")), "description": str(o.get("description", ""))}
                for o in q.get("options", [])
            ],
            "multiSelect": multi if isinstance(multi, bool) else False,
        })
    return out


def _format_answered(questions: list[dict], answers) -> str:
    """`User answered:\\n{q} => {a}`（vendor 同款文案；多问按提问顺序逐行）。

    answers 是端点投递的 [{"question", "answer"}]：优先按题目文本匹配（乱序也稳），
    位置兜底；answer 恒字符串（multiSelect 由前端拼接）。
    """
    entries = list(answers or [])
    by_question = {e.get("question"): e.get("answer", "") for e in entries if isinstance(e, dict)}
    lines = []
    for i, q in enumerate(questions):
        answer = by_question.get(q["question"])
        if answer is None and i < len(entries):
            answer = entries[i].get("answer", "") if isinstance(entries[i], dict) else str(entries[i])
        lines.append(f"{q['question']} => {answer if isinstance(answer, str) else ''}")
    return "User answered:\n" + "\n".join(lines)


class AskUserQuestionTool(Tool):
    """无头版 AskUserQuestion：经 DB 暴露问题、经进程内桥收答案。

    构造参数 (task_id, log_cb, deadline_at)：deadline 为 time.monotonic 基绝对时刻且**必传**
    （None 默认会无限等待，退出全押调用方记得 abort——不做），由 1.2 的 run() 按
    skill_run_timeout 剩余额度注入；log_cb 即任务时间线回调（留痕封顶随 tasks.py）。
    """

    def __init__(self, task_id: str, log_cb: Callable[[str], None] | None,
                 deadline_at: float) -> None:
        self._task_id = task_id
        self._log = log_cb if log_cb is not None else (lambda msg: None)
        self._deadline_at = deadline_at

    @property
    def name(self) -> str:
        return "AskUserQuestion"

    @property
    def description(self) -> str:
        return (
            "向用户提问并等待应答（执行中的人工分叉决策）。适用：\n"
            "1. 收集用户偏好或需求\n"
            "2. 澄清有歧义的指令\n"
            "3. 在实现方案间请用户拍板\n"
            "4. 就后续方向给出选项\n\n"
            "使用须知：\n"
            "- 仅用于依赖用户偏好/授权才能定的分叉；可查资料自行回答的不许问\n"
            "- 一次 1-4 个问题，每问 2-4 个选项（label + description）\n"
            "- 用户永远可以自由输入（Other）；multiSelect: true 允许多选\n"
            "- 推荐某选项时放第一位并在 label 末尾加 \"(Recommended)\"\n"
            "- 提问后任务转入 waiting，用户应答/取消或超时后恢复运行"
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label", "description"],
                                },
                                "minItems": 2,
                                "maxItems": 4,
                            },
                            "multiSelect": {"type": "boolean", "default": False},
                        },
                        "required": ["question", "options"],
                    },
                    "minItems": 1,
                    "maxItems": 4,
                }
            },
            "required": ["questions"],
        }

    def is_read_only(self) -> bool:
        # 与 vendor 版相反：False 借引擎「非只读工具独立成批串行单跑」，保证同任务单问
        return False

    def get_activity_description(self, **kwargs) -> str | None:
        questions = kwargs.get("questions")
        first = ""
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            first = str(questions[0].get("question", ""))
        first = " ".join(first.split())[:60]  # 单行截断：问句常带换行，会撑乱时间线
        return f"等待用户选择：{first}"

    def execute(self, **kwargs) -> ToolResult:
        questions = kwargs.get("questions")
        invalid = _validate_questions(questions)
        if invalid is not None:
            return ToolResult(content=invalid, is_error=True)  # 不写 DB、不注册

        if time.monotonic() >= self._deadline_at:
            # deadline 已过：不注册、不写 DB——否则留下转瞬即逝的幻影问题，
            # 模型重试还会连打 ❓ 把时间线闪成碎片
            return ToolResult(content=_TIMEOUT_TEXT, is_error=True)

        bridge = Bridge(self._task_id, self._deadline_at)
        with _REGISTRY_LOCK:
            if _REGISTRY.get(self._task_id) is not None:  # 双保险：同任务同时至多一问
                return ToolResult(
                    content="A question is already waiting for this task; only one question at a time.",
                    is_error=True,
                )
            _REGISTRY[self._task_id] = bridge
        result: ToolResult
        try:
            pending = {
                "id": uuid.uuid4().hex,  # 每问唯一，多轮问 = 多次调用各新 id
                "questions": _normalize_questions(questions),
                "asked_at": _now().isoformat(timespec="milliseconds"),
            }
            _mark_waiting(self._task_id, pending)
            preview = "；".join(q["question"] for q in pending["questions"])[:120]
            self._log(f"❓ 等待用户应答：{preview}")

            outcome = bridge.wait()
            if outcome is None:  # deadline 到期或 abort_bridge（共用超时文案）
                self._log("⏳ 等待应答超时/中止，继续执行")
                result = ToolResult(content=_TIMEOUT_TEXT, is_error=True)
            else:
                kind, answers = outcome
                if kind == _KIND_CANCELLED:
                    result = ToolResult(content=_CANCEL_TEXT, is_error=True)
                else:
                    result = ToolResult(content=_format_answered(pending["questions"], answers))
        except Exception as exc:
            # 引擎 _execute_tool 吞工具异常会跳过清场，故清场必须在 finally；
            # 这里先行转 is_error 返回，不上抛
            result = ToolResult(content=f"AskUserQuestion error: {exc}", is_error=True)
        finally:
            try:
                _restore_running(self._task_id)
            except Exception as cleanup_exc:
                # 清场 DB 写失败也不外抛（不上抛 Always），就地转错误文案；
                # 嵌套 finally 保证注销无条件执行——桥泄漏会卡死该任务后续所有提问
                result = ToolResult(content=f"AskUserQuestion error: cleanup failed: {cleanup_exc}",
                                    is_error=True)
            finally:
                _unregister(self._task_id, bridge)
        return result
