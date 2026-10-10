"""脚手架生成器：把「需求 + 技术条目 + 开源选型」交给 agent_sdk 生成可启动项目骨架。

build_pipeline（scaffold_build 任务）：组合指纹 → 产物缓存命中则秒回 →
base 预判（SCAFFOLD_BASE_SYSTEM）→ agent 按生成规范写工作区 → 六件套校验 →
zipfile 打包 → 产物缓存登记 → 清工作区 → 回写 build + status=built。
设计见 doc/scaffold/architecture.md 第 4 节；S6 技术验证门结论见 validation.md。
"""
import re
import shutil
import zipfile

from ..db import SessionLocal
from ..models import ScaffoldRequest, TaskRun
from ..tasks import _append_log
from . import agent_sdk
from .llm import LLMClient, LLMNotConfigured, scaffold_base

# 生成轮次上限：整项目 10+ 文件，/tech 课程实测 16 轮起步，60 不够用
BUILD_MAX_TURNS = 120
# 单轮输出上限：vendor 默认 openai 回落 8192，整文件写入的轮次不够用；
# 16k 实测会被「一轮连写多个文件」的长响应顶满（E2E 截断失败），glm-4.7 输出上限远高于 32k
BUILD_MAX_TOKENS = 32768

PROJECT_ROOT = agent_sdk.PROJECT_ROOT
SCAFFOLDS_ROOT = PROJECT_ROOT / "scaffolds"

# 平台侧校验的六件套（缺任一即 build 任务失败，见 stories.md S7-AC2）
REQUIRED_FILES = [
    "README.md",          # 启动说明（三步内）
    "LICENSES.md",        # 各组件 license 清单
    "docs/research.md",   # 行业调研文档（全链路汇编）
    "docs/requirement.md",  # 原始需求 + 归纳
    "docs/architecture.md",  # 条目 + 选型 + 集成关系
]
# 前端页面的常见形态：零构建 static/、Next.js 的 app/page.*、Django 的 templates/，
# 最后兜底「任意 .html 文件」——base 框架不同前端落位不同，不能只认 index.html
FRONTEND_GLOBS = ("index.html", "static/index.html", "frontend/index.html", "web/index.html",
                  "public/index.html", "app/page.*", "pages/index.*", "src/app/page.*",
                  "src/pages/index.*", "templates/**/*.html")


def _check_six(workspace) -> list[str]:
    """六件套校验，返回缺失清单（空 = 齐全）。"""
    missing = [rel for rel in REQUIRED_FILES if not (workspace / rel).is_file()]
    has_frontend = any(next(workspace.glob(g), None) for g in FRONTEND_GLOBS) \
        or any(workspace.rglob("*.html"))
    if not has_frontend:
        missing.append("前端页面（index.html / app/page.* / templates 等）")
    return missing


def _check_imports(workspace) -> list[str]:
    """Python 相对导入静态校验：点数 = 上退目录级数，目标文件必须实存。
    生成高频 bug 类（子包模块引根目录 config 少写一个点 → 运行时 ModuleNotFoundError），
    E2E 实证两轮均中招，作为平台侧兜底门（JS 侧无静态语法可查，仍靠规范约束）。"""
    import ast

    problems: list[str] = []
    for p in sorted(workspace.rglob("*.py")):
        rel = p.relative_to(workspace).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 语法问题不是本门的职责
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.level > 0):
                continue
            base = p.parent
            for _ in range(node.level - 1):
                base = base.parent
            dots = "." * node.level
            if base == workspace.parent:
                problems.append(f"{rel}: from {dots}{node.module or ''} 上退越过了项目顶层")
                continue
            if node.module:  # from ..x import y → 校验 x 存在（目录也算，兼容命名空间包）
                cand = base.joinpath(*node.module.split("."))
                if not (cand.with_suffix(".py").is_file() or cand.is_dir()):
                    problems.append(f"{rel}: from {dots}{node.module} 目标不存在")
            else:           # from . import a, b → 逐个校验子模块
                for alias in node.names:
                    if not ((base / alias.name).with_suffix(".py").is_file()
                            or (base / alias.name).is_dir()):
                        problems.append(f"{rel}: from {dots} import {alias.name} 目标不存在")
    return problems

SCAFFOLD_SYSTEM = """你是 celestial-snow 平台的脚手架生成 agent，任务：把「用户需求 + 技术条目 + 开源选型」\
变成一个解压即可启动的项目骨架。

## 环境与工具
- 你运行在云服务进程内，没有 Shell，不能执行安装/运行命令——产物必须做到「用户解压后按 README 三步内启动成功」。
- 工具：WebFetch（查开源项目 README/文档，辅助集成）、ReadFile/ListDir（核对已写文件）、
  WriteFile（写工作区文件）、PlatformAPI（调平台 API）。
- 所有文件只写到本次任务指定的工作区目录下。

## 产物规范（六件套，缺一不可）
1. 项目代码骨架：base 框架的目录结构 + 每个技术条目一个模块位（文件/包 + 最小可运行实现）
2. 前端页面：必有。浏览器打开即用的入口页（优先零构建：原生 HTML/JS 或 CDN 引入库），作为项目控制台或主界面
3. docs/research.md：调研留痕，必须完整覆盖四段（候选与分数用任务输入提供的实录，不得虚构）：
   ① 原始需求与归纳；② 整体框架匹配对比（平台检索的候选/适配分/理由，及为何未整体采用而拆条）；
   ③ 每个技术条目的候选对比与最终选型理由（自研条目写自研理由）；④ 参考链接（所选与落选候选的 GitHub 地址）
4. docs/requirement.md：原始需求（用户原话）+ 系统归纳
5. docs/architecture.md：技术条目清单、每条选型结果与集成关系（谁挂在谁上面）
6. LICENSES.md：所用开源组件的 license 列表（组件 / 仓库 / 协议）

## 验收线（平台会校验，也是你的完成标准）
- 解压 → 装依赖（pip install -r requirements.txt 或 npm install）→ README 里的一条启动命令 → 页面可打开
- 每个条目模块的接口位接好；核心业务逻辑用结构化 TODO 占位，格式统一：
  `# TODO [条目名] 一句话描述 | 参考: owner/repo | 状态: 待实现`
- 不承诺功能完整——骨架能跑、结构清晰、TODO 指路即可

## 选型落位规则
- 包型选型（以 pip/npm 包分发的）→ 进依赖清单 requirements.txt / package.json，代码里 import 使用
- 应用型选型（独立运行的程序）→ vendor/ 下放 README 说明 + clone.sh 克隆脚本，不打包其源码
- 自研条目（无开源选型）→ 从零写最小骨架
- 本地文件型组件（向量库/数据库等）优先跨平台方案，避免只支持 Linux/macOS 的组件
  （E2E 实证：Milvus Lite 无 Windows 轮子，pip 静默跳过 extra、启动即 ConnectionConfigException；
  可落 SQLite 系或把该限制写进 README 的「已知限制」）

## 质量要求
- 文件总量 10-25 个，单文件不超过 ~300 行
- 每写完一批用 ListDir 核对；最后清点六件套齐全
- README 启动步骤 ≤3 步，写清端口与访问地址
- 依赖必须自洽：有 tsconfig.json 就把 typescript 与 @types/* 写进 devDependencies，
  有 .py 脚本就把 Python 依赖写进 requirements.txt——不得依赖框架的「启动时自动安装」
- import 相对路径必须实存核对（E2E 实测教训，平台会对 Python 做静态校验）：
  JS 的 app/api/**/route.js 引根目录 lib/ 要退满三级 `../../../lib/`，少一级运行时 500；
  Python 点数 = 上退目录级数——子包模块（如 app/services/x.py）引根目录配置必须 `from ..config import`，
  写 `from .config` 启动即 ModuleNotFoundError；每写完一批文件，用 ListDir 对照一遍所有 import 目标
- 文档中文；代码命名贴合所选生态的惯例
"""


def _clip(s, n: int = 80) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def build_prompt(raw_text: str, need_brief: dict, items: list[dict],
                 base_hint: str, workspace: str,
                 framework_candidates: list[dict] | None = None) -> str:
    """组装一次生成任务的指令（S8 起带全链决策素材，供 agent 写 research.md 留痕）。"""
    lines = [f"## 本次任务\n", f"### 用户需求（原话）\n{raw_text}\n"]
    if need_brief:
        brief = need_brief if isinstance(need_brief, str) else "\n".join(
            f"- {k}: {v}" for k, v in need_brief.items() if v)
        lines.append(f"### 需求归纳\n{brief}\n")
    # 整体匹配实录（research.md 第②段的素材）：为什么没有整体采用某个框架而走到拆条
    if framework_candidates:
        lines.append("### 整体框架匹配实录（平台检索 + 双轨评分，供 research.md 第②段引用）")
        for c in sorted(framework_candidates, key=lambda x: -(x.get("fit_score") or 0))[:6]:
            lines.append(f"- {c.get('full_name')}（适配 {c.get('fit_score')}）"
                         f"{_clip(c.get('reason'))}｜{c.get('clone_url')}")
        lines.append("")
    lines.append("### 技术条目与选型（落位规则见系统提示；license 写进 LICENSES.md；候选对比写进 research.md 第③段）")
    for it in items:
        lic = f"（{it['license']}）" if it.get("license") else ""
        sel = it.get("selected") or "自研（无开源选型，从零写最小骨架）"
        lines.append(
            f"{it.get('no')}. {it.get('name')} → {sel}{lic}\n"
            f"   职责：{it.get('desc', '')}\n   选型理由：{it.get('reason', '')}")
        for c in sorted(it.get("candidates") or [], key=lambda x: -(x.get("fit_score") or 0)):
            mark = "✅ 采纳" if c.get("full_name") == it.get("selected") else "落选"
            lines.append(f"   - 候选 {c.get('full_name')}（适配 {c.get('fit_score')}，{mark}）："
                         f"{_clip(c.get('reason'))}｜{c.get('clone_url') or ''}")
    lines.append(f"\n### base 框架\n{base_hint}\n")
    lines.append(f"### 工作区\n`{workspace}` —— 所有文件写该目录下，zip 将从该目录打包。\n")
    lines.append(
        "现在开始：先简短规划文件清单（10 行以内），然后逐个 WriteFile 生成，"
        "最后 ListDir 核对六件套齐全并输出一段总结（生成了什么、怎么启动、遗留 TODO 数）。")
    return "\n".join(lines)


# ---------- 组合指纹与产物缓存 ----------

def build_fingerprint(items: list[dict], tech_stack: str) -> str:
    """组合指纹 = 主技术栈 + 各条目(名称→选型) + 版本号；任一变化即视为新组合。
    生成规范升级时改版本号，旧产物缓存自动失效。"""
    import hashlib

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).lower()

    parts = [norm(tech_stack or "")]
    for it in sorted(items, key=lambda x: x.get("no", 0)):
        sel = it.get("selected") or ("self" if it.get("self_dev") else "none")
        parts.append(f"{norm(str(it.get('name', '')))}={norm(str(sel))}")
    parts.append("v3")  # v3：相对导入静态校验门 + Python 点级规范（v2 产物实测有少一个点的 ModuleNotFoundError）；
    # v2 曾升版引入 research.md 全链留痕素材（整体匹配实录 + 每条目候选对比 + clone_url）
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _slug(text: str, limit: int = 30) -> str:
    """zip 顶层目录名：需求前缀的可读 slug（中文保留，路径非法字符替换）。"""
    s = re.sub(r'[\\/:*?"<>|#%&{}$!\'@+`=\[\]^~]', "-", (text or "").strip())[:limit].strip("- ")
    return s or "scaffold"




async def build_pipeline(github, request_id: int, task_id: str, progress) -> dict:
    """生成管线（scaffold_build 任务）：缓存秒回 → base 预判 → Agent 生成 → 校验 → zip → 回写。"""
    with SessionLocal() as session:
        req = session.get(ScaffoldRequest, request_id)
        if req is None:
            raise ValueError(f"脚手架需求 {request_id} 不存在")
        raw_text = req.raw_text
        need_brief = req.need_brief or {}
        tech_stack = req.tech_stack or ""
        items = [dict(it) for it in (req.items or [])]
        framework_candidates = [dict(c) for c in (req.framework_candidates or [])]

    if not items:
        raise ValueError("没有技术条目（先拆条选型）")
    unselected = [it.get("name") for it in items if not it.get("selected") and not it.get("self_dev")]
    if unselected:
        raise ValueError(f"条目未选型：{'、'.join(unselected)}（可标自研）")

    def log(msg: str) -> None:
        _append_log(task_id, msg)

    stats: dict = {"files": 0, "turns": 0, "cost_usd": 0.0, "cache_hit": False, "errors": []}

    # 1. 组合指纹 → 产物缓存命中则直接回写（同组合重生成不重跑 Agent）
    fingerprint = build_fingerprint(items, tech_stack)
    cache_key = f"build:{fingerprint}"
    with SessionLocal() as session:
        from ..models import ScaffoldCache
        cached = session.get(ScaffoldCache, cache_key)
        cached_payload = cached.payload if cached else None
    if cached_payload and cached_payload.get("zip_key"):
        zip_path = SCAFFOLDS_ROOT / cached_payload["zip_key"]
        if zip_path.is_file():
            with SessionLocal() as session:
                req = session.get(ScaffoldRequest, request_id)
                req.build = {**cached_payload["build"], "cache_hit": True}
                req.status = "built"
                session.commit()
            stats["cache_hit"] = True
            log("命中产物缓存：同组合已生成过，直接返回 zip（未重跑 Agent）")
            _finish_payload(task_id, stats, request_id)
            return stats

    # 2. base 预判（单次 LLM）
    llm = LLMClient()
    if not llm.configured:
        await llm.close()
        raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY），无法生成脚手架")
    try:
        log("预判 base 主干与挂载关系…")
        try:
            base_plan = await scaffold_base(llm, raw_text, tech_stack, items)
        except Exception as e:  # noqa: BLE001 预判失败退化为「无主干」描述，不阻塞生成
            log(f"base 预判失败（退化为默认骨架）：{e}")
            base_plan = {}
        base_name = str(base_plan.get("base") or "").strip()
        base_hint = (f"{base_name} 为主干。{base_plan.get('mounting', '')}"
                     if base_name else
                     f"无单一大主干：以 {tech_stack or '主技术栈'} 的常规项目骨架组织，各条目模块平铺挂载。")
        log(f"base：{base_name or '（无，默认骨架）'}")

        # 3. 组装选型素材（license/reason 给 Agent 写 LICENSES.md 与调研文档）
        cand_index = {c["full_name"]: c for it in items for c in (it.get("candidates") or [])}
        for it in items:
            sel = it.get("selected")
            if sel:
                c = cand_index.get(sel, {})
                it["license"] = c.get("license") or ""
                it.setdefault("reason", c.get("reason", ""))
            else:
                it["license"] = ""

        # 4. Agent 生成（进度事件实时进任务时间线）
        workspace = SCAFFOLDS_ROOT / "workspace" / str(request_id)
        if workspace.exists():
            shutil.rmtree(workspace)  # 重跑幂等：清掉上次残留
        workspace.mkdir(parents=True, exist_ok=True)
        prompt = build_prompt(raw_text, need_brief, items, base_hint,
                              f"scaffolds/workspace/{request_id}",
                              framework_candidates=framework_candidates)
        log("Agent 开始生成项目骨架（10-25 个文件，预计数分钟）…")
        result = await agent_sdk.run(
            prompt,
            progress=lambda m: None,
            log=log,
            timeout=3600,
            max_turns=BUILD_MAX_TURNS,
            max_tokens=BUILD_MAX_TOKENS,
            system_prompt=SCAFFOLD_SYSTEM,
        )
        stats.update(turns=result["num_turns"], cost_usd=result["cost_usd"])

        # 5. 六件套校验 + 相对导入静态校验（缺失/错级即失败，日志指明问题，可重试）
        missing = _check_six(workspace)
        import_problems = _check_imports(workspace)
        if missing or import_problems:
            raise ValueError("生成产物校验未过："
                             + (f"缺件[{'、'.join(missing)}] " if missing else "")
                             + (f"导入错误[{'；'.join(import_problems)}]" if import_problems else "")
                             + "（可重试；重试会重新生成）")

        # 6. 打包 zip（顶层目录 = 需求 slug）+ 登记产物缓存
        files = [p for p in workspace.rglob("*") if p.is_file()]
        stats["files"] = len(files)
        zip_key = f"{request_id}/scaffold.zip"
        zip_path = SCAFFOLDS_ROOT / zip_key
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        top_dir = _slug(raw_text)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(files):
                zf.write(p, f"{top_dir}/{p.relative_to(workspace).as_posix()}")
        total_bytes = zip_path.stat().st_size

        # license 告警（不阻断）：协议未知的、GPL/AGPL 强传染的
        warnings = [
            f"{it.get('selected')}: {it.get('license') or 'license 未知'}"
            for it in items if it.get("selected") and not it.get("license")
        ] + [
            f"{it.get('selected')}: 强传染协议（{it['license']}）"
            for it in items if str(it.get("license") or "").upper().startswith(("GPL", "AGPL"))
        ]
        build_info = {
            "fingerprint": fingerprint,
            "zip_key": zip_key,
            "zip_url": f"/scaffolds/{zip_key}",
            "file_count": len(files),
            "total_bytes": total_bytes,
            "base": base_name,
            "base_rationale": str(base_plan.get("rationale") or ""),
            "mounting": str(base_plan.get("mounting") or ""),
            "turns": result["num_turns"],
            "cost_usd": result["cost_usd"],
            "licenses": [{"item": it["name"], "repo": it.get("selected"), "license": it.get("license")}
                         for it in items if it.get("selected")],
            "warnings": warnings,
        }

        with SessionLocal() as session:
            from ..models import ScaffoldCache
            if session.get(ScaffoldCache, cache_key) is None:
                session.add(ScaffoldCache(key=cache_key, kind="build",
                                          payload={"zip_key": zip_key, "build": build_info}))
                session.commit()

        shutil.rmtree(workspace)  # 工作区用完即清，产物只留 zip

        # 7. 回写需求记录
        with SessionLocal() as session:
            req = session.get(ScaffoldRequest, request_id)
            req.build = build_info
            req.status = "built"
            session.commit()
        log(f"生成完成：{len(files)} 个文件打包 {total_bytes // 1024}KB"
            f"（{result['num_turns']} 轮 / ${result['cost_usd']:.2f}），zip 可下载")
        _finish_payload(task_id, stats, request_id)
        return stats
    finally:
        await llm.close()


def _finish_payload(task_id: str, stats: dict, request_id: int) -> None:
    with SessionLocal() as session:
        run = session.get(TaskRun, task_id)
        if run is not None:
            run.payload = {**(run.payload or {}), "stats": stats, "request_id": request_id}
            session.commit()
