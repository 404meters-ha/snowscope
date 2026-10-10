"""脚手架链路编排：一句话需求 → 整体框架匹配（V1）→ 拆条与条目级选型（V2）→ 生成 zip（V3）。

match_pipeline：LLM 归纳需求与关键词 → 双通道检索 → 双轨适配度 → fit 缓存 → 入库打 domain 标签。
split_items（同步）：需求 → 技术条目 + 主技术栈（拆解缓存优先）。
select_pipeline：每条目串行小型检索 → 候选双轨评分 → 回写 items[].candidates。

由 TaskManager 驱动（经 api._submit 包装，签名 (github, *args, task_id, progress)）；
过程日志走 _append_log 时间线。设计见 doc/scaffold/architecture.md。
"""
import hashlib
import json
import logging
import re

from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Repo, ScaffoldCache, ScaffoldRequest, TaskRun
from ..tasks import _append_log
from .github_client import GitHubClient, GitHubRateLimitError
from .industry import _slim_repo
from .llm import LLMClient, LLMNotConfigured, scaffold_brief, scaffold_fit_batch, scaffold_split
from .pipeline import _upsert_repo
from .scoring import scaffold_rule_fit, score_repo
from .tags import ensure_tags

logger = logging.getLogger(__name__)

MATCH_CANDIDATES = 8   # 整体匹配候选数上限
MATCH_MIN = 3          # 少于这个数说明检索面太窄，直接报错让用户换说法
SELECT_CANDIDATES = 5  # 条目级选型的每条目候选数上限
SEARCH_KEYWORD_LIMIT = 5  # GitHub 搜索次数上限（无 token 砍半，同行业分析）
LOCAL_TOP_N = 5        # 本地库每个关键词取的条数


# ---------- 指纹与缓存 ----------

def _norm(text: str) -> str:
    """指纹前的规范化：去空白差异与大小写。"""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(_norm(p) for p in parts).encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    with SessionLocal() as session:
        row = session.get(ScaffoldCache, key)
        return row.payload if row else None


def _cache_put(key: str, kind: str, payload: dict) -> None:
    with SessionLocal() as session:
        if session.get(ScaffoldCache, key) is None:
            session.add(ScaffoldCache(key=key, kind=kind, payload=payload))
            session.commit()


def _json_like(value: str) -> str:
    """JSON 列的 LIKE 模式（topics/tags 存成 json.dumps 转义后的形态，见 api._json_like）。"""
    return f"%{json.dumps(value)}%"


# ---------- 双通道检索与双轨评分（match / select 共用） ----------

async def _search_candidates(github: GitHubClient, keywords: list[str], top_n: int,
                             log, stats: dict) -> list[dict]:
    """本地库（描述/话题/标签 LIKE）+ GitHub Search（star 降序）双通道，合并去重取 top_n。
    本地命中带 in_lib=True 且优先于远程同名的精简数据（含 zh_desc）。"""
    settings = get_settings()
    merged: dict[str, dict] = {}
    with SessionLocal() as session:
        for kw in dict.fromkeys(keywords[:4]):
            rows = session.execute(
                select(Repo)
                .where(Repo.description.like(f"%{kw}%")
                       | Repo.full_name.like(f"%{kw}%")
                       | Repo.topics.like(_json_like(kw))
                       | Repo.tags.like(_json_like(kw)))
                .order_by(Repo.total_score.desc())
                .limit(LOCAL_TOP_N)
            ).scalars().all()
            for r in rows:
                merged[r.full_name] = {
                    "full_name": r.full_name, "description": r.description[:300],
                    "zh_desc": r.zh_desc or "", "stars": r.stars, "language": r.language,
                    "topics": (r.topics or [])[:8], "license": r.license,
                    "pushed_at": r.pushed_at.isoformat() if r.pushed_at else None,
                    "github_created_at": (r.github_created_at.isoformat()
                                          if r.github_created_at else None),
                    "in_lib": True,
                }
    stats["local_hits"] = stats.get("local_hits", 0) + len(merged)

    kw_limit = SEARCH_KEYWORD_LIMIT if settings.github_token else max(SEARCH_KEYWORD_LIMIT // 2, 2)
    for kw in list(dict.fromkeys(keywords))[:kw_limit]:
        log(f"GitHub 搜索：{kw}")
        try:
            for raw in await github.search_repos(kw, per_page=15):
                item = _slim_repo(raw)
                if item["full_name"]:
                    merged.setdefault(item["full_name"], item)  # 本地命中优先，远程只补缺
                    stats["searched"] = stats.get("searched", 0) + 1
        except GitHubRateLimitError as e:
            stats["errors"].append(str(e))
            log(f"搜索限流，提前结束：{e}")
            break
        except Exception as e:  # noqa: BLE001 单个关键词失败继续
            stats["errors"].append(f"搜索 {kw}: {e}")
    return sorted(merged.values(), key=lambda c: c["stars"], reverse=True)[:top_n]


async def _score_candidates(llm: LLMClient, need_text: str, item_fp: str, candidates: list[dict],
                            skills: list[str], keywords: list[str], stats: dict, log,
                            focus: str = "") -> None:
    """就地给候选填双轨分：规则分（全量）+ LLM 语义分（0-100 折算 0-60，fit 缓存优先）+ 合成 fit_score。"""
    llm_pend: list[dict] = []
    for c in candidates:
        c["clone_url"] = f"https://github.com/{c['full_name']}.git"
        c["rule_score"], c["rule_reason"] = scaffold_rule_fit(keywords, c, skills)
        cached = _cache_get(f"fit:{item_fp}:{c['full_name']}")
        if cached and "llm_score" in cached:
            c["llm_score"] = cached["llm_score"]
            c["reason"] = cached.get("reason", "")
            stats["fit_cached"] = stats.get("fit_cached", 0) + 1
        else:
            llm_pend.append({"full_name": c["full_name"], "description": c.get("description", ""),
                             "stars": c["stars"], "language": c.get("language", ""),
                             "topics": (c.get("topics") or [])[:6]})
    if llm_pend:
        log(f"LLM 评估 {len(llm_pend)} 个候选的适配度…")
        try:
            scored = await scaffold_fit_batch(llm, need_text, llm_pend, focus=focus)
        except Exception as e:  # noqa: BLE001 评分失败不拖垮（候选仍有规则分）
            stats["errors"].append(f"LLM 评分: {e}")
            log(f"LLM 评分失败，退回纯规则分：{e}")
            scored = []
        by_name = {s["full_name"]: s for s in scored if s.get("full_name")}
        for c in candidates:
            if "llm_score" in c:
                continue
            s = by_name.get(c["full_name"]) or {}
            try:
                c["llm_score"] = min(60, max(0, round(float(s.get("score") or 0) * 0.6)))
            except (TypeError, ValueError):
                c["llm_score"] = 0
            c["reason"] = str(s.get("reason") or "")[:200]
            _cache_put(f"fit:{item_fp}:{c['full_name']}", "fit",
                       {"llm_score": c["llm_score"], "reason": c["reason"]})
    for c in candidates:
        c["fit_score"] = round(min(100, c["rule_score"] + c.get("llm_score", 0)), 1)
    candidates.sort(key=lambda c: c["fit_score"], reverse=True)


# ---------- 整体匹配（V1） ----------

async def match_pipeline(github: GitHubClient, request_id: int, task_id: str, progress) -> dict:
    """整体框架匹配：需求一句话 → 候选 5-8 个（双轨适配度 + 理由）→ status=matched。"""
    settings = get_settings()
    with SessionLocal() as session:
        req = session.get(ScaffoldRequest, request_id)
        if req is None:
            raise ValueError(f"脚手架需求 {request_id} 不存在")
        raw_text = req.raw_text

    llm = LLMClient()
    if not llm.configured:
        await llm.close()
        raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY），无法做框架匹配")

    def log(msg: str) -> None:
        _append_log(task_id, msg)

    stats: dict = {"searched": 0, "local_hits": 0, "candidates": 0, "new_repos": 0,
                   "fit_cached": 0, "errors": []}
    try:
        # 1. LLM 需求归纳 + 检索关键词规划
        log("LLM 归纳需求与检索关键词…")
        brief = await scaffold_brief(llm, raw_text, settings.user_profile)
        keywords = [str(k).strip() for k in brief.get("search_keywords", []) if str(k).strip()]
        keywords += [str(k).strip() for k in brief.get("tech_hints", []) if str(k).strip()]
        if not keywords:
            keywords = [raw_text]
        log(f"归纳完成：领域「{brief.get('domain', '?')}」，{len(set(keywords))} 个检索关键词")

        # 2. 双通道检索
        ranked = await _search_candidates(github, keywords, MATCH_CANDIDATES, log, stats)
        if len(ranked) < MATCH_MIN:
            raise ValueError(f"只检索到 {len(ranked)} 个相关项目（需 ≥{MATCH_MIN}），换个更明确的说法试试")
        stats["candidates"] = len(ranked)
        if stats["local_hits"]:
            log(f"本地库命中 {stats['local_hits']} 个相关项目")

        # 3. 双轨评分（整体匹配阶段「条目指纹」= 需求指纹）
        need_text = (f"{raw_text}\n归纳：{brief.get('summary', '')}；"
                     f"功能：{'、'.join(brief.get('features', []))}")
        skills = [s.strip() for s in settings.user_skills.split(",") if s.strip()]
        await _score_candidates(llm, need_text, _fp(raw_text), ranked, skills, keywords, stats, log)

        # 4. 候选入库 + 打 domain 标签（累积知识，下次同类需求本地库直接命中）
        domain_raw = str(brief.get("domain") or "").strip()
        domain_tag = ""
        if domain_raw:
            try:
                mapping = await ensure_tags(llm, [domain_raw], source="scaffold")
                domain_tag = mapping.get(domain_raw, domain_raw)
            except Exception as e:  # noqa: BLE001 归一失败不阻塞，退回原词
                logger.info("domain 标签归一失败: %s", e)
                domain_tag = domain_raw
        with SessionLocal() as session:
            for c in ranked:
                if c.get("in_lib"):
                    continue
                repo = _upsert_repo(session, c)
                if domain_tag:
                    repo.tags = sorted(set((repo.tags or []) + [domain_tag]))
                if not repo.rule_score:
                    repo.rule_score, repo.rule_detail = score_repo(c, period_stars=0, period_days=30)
                c["in_lib"] = True
                stats["new_repos"] += 1
            session.commit()

        # 5. 回写需求记录 + 任务 payload 挂 stats/request_id（前端任务面板可直接跳详情）
        with SessionLocal() as session:
            req = session.get(ScaffoldRequest, request_id)
            req.need_brief = brief
            req.framework_candidates = ranked
            req.status = "matched"
            session.commit()
        with SessionLocal() as session:
            run = session.get(TaskRun, task_id)
            if run is not None:
                run.payload = {**(run.payload or {}), "stats": stats, "request_id": request_id}
                session.commit()
        log(f"匹配完成：{len(ranked)} 个候选，最高适配度 {ranked[0]['fit_score']}"
            f"（{ranked[0]['full_name']}）")
        return stats
    finally:
        await llm.close()


# ---------- 拆条（V2，同步 2-10s） ----------

async def split_items(request_id: int) -> dict:
    """需求 → 技术条目 + 主技术栈；拆解缓存（需求指纹）优先命中秒回。"""
    settings = get_settings()
    with SessionLocal() as session:
        req = session.get(ScaffoldRequest, request_id)
        if req is None:
            raise ValueError(f"脚手架需求 {request_id} 不存在")
        raw_text, brief = req.raw_text, (req.need_brief or {})

    cache_key = f"decompose:{_fp(raw_text)}"
    cached = _cache_get(cache_key)
    if cached and cached.get("items"):
        result = cached
    else:
        llm = LLMClient()
        try:
            result = await scaffold_split(llm, raw_text, brief, settings.user_profile)
        finally:
            await llm.close()
        _cache_put(cache_key, "decompose", result)

    items = [
        {"no": i, "name": str(it.get("name", "")).strip() or f"条目{i}",
         "desc": str(it.get("desc", "")).strip(),
         "keywords": [str(k).strip() for k in (it.get("keywords") or []) if str(k).strip()],
         "candidates": [], "selected": None, "self_dev": False}
        for i, it in enumerate(result.get("items", [])[:8], 1)
    ]
    if not items:
        raise ValueError("拆解结果为空，换个更具体的需求描述试试")
    tech_stack = str(result.get("tech_stack") or "").strip()

    with SessionLocal() as session:
        req = session.get(ScaffoldRequest, request_id)
        req.items = items
        if tech_stack:
            req.tech_stack = tech_stack
        req.status = "split"
        session.commit()
        return {"items": items, "tech_stack": req.tech_stack, "cached": bool(cached)}


# ---------- 条目级选型（V2） ----------

async def select_pipeline(github: GitHubClient, request_id: int, task_id: str, progress) -> dict:
    """每条目串行：关键词检索 → 候选双轨评分（条目指纹 fit 缓存）→ 回写 items[].candidates。"""
    settings = get_settings()
    with SessionLocal() as session:
        req = session.get(ScaffoldRequest, request_id)
        if req is None:
            raise ValueError(f"脚手架需求 {request_id} 不存在")
        raw_text = req.raw_text
        items = [dict(it) for it in (req.items or [])]

    if not items:
        raise ValueError("没有待选型的技术条目（先拆条并确认）")

    llm = LLMClient()
    if not llm.configured:
        await llm.close()
        raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY），无法做条目选型")

    def log(msg: str) -> None:
        _append_log(task_id, msg)

    stats: dict = {"items": len(items), "searched": 0, "local_hits": 0,
                   "candidates": 0, "fit_cached": 0, "new_repos": 0, "errors": []}
    skills = [s.strip() for s in settings.user_skills.split(",") if s.strip()]
    try:
        # 条目名过标签库归一（技术能力维度的 canonical，选型候选入库时打这个标签）
        names = [str(it.get("name") or "").strip() for it in items if str(it.get("name") or "").strip()]
        try:
            tag_map = await ensure_tags(llm, names, source="scaffold")
        except Exception as e:  # noqa: BLE001 归一失败退回原词
            logger.info("条目标签归一失败: %s", e)
            tag_map = {}

        progress(f"0/{len(items)} 个条目")
        for idx, item in enumerate(items, 1):
            name = str(item.get("name") or "").strip()
            keywords = [str(k).strip() for k in (item.get("keywords") or []) if str(k).strip()]
            log(f"[{idx}/{len(items)}] 条目「{name}」检索候选…")
            candidates = await _search_candidates(github, keywords, SELECT_CANDIDATES, log, stats)
            item_fp = _fp(name, item.get("desc") or "", " ".join(keywords))
            need_text = f"{raw_text}｜条目「{name}」：{item.get('desc', '')}"
            focus = ("【评分视角】这是给单个技术条目选组件，不是给整个需求找框架："
                     "项目必须能承担该条目的具体职责才算高分，只会做界面的通用框架不算。\n")
            if candidates:
                await _score_candidates(llm, need_text, item_fp, candidates, skills, keywords,
                                        stats, log, focus=focus)
            # 候选入库 + 打条目标签（库越用越厚：下次「视觉识别」类条目本地库直接命中）
            item_tag = tag_map.get(name, name)
            with SessionLocal() as session:
                for c in candidates:
                    if c.get("in_lib"):
                        continue
                    repo = _upsert_repo(session, c)
                    repo.tags = sorted(set((repo.tags or []) + [item_tag]))
                    if not repo.rule_score:
                        repo.rule_score, repo.rule_detail = score_repo(c, period_stars=0, period_days=30)
                    c["in_lib"] = True
                    stats["new_repos"] += 1
                session.commit()
            item["candidates"] = candidates
            item.pop("selected", None)  # 重选型清掉旧选择
            item.pop("self_dev", None)
            stats["candidates"] += len(candidates)
            top = candidates[0] if candidates else None
            log(f"条目「{name}」候选 {len(candidates)} 个"
                + (f"，最高适配 {top['fit_score']}（{top['full_name']}）" if top else "（检索为空，可标自研）"))
            progress(f"{idx}/{len(items)} 个条目")

        with SessionLocal() as session:
            req = session.get(ScaffoldRequest, request_id)
            req.items = items
            req.status = "selected"
            session.commit()
        with SessionLocal() as session:
            run = session.get(TaskRun, task_id)
            if run is not None:
                run.payload = {**(run.payload or {}), "stats": stats, "request_id": request_id}
                session.commit()
        log(f"选型完成：{len(items)} 个条目共 {stats['candidates']} 个候选，逐条勾选后即可生成脚手架")
        return stats
    finally:
        await llm.close()
