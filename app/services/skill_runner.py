"""技能发现与 prompt 解析；执行统一委托给 agent_sdk（in-process SDK，无子进程）。

技能（SKILL.md）是每次调用现读磁盘的——新增/修改 `.claude/skills/` 或
`~/.claude/skills/` 下的技能，下一次调用即生效，服务端无需重启。
frontmatter 里 `requires: local` 的技能依赖本地文件环境（如写课程文件），SDK 不支持。

参数表单：frontmatter 可写 `arguments: {单行 JSON}`，web 端据此把 args 文本框升级为结构化表单
（issue 下拉 / 单选 / 文本），把「执行中问用户」提前到「提交前选好」。词表见 _parse_arguments。
"""
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROJECT_SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
USER_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """SKILL.md 的 frontmatter 只有扁平 key: value，正则解析足够，不引 yaml 依赖。

    返回 (字段表, 正文)。描述若是 YAML 折叠/字面标量（>、|- 等），回退为正文第一个非空行。
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    fields: dict[str, str] = {}
    body = text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip().strip('"')
        body = m.group(2)
    if fields.get("description", "") in (">", ">-", "|", "|-", "|+", "+"):
        body_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        fields["description"] = body_line[:120]
    return fields, body


def _parse_arguments(raw: str) -> dict:
    """frontmatter 的 `arguments:` 值（单行 JSON）→ 参数表。坏 JSON 静默忽略，退回普通 args 文本框。

    词表（前端渲染依据，保持两端同步）：
      type: "text" 文本框 | "issue" issue 下拉（/api/issues）| "repo" 项目下拉（/api/repos）|
            "book" 教材下拉（/api/books）| "select" 单选
      label / placeholder / required
      visible_if: "existing_course"（所选 issue 已有课，/tech 的覆盖选择）|
                  "existing_repo_course"（所选项目已有项目课）| "existing_book_course"（所选教材已有教程）
    参数按声明顺序以空格拼进 args（空值跳过），技能正文照旧用 $ARGUMENTS 接。
    """
    try:
        args = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(args, dict):
        return {}
    return {k: v for k, v in args.items() if isinstance(v, dict)}


def list_skills() -> list[dict]:
    """现扫磁盘（不缓存）：项目级覆盖同名的用户级。"""
    skills: list[dict] = []
    seen: set[str] = set()
    for scope, root in (("project", PROJECT_SKILLS_DIR), ("user", USER_SKILLS_DIR)):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            skill_md = d / "SKILL.md"
            if not d.is_dir() or not skill_md.is_file():
                continue
            try:
                fields, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = fields.get("name") or d.name
            if name in seen:
                continue
            seen.add(name)
            skills.append({
                "name": name,
                "scope": scope,
                "description": fields.get("description", ""),
                "argument_hint": fields.get("argument-hint", ""),
                "requires": fields.get("requires", "").lower(),
                "arguments": _parse_arguments(fields.get("arguments", "")),
            })
    return skills


def resolve_skill(name: str, args: str) -> str:
    """读技能正文（剥 frontmatter，$ARGUMENTS 替换参数）作为 SDK 的 prompt。

    requires: local 的技能抛错——它们要写本地文件（如课程生成），云端 SDK 无文件工具。
    """
    for root in (PROJECT_SKILLS_DIR, USER_SKILLS_DIR):
        skill_md = root / name / "SKILL.md"
        if skill_md.is_file():
            try:
                fields, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            except OSError as e:
                raise RuntimeError(f"技能 {name} 读取失败：{e}") from e
            if fields.get("requires", "").lower() == "local":
                raise RuntimeError(f"技能 /{name} 标记了 requires: local，需要本地文件环境，SDK 引擎不支持")
            return body.replace("$ARGUMENTS", args.strip())
    raise RuntimeError(f"技能 {name} 不存在（检查 .claude/skills/ 或 ~/.claude/skills/）")


async def run_prompt(prompt: str, task_id: str, progress, log=None, timeout: int = 3600) -> dict:
    """无头执行一段 prompt：/开头解析为技能（正文注入），自由文本原样执行。

    log 是追加式时间线回调（前端进度面板用）；不传时退回 progress 的单行语义。
    task_id 与 interactive=True 透传给 agent_sdk.run——技能 invoke 与 AI 命令栏两个
    入口都经这里，由此统一获得「执行中可问用户」能力（挂 AskUserQuestion + 提问提示词段）。
    返回 {result, cost_usd, duration_ms, num_turns}；由调用方（任务层）落库。
    """
    from . import agent_sdk

    emit = log or progress
    prompt = prompt.strip()
    if prompt.startswith("/"):
        m = re.match(r"^/([\w-]+)\s*(.*)$", prompt, re.S)
        if m:
            emit(f"解析技能 /{m.group(1)}…")
            prompt = resolve_skill(m.group(1), m.group(2))
    emit("启动内置 Agent SDK…")
    return await agent_sdk.run(prompt, progress, log=log, timeout=timeout,
                               task_id=task_id, interactive=True)


async def run_skill(name: str, args: str, task_id: str, progress, log=None,
                    timeout: int = 3600) -> dict:
    """执行一个技能（等价 run_prompt(f"/{name} {args}")），参数与结果同 run_prompt。"""
    return await run_prompt(f"/{name} {args}".strip(), task_id, progress, log=log, timeout=timeout)
