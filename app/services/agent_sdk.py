"""Agent SDK：vendored cc-mini 以库方式内嵌运行——无子进程、无 CLI、无本地文件概念。

分析对象是 GitHub 上的远程开源代码：模型通过 WebFetch 工具抓 api.github.com /
raw.githubusercontent.com / 项目官网，拿真实数据作答。云端部署只依赖本进程与网络。
"""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from core.engine import AbortedError, Engine
from core.permissions import PermissionChecker
from core.tool import Tool, ToolResult
from features.cost_tracker import CostTracker

from . import askuser

MAX_TURNS = 60  # API 轮次上限，防失控（课程生成一类任务工具调用多，40 不够用）

# 文件白名单：agent 只能碰产物子树。读=技能资产模板+课程+脚手架+教材页文本；写=课程产物+脚手架工作区。
# .env / 源码 / 数据库都在白名单外——LLM 拿不到凭据是底线。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
READ_ROOTS = [PROJECT_ROOT / ".claude" / "skills", PROJECT_ROOT / "courses", PROJECT_ROOT / "scaffolds",
              PROJECT_ROOT / "uploads"]
WRITE_ROOTS = [PROJECT_ROOT / "courses", PROJECT_ROOT / "scaffolds"]
TEXT_LOG_INTERVAL = 2.5  # 生成期进度上报的最小间隔（秒）；阈值只看时间不看字符数，
# 否则慢流（每块几十字符）永远够不到门槛，界面又冻住

# 引擎 yield 的事件（vendor/cc-mini/src/core/engine.py）：
#   ("text", chunk) / ("waiting",) / ("usage", usage) / ("error", msg)
#   ("tool_call", name, input, activity) / ("tool_executing", name, input, activity)
#   ("tool_result", name, input, ToolResult)
# 旧实现只认 text/tool_call/usage，工具完成态与 API 报错全丢——并行抓取时只剩「调用」没有「完成」，
# 重试/截断等异常也无处可见。


def _one_line(text: str, limit: int = 160) -> str:
    """进度行必须单行：工具报错里常带换行，直接塞进日志会撑乱时间线。"""
    return " ".join(str(text or "").split())[:limit]


def _brief(tool_input) -> str:
    """activity 缺失时的兜底描述（工具没实现 get_activity_description 时）。"""
    if isinstance(tool_input, dict):
        url = tool_input.get("url")
        if url:
            return str(url)
        return _one_line(json.dumps(tool_input, ensure_ascii=False), 100)
    return _one_line(tool_input, 100)


def _cum_tokens(cost: CostTracker) -> int:
    """累计 token（本轮及之前的输入+输出）。缓存命中数会随轮次重复累计，故不计入。"""
    return sum((u.input_tokens or 0) + (u.output_tokens or 0) for u in cost._model_usage.values())


class WebFetchTool(Tool):
    """只读网页抓取：GitHub API / raw 源码 / 文档页面。进程内执行，无文件系统与 Shell。"""

    MAX_CHARS = 50_000  # 单次抓取上限，防止单页撑爆上下文

    @property
    def name(self) -> str:
        return "WebFetch"

    @property
    def description(self) -> str:
        return ("HTTP GET 抓取一个 URL 并返回文本内容：GitHub API 返回 JSON 原文，"
                "HTML 页面剥壳为纯文本，raw.githubusercontent.com 返回源码原文。只读安全。")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的完整 URL（https:// 开头）"},
            },
            "required": ["url"],
        }

    def get_activity_description(self, url: str = "", **_) -> str:
        return f"抓取 {url[:80]}"

    def is_read_only(self) -> bool:
        return True

    def execute(self, url: str = "", **_) -> ToolResult:
        if not url.startswith(("https://", "http://")):
            return ToolResult("url 必须以 http(s):// 开头", is_error=True)
        headers = {"User-Agent": "celestial-snow-agent"}
        if url.startswith("https://api.github.com"):
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            from ..config import get_settings

            token = get_settings().github_token
            if token:  # 提额：60 次/小时 → 5000 次/小时
                headers["Authorization"] = f"Bearer {token}"
        try:
            resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
        except httpx.HTTPError as e:
            return ToolResult(f"抓取失败：{e}", is_error=True)
        if resp.status_code >= 400:
            return ToolResult(f"HTTP {resp.status_code}：{resp.text[:300]}", is_error=True)
        return ToolResult(self._extract(resp))

    @staticmethod
    def _extract(resp: httpx.Response) -> str:
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype:
            text = BeautifulSoup(resp.text, "html.parser").get_text(separator="\n")
            text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
        else:
            text = resp.text
        if len(text) > WebFetchTool.MAX_CHARS:
            text = text[:WebFetchTool.MAX_CHARS] + f"\n…（截断，原文共 {len(resp.text)} 字符）"
        return text


def _resolve_under(path: str, roots: list[Path]) -> Path | None:
    """把模型给的路径解析到 roots 之一下面；越界（含 ../ 穿越、绝对路径指到外面）返回 None。

    Windows 大小写不敏感，比较前 normcase 归一。resolve() 会展开符号链接与 ..。
    """
    if not path or not isinstance(path, str):
        return None
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        p = p.resolve()
    except OSError:
        return None
    for root in roots:
        if os.path.normcase(str(p)) == os.path.normcase(str(root.resolve())):
            return p  # 恰好是根目录本身（ListDir 列 courses/ 要放行）
        if os.path.normcase(str(p)).startswith(os.path.normcase(str(root.resolve())) + os.sep):
            return p
    return None


class ReadFileTool(Tool):
    """读白名单内的文本文件：技能资产模板（.claude/skills/**）与已生成课程（courses/**）。"""

    @property
    def name(self) -> str:
        return "ReadFile"

    @property
    def description(self) -> str:
        return ("读取一个文本文件的内容（带行号）。只能读 .claude/skills/、courses/、scaffolds/ 与"
                " uploads/（教材页文本）下的文件；路径可写相对项目根的"
                "（如 .claude/skills/tech/assets/quiz.js）或绝对路径。")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "起始行（0 起，可省）"},
                "limit": {"type": "integer", "description": "最多读多少行（默认 2000）"},
            },
            "required": ["path"],
        }

    def get_activity_description(self, path: str = "", **_) -> str:
        return f"读取 {path[:100]}" if path else None

    def is_read_only(self) -> bool:
        return True

    def execute(self, path: str = "", offset: int = 0, limit: int = 2000, **_) -> ToolResult:
        p = _resolve_under(path, READ_ROOTS)
        if p is None:
            return ToolResult(f"拒绝：{path} 不在可读范围（.claude/skills/、courses/ 或 uploads/）", is_error=True)
        if not p.exists():
            return ToolResult(f"文件不存在：{path}", is_error=True)
        if not p.is_file():
            return ToolResult(f"不是文件（目录请用 ListDir）：{path}", is_error=True)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(f"读取失败：{e}", is_error=True)
        lines = text.splitlines(keepends=True)
        sliced = lines[offset:offset + limit]
        numbered = "".join(f"{offset + i + 1}\t{ln}" for i, ln in enumerate(sliced))
        if len(lines) > offset + limit:
            numbered += f"\n…（共 {len(lines)} 行，还有 {len(lines) - offset - limit} 行）"
        return ToolResult(numbered or "（空文件）")


class ListDirTool(Tool):
    """列目录：核对课程文件是否齐全（替代本地 ls）。"""

    @property
    def name(self) -> str:
        return "ListDir"

    @property
    def description(self) -> str:
        return ("列出一个目录下的直接子项（文件/目录名）。只能看 .claude/skills/ 与 courses/ 下的目录。")

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string", "description": "目录路径"}}, "required": ["path"]}

    def get_activity_description(self, path: str = "", **_) -> str:
        return f"列目录 {path[:100]}" if path else None

    def is_read_only(self) -> bool:
        return True

    def execute(self, path: str = "", **_) -> ToolResult:
        p = _resolve_under(path, READ_ROOTS)
        if p is None:
            return ToolResult(f"拒绝：{path} 不在可列范围", is_error=True)
        if not p.is_dir():
            return ToolResult(f"不是目录：{path}", is_error=True)
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        out = "\n".join(f"{'📁' if x.is_dir() else '📄'} {x.name}" for x in items)
        return ToolResult(out or "（空目录）")


class WriteFileTool(Tool):
    """写产物文件：只能写 courses/ 与 scaffolds/ 下，父目录自动创建，覆盖式（重生成幂等）。"""

    @property
    def name(self) -> str:
        return "WriteFile"

    @property
    def description(self) -> str:
        return ("写入一个文件（覆盖已有内容）。**只能写 courses/ 或 scaffolds/ 目录下**（产物区）；"
                "父目录不存在会自动创建。content 必须是完整文件内容。")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，如 courses/3/index.html"},
                "content": {"type": "string", "description": "完整文件内容（UTF-8 文本）"},
            },
            "required": ["path", "content"],
        }

    def get_activity_description(self, path: str = "", **_) -> str:
        return f"写 {path[:100]}" if path else None

    def is_read_only(self) -> bool:
        return False

    def execute(self, path: str = "", content: str = "", **_) -> ToolResult:
        p = _resolve_under(path, WRITE_ROOTS)
        if p is None:
            return ToolResult(f"拒绝：{path} 不在可写范围（只能写 courses/ 或 scaffolds/ 下）", is_error=True)
        if not content and not isinstance(content, str):
            return ToolResult("content 不能为空", is_error=True)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # newline=""：不做 \n→\r\n 转换，写进去什么就是什么（资产原样复制才逐字节可信）
            p.write_text(content, encoding="utf-8", newline="")
        except OSError as e:
            return ToolResult(f"写入失败：{e}", is_error=True)
        n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(f"已写入 {path}（{n} 行，{len(content.encode('utf-8'))} 字节）")


class PlatformApiTool(Tool):
    """调用平台自身的 REST API：注册课程、取学习上下文、发布课程副本等都走它（替代本地 curl）。"""

    @property
    def name(self) -> str:
        return "PlatformAPI"

    @property
    def description(self) -> str:
        return ("调用本平台（celestial-snow）自己的 HTTP API。GET 取数据；POST 提交 JSON（如注册课程 "
                "POST /api/courses、发布课程 POST /api/courses/{id}/publish）。path 必须以 /api/ 开头，"
                "返回 JSON 原文。")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST"], "description": "HTTP 方法"},
                "path": {"type": "string", "description": "API 路径，如 /api/learning-context/462"},
                "body": {"type": "object", "description": "POST 的 JSON 请求体（可省）"},
            },
            "required": ["method", "path"],
        }

    def get_activity_description(self, method: str = "", path: str = "", **_) -> str:
        return f"{method} {path[:100]}" if path else None

    def is_read_only(self) -> bool:
        return False  # 能 POST，按可变更工具对待

    def execute(self, method: str = "GET", path: str = "", body: dict | None = None, **_) -> ToolResult:
        from ..config import get_settings

        if not path.startswith("/api/"):
            return ToolResult("拒绝：只能调用平台自身的 /api/ 路径", is_error=True)
        base = get_settings().platform_api_base.rstrip("/")
        try:
            if method.upper() == "POST":
                resp = httpx.post(f"{base}{path}", json=body or {}, timeout=120)
            else:
                resp = httpx.get(f"{base}{path}", timeout=60)
        except httpx.HTTPError as e:
            return ToolResult(f"调用失败：{e}", is_error=True)
        text = resp.text
        if len(text) > WebFetchTool.MAX_CHARS:  # 与 WebFetch 同一上限：learning-context 带 README 全文
            text = text[:WebFetchTool.MAX_CHARS] + f"\n…（截断，原文共 {len(resp.text)} 字符）"
        head = f"HTTP {resp.status_code}\n"
        return ToolResult(head + text, is_error=resp.status_code >= 400)


def _build_tools(task_id: str | None, log_cb, deadline_at: float,
                 interactive: bool = False) -> list[Tool]:
    """run() 与测试共用的工具组装：基础五件 + interactive 追加 AskUserQuestion。

    interactive=True 且 task_id 非空才挂问询工具（无任务行可写时不挂、不报错）；
    deadline_at 由 run() 按 timeout 在起点算好注入——问询等待与工具执行共享同一额度。
    """
    tools: list[Tool] = [WebFetchTool(), PlatformApiTool(), ReadFileTool(), ListDirTool(), WriteFileTool()]
    if interactive and task_id:
        tools.append(askuser.AskUserQuestionTool(task_id, log_cb, deadline_at))
    return tools


def _system_prompt(interactive: bool = False) -> str:
    """云端分析 agent 的身份与操作手册（替代本地 CLAUDE.md 概念）。

    interactive=True（技能 invoke / AI 命令栏等有人值守运行）时「环境与能力」如实列出
    AskUserQuestion，并追加「向用户提问」段（何时该问/用法）；False 与历史版本逐字一致
    （scaffold 走覆盖式 system_prompt，本就不受这里影响）。
    """
    from ..config import get_settings

    s = get_settings()
    tools_line = (
        "- 你运行在云服务进程内，没有 Shell。工具六件：WebFetch（抓远程网页/GitHub）、PlatformAPI（调本平台 API）、\n"
        "  ReadFile（读 .claude/skills/、courses/、scaffolds/、uploads/ 下文件）、ListDir（列这些目录核对产物）、"
        "WriteFile（只能写 courses/ 或 scaffolds/ 下）、\n"
        "  AskUserQuestion（向用户提问并等待应答，用法见下方「向用户提问」段）。"
        if interactive else
        "- 你运行在云服务进程内，没有 Shell。工具四个：WebFetch（抓远程网页/GitHub）、PlatformAPI（调本平台 API）、\n"
        "  ReadFile/ListDir（读 .claude/skills/ 技能资产与 courses/、scaffolds/、uploads/ 产物文件）、"
        "WriteFile（只能写 courses/ 或 scaffolds/ 下）。"
    )
    ask_section = (
        "\n## 向用户提问\n"
        "- 何时该问：只有依赖用户偏好或授权才能定的分叉（如实现方案取舍、有歧义指令的澄清、是否继续推进），"
        "才用 AskUserQuestion 提问——一次 1-4 个问题、每问 2-4 个选项（label + description），"
        "推荐选项放第一位并在 label 末尾加 \"(Recommended)\"；用户永远可以自由输入（Other），multiSelect: true 允许多选。\n"
        "- 何时不该问：凡是能靠工具查资料回答的（WebFetch/PlatformAPI/ReadFile），自己查、不许问用户；"
        "也不要为同一件事反复提问。\n"
        "- 提问后任务转入 waiting，用户应答、取消或超时后自动恢复运行，答案作为继续执行的依据。\n"
        if interactive else ""
    )
    return f"""你是 celestial-snow 平台内置的开源项目分析 agent，运行在云服务进程内。

## 环境与能力
{tools_line}
- 分析对象是 GitHub 上的开源项目代码与 issue：一切信息通过网络获取，禁止凭空编造。
{ask_section}
## 平台工具手册（生成课程一类任务用）
- 平台 API 一律走 PlatformAPI，不要用 WebFetch 打本机地址：取学习上下文
  GET /api/learning-context/{{issue_id}}（issue 课）/ /api/learning-context/repo/{{repo_id}}（项目课）/
  /api/learning-context/book/{{book_id}}（教材课）；注册课程 POST /api/courses（body 含
  source_type 与对应来源 id、title、lessons）；发布课程 POST /api/courses/{{id}}/publish（重生成后加 body {{"prune": true}}）。
- 课程文件写到 courses/{{course_id}}/ 下（WriteFile 只认这个范围）；assets/style.css 与 assets/quiz.js 等共享模板
  在 .claude/skills/tech/assets/，用 ReadFile 读出后原样 WriteFile 到课程目录，不要自己重写。
- 教材课的逐页文本在 uploads/books/{{book_id}}/text/pNNNN.txt（ReadFile 可读），N 是页码。
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


def warmup() -> None:
    """预先把重依赖与引擎构造走一遍（服务启动时在后台线程调用）。

    Engine 构造 + 依赖导入实测要 2s 左右，且是同步的——跑在事件循环上就把同一时刻的
    HTTP 响应一起卡住（提交任务后 POST 迟迟不回，前端面板要等 2s 才出现）。
    """
    cost = CostTracker()
    Engine(
        tools=_build_tools(None, None, 0.0),
        system_prompt="warmup",
        permission_checker=PermissionChecker(auto_approve=True),
        provider="openai",
        api_key="warmup",
        base_url="http://127.0.0.1:1",
        model="warmup",
        cost_tracker=cost,
    )


async def run(prompt: str, progress, log=None, timeout: int = 3600,
              max_turns: int = MAX_TURNS, system_prompt: str | None = None,
              max_tokens: int | None = None, task_id: str | None = None,
              interactive: bool = False) -> dict:
    """执行一次 agent。prompt 为自由指令或已解析好的技能正文。

    log 是追加式时间线回调（长任务用）；不传时退回 progress 的单行覆盖语义。
    system_prompt 不传时用默认的分析 agent 身份；任务型调用方（如脚手架生成）传自己的。
    max_tokens 不传时走 vendor 默认（openai 回落 8192）；生成类任务单轮输出大，传 16384+。
    interactive=True 且 task_id 非空时挂 AskUserQuestionTool 并启用提示词「向用户提问」段
    （技能 invoke / AI 命令栏等有人值守运行）；scaffold 等无人值守调用保持默认 False 不受影响。
    传自定义 system_prompt 时提问段不会被追加（整段被覆盖），但工具仍按 interactive/task_id 条件挂载；
    interactive=True 而 task_id 为空时，工具与提示词提问段都不启用（无任务行可写）。
    返回 {result, cost_usd, duration_ms, num_turns}；由调用方（任务层）落库。
    """
    from ..config import get_settings

    s = get_settings()
    if not s.llm_configured:
        raise RuntimeError("LLM 未配置（.env 的 LLM_BASE_URL / LLM_API_KEY），无法运行 agent")

    emit = log or progress
    t0 = time.monotonic()
    # AskUserQuestion 的等待额度与 run(timeout) 同源、run 起点算：问询等待与工具执行共享
    # 同一额度——任务级超时一到桥先过期，不留「run 已超时、问题还挂在界面上」的窗口
    deadline_at = t0 + timeout
    cost = CostTracker()
    engine = Engine(
        tools=_build_tools(task_id, emit, deadline_at, interactive),
        # 提示词旗标与工具挂载同条件：interactive 无 task_id 时工具不挂，提示词也不宣告（防调不存在的工具）
        system_prompt=system_prompt or _system_prompt(bool(interactive and task_id)),
        permission_checker=PermissionChecker(auto_approve=True),
        provider="openai",
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        model=s.llm_model,
        max_tokens=max_tokens,
        cost_tracker=cost,
    )

    def _drive() -> str:
        chunks: list[str] = []
        # 本轮生成状态：at=上次上报时刻（0 表示本轮还没报过），len=本轮已生成字符数
        text = {"at": 0.0, "len": 0}
        # 已宣告但还没出结果的工具数。引擎先批量宣告 tool_call，再逐个回 tool_result；
        # 一批回完就说明要发下一轮请求了——而「下一轮首字」实测能等 19s（模型思考），
        # 中间不写点什么，界面就是干等。
        pending = {"tools": 0}
        try:
            for event in engine.submit(prompt):
                kind = event[0]
                if kind == "text":
                    chunk = event[1]
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    text["len"] += len(chunk)
                    now = time.monotonic()
                    if text["at"] == 0.0:  # 本轮首块：先报一声，避免长生成期界面像卡死
                        emit("模型正在生成回复…")
                        text["at"] = now
                    elif now - text["at"] >= TEXT_LOG_INTERVAL:
                        emit(f"正在生成回复…（已 {text['len']} 字符）")
                        text["at"] = now
                elif kind == "tool_call":
                    pending["tools"] += 1
                    emit(f"调用 {event[1]}：{event[3] or _brief(event[2])}")
                elif kind == "tool_result":
                    pending["tools"] -= 1
                    result = event[3]
                    if getattr(result, "is_error", False):
                        emit(f"✗ {event[1]} 失败：{_one_line(result.content, 120)}")
                    else:
                        emit(f"✓ {event[1]} 完成（{len(result.content or '')} 字符）")
                    if pending["tools"] <= 0:
                        emit("工具结果已回传，等待模型继续…")
                elif kind == "usage":
                    state["turns"] += 1
                    text["at"], text["len"] = 0.0, 0  # 下一轮重新计数
                    emit(f"第 {state['turns']} 轮 · 累计 ${cost.total_cost_usd:.4f}"
                         f" / {_cum_tokens(cost)} tokens")
                    if state["turns"] >= max_turns:
                        emit(f"已达轮次上限 {max_turns}，主动中止")
                        engine.abort()
                        if task_id:  # 防御性：事件流里工具不可能正阻塞，但两处中止语义保持一致
                            askuser.abort_bridge(task_id)
                elif kind == "error":
                    emit(f"⚠️ {_one_line(event[1])}")
                # "tool_executing" / "waiting" 是调度细节，对用户没有信息量，忽略
        except AbortedError:
            pass  # 超时/轮次上限触发的主动中止，返回已有内容
        return engine.last_assistant_text() or "".join(chunks).strip()

    state = {"turns": 0}
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_drive), timeout=timeout)
    except asyncio.TimeoutError:
        emit(f"超时（>{timeout}s），主动中止")
        engine.abort()  # worker 线程下一个事件点自行退出
        if task_id:
            # abort 只关 HTTP 流叫不醒 Event.wait——必须叫醒阻塞中的问询（无桥时幂等静默）
            askuser.abort_bridge(task_id)
        raise RuntimeError(f"agent 执行超时（>{timeout}s），已中止") from None
    except asyncio.CancelledError:
        # 任务层取消/服务关停：镜像超时语义——不叫醒的话阻塞中的问询会泊到 deadline
        # （最长 skill_run_timeout，3600s），worker 线程同理
        engine.abort()
        if task_id:
            askuser.abort_bridge(task_id)
        raise

    return {
        "result": result,
        "cost_usd": cost.total_cost_usd,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "num_turns": state["turns"],
    }
