"""REST API：榜单、详情、刷新、贡献分析、issue 排行、任务进度、学习闭环。"""
import io
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, select

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .config import get_settings
from .db import SessionLocal
from .models import (
    Analysis,
    Book,
    ContributionReport,
    Course,
    IndustryReport,
    Issue,
    QuizResult,
    Repo,
    ScaffoldRequest,
    TaskRun,
    utcnow,
)
from .services import askuser, course_publish, scoring, skill_runner
from .services.store import StoreError
from .services.github_client import GitHubClient
from .services.industry import (
    multi_industry_pipeline,
    parse_input,
    tagging_pipeline,
    translate_pipeline,
)
from .services.scaffold import match_pipeline, select_pipeline, split_items
from .services.scaffold_builder import build_pipeline
from .services.books import book_dir, extract_pipeline, series_pipeline
from .services.llm import LLMClient, LLMNotConfigured, scaffold_adopt_report
from .services.pipeline import (
    MAX_ANALYZE_REPOS,
    MAX_CONTRIB_REPOS,
    _upsert_repo,
    analyze_pipeline,
    contribution_pipeline,
    refresh_pipeline,
)
from .tasks import _append_log, manager

router = APIRouter(prefix="/api")


# ---------- 榜单 ----------

def _repo_view(repo: Repo, analysis: Analysis | None) -> dict:
    return {
        "id": repo.id,
        "full_name": repo.full_name,
        "description": repo.description,
        "zh_desc": repo.zh_desc or "",
        "language": repo.language,
        "topics": repo.topics or [],
        "homepage": repo.homepage,
        "license": repo.license,
        "stars": repo.stars,
        "open_issues": repo.open_issues,
        "contributors_count": repo.contributors_count,
        "periods": repo.periods or [],
        "tags": repo.tags or [],
        "rule_score": repo.rule_score,
        "rule_detail": repo.rule_detail or {},
        "total_score": repo.total_score,
        "first_seen_at": repo.first_seen_at.isoformat() if repo.first_seen_at else None,
        "github_created_at": repo.github_created_at.isoformat() if repo.github_created_at else None,
        "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
        "analyzed": bool(analysis and analysis.status == "done"),
        "ai_analyzed": bool(analysis and analysis.note.startswith("AI")),
        "core_idea": analysis.core_idea if analysis else "",
        "enterprise_cases": analysis.enterprise_cases if analysis else [],
        "llm_scores": analysis.llm_scores if analysis else {},
    }


def _json_like(value: str) -> str:
    """JSON 列的 LIKE 模式：SQLAlchemy 落库走 json.dumps（默认 ensure_ascii），
    中文 tag 存成 "\\uXXXX"——模式必须同样转义，裸写 '%"中文"%' 永远匹配不到。"""
    return f"%{json.dumps(value)}%"


# ---------- 入库批次（周）：刷新榜单后区分新入与老项目 ----------

# first_seen_at 存 UTC，批次按本地（东八区）自然周划分——用户直觉里的「这周刷进来的一批」
BATCH_TZ = timezone(timedelta(hours=8))


def _iso_week(dt: datetime | None) -> str:
    """UTC 入库时间 → 本地时区 ISO 周标识（如 2026-W41）；空值返回空串。"""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso = dt.astimezone(BATCH_TZ).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_bounds_utc(week: str) -> tuple[datetime, datetime] | None:
    """ISO 周标识 → 该本地自然周的 [start, end) UTC naive 区间。

    first_seen_at 落库为 UTC naive 字符串，比较也用 naive 绑定，避免 tz 格式不一致。"""
    try:
        year, _, wk = week.partition("-W")
        local_start = datetime.fromisocalendar(int(year), int(wk), 1).replace(tzinfo=BATCH_TZ)
    except ValueError:
        return None
    start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end = (local_start + timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


@router.get("/repos/weeks")
def list_repo_weeks():
    """项目榜入库批次清单：按本地 ISO 周聚合 first_seen_at，倒序，供批次筛选 tab。

    label 只对最近两个自然周给「本周/上周」，更早的周前端直接显示 range（MM.DD–MM.DD）。"""
    with SessionLocal() as session:
        rows = session.execute(select(Repo.first_seen_at)).scalars().all()
    counts: dict[str, int] = {}
    for seen in rows:
        wk = _iso_week(seen)
        if wk:
            counts[wk] = counts.get(wk, 0) + 1
    now = datetime.now(timezone.utc)
    this_week, last_week = _iso_week(now), _iso_week(now - timedelta(days=7))
    newest = max(counts) if counts else ""  # 数据里最新的周（new_batch 徽标同基准）
    weeks = []
    for wk in sorted(counts, reverse=True):
        bounds = _week_bounds_utc(wk)
        if bounds is None:
            continue
        local_start = bounds[0].replace(tzinfo=timezone.utc).astimezone(BATCH_TZ)
        local_end = (bounds[1] - timedelta(seconds=1)).replace(tzinfo=timezone.utc).astimezone(BATCH_TZ)
        weeks.append({
            "week": wk,
            "count": counts[wk],
            "label": "本周" if wk == this_week else "上周" if wk == last_week else "",
            "range": f"{local_start.strftime('%m.%d')}–{local_end.strftime('%m.%d')}",
            "latest": wk == newest,
        })
    return {"weeks": weeks}


@router.get("/repos")
def list_repos(
    sort: str = "total",  # total | rule | stars
    q: str = "",  # full_name/description 模糊
    limit: int = 20,
    offset: int = 0,
    analyzed: str = "all",  # all | done（已精析）| todo（未精析，含 failed/skipped/无记录）
    period: str = "",  # weekly | monthly | 空=不限榜单期次
    week: str = "",  # 入库批次（本地 ISO 周）：latest=最新批次周，YYYY-Www=指定周，空=不限
    tag: list[str] = Query(default=[]),  # 标签多选（AND 交集：同时具备全部所选标签）
):
    """项目榜：分页（limit/offset）+ 精析状态/榜单期次/入库批次/标签过滤；total 供前端分页控件。"""
    with SessionLocal() as session:
        # 最新批次周（行内「新入库」徽标基准；week=latest 也以它为准——库最晚入库时间所在周）
        latest_week = _iso_week(session.scalar(select(func.max(Repo.first_seen_at))))
        if week == "latest":
            week = latest_week
        stmt = select(Repo, Analysis).outerjoin(Analysis, Analysis.repo_id == Repo.id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Repo.full_name.like(like) | Repo.description.like(like))
        if analyzed == "done":
            stmt = stmt.where(Analysis.status == "done")
        elif analyzed == "todo":
            # 无 Analysis 记录的行 status 为 NULL（NULL != 'done' 判不出真），必须显式 or
            stmt = stmt.where(or_(Analysis.id.is_(None), Analysis.status != "done"))
        if period in ("weekly", "monthly"):
            stmt = stmt.where(Repo.periods.like(_json_like(period)))
        if week:
            bounds = _week_bounds_utc(week)
            if bounds is None:
                raise HTTPException(400, f"周次格式应为 YYYY-Www，收到：{week}")
            stmt = stmt.where(Repo.first_seen_at >= bounds[0], Repo.first_seen_at < bounds[1])
        if tag:
            conditions = [Repo.tags.like(_json_like(t)) for t in tag if t.strip()]
            if conditions:
                stmt = stmt.where(and_(*conditions))
        # id 兜底 tiebreaker：OFFSET 分页要求排序全序稳定
        if sort == "stars":
            stmt = stmt.order_by(Repo.stars.desc(), Repo.id.desc())
        elif sort == "rule":
            stmt = stmt.order_by(Repo.rule_score.desc(), Repo.id.desc())
        else:
            stmt = stmt.order_by(Repo.total_score.desc(), Repo.rule_score.desc(), Repo.id.desc())
        total = session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = session.execute(stmt.limit(min(limit, 200)).offset(max(offset, 0))).unique().all()
        # 最近一份贡献报告 id（行内「贡献报告」按钮）；只对查出的一页做，避免全表扫
        repo_ids = [repo.id for repo, _ in rows]
        report_ids: dict[int, int] = {}
        if repo_ids:
            report_rows = session.execute(
                select(ContributionReport.repo_id, func.max(ContributionReport.id))
                .where(ContributionReport.repo_id.in_(repo_ids))
                .group_by(ContributionReport.repo_id)
            ).all()
            report_ids = dict(report_rows)
        views = []
        for repo, analysis in rows:
            view = _repo_view(repo, analysis)
            # 最新批次周内首次入库 → 行内「新入库」徽标（不切批次 tab 也能一眼分新旧）
            view["new_batch"] = bool(latest_week and _iso_week(repo.first_seen_at) == latest_week)
            view["latest_report"] = (
                {"id": report_ids[repo.id], "status": "done", "created_at": None}
                if repo.id in report_ids
                else None
            )
            views.append(view)
        return {"repos": views, "total": total}


@router.get("/repos/{full_name:path}")
def repo_detail(full_name: str):
    with SessionLocal() as session:
        repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
        if repo is None:
            raise HTTPException(404, "仓库不在库中，请先刷新榜单")
        analysis = session.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
        report = session.scalar(
            select(ContributionReport)
            .where(ContributionReport.repo_id == repo.id)
            .order_by(ContributionReport.created_at.desc())
        )
        view = _repo_view(repo, analysis)
        view["readme_cached"] = bool(analysis and analysis.readme)
        view["report_md"] = analysis.report_md if analysis else ""
        view["latest_report"] = (
            {"id": report.id, "status": report.status, "created_at": report.created_at.isoformat()}
            if report
            else None
        )
        return view


# ---------- Issue 排行榜 ----------

# 「疑似已修复」的判定正则已挪到 services/scoring.py（fixed_hint 物化列共用）


@router.get("/issue-repos")
def issue_repos():
    """Issue 榜筛选器数据源：有 issue 的项目清单（含条数），按条数降序。"""
    with SessionLocal() as session:
        rows = session.execute(
            select(Repo.full_name, func.count(Issue.id))
            .join(Issue, Issue.repo_id == Repo.id)
            .group_by(Repo.id)
            .order_by(func.count(Issue.id).desc())
        ).all()
        return {"repos": [{"full_name": name, "issue_count": count} for name, count in rows]}


@router.get("/issues")
def list_issues(
    sort: str = "match",  # match=匹配度（LLM 分优先，缺则规则分） | rule | latest
    difficulty: str = "",  # 低|中|高|空=全部
    repo: str = "",  # 过滤仓库 full_name（支持子串模糊匹配）
    min_score: float = 0,
    deep_only: bool = False,  # 只看深读过的
    limit: int = 20,
    offset: int = 0,
    analyzed: str = "all",  # all | done（LLM 精筛过，match_score 非空）| todo
):
    """跨仓库 issue 排行榜：分页 + 精筛状态过滤；「疑似已修复」按物化列 SQL 沉底。"""
    with SessionLocal() as session:
        stmt = select(Issue, Repo).join(Repo, Issue.repo_id == Repo.id)
        if difficulty:
            stmt = stmt.where(Issue.difficulty == difficulty)
        if repo:
            stmt = stmt.where(Repo.full_name.like(f"%{repo}%"))
        if deep_only:
            stmt = stmt.where(Issue.deep_read.is_(True))
        if analyzed == "done":
            stmt = stmt.where(Issue.match_score.is_not(None))
        elif analyzed == "todo":
            stmt = stmt.where(Issue.match_score.is_(None))
        # 排序键：LLM 匹配分缺失时退回规则预分；match 排序下 LLM 已评分的恒优先（两套口径不混排）；
        # 「疑似已修复」经物化列 fixed_hint 沉底（latest 是显式时间视图，保持纯时间序）
        eff_score = func.coalesce(Issue.match_score, Issue.rule_match_score)
        stmt = stmt.where(eff_score >= min_score)
        if sort == "rule":
            stmt = stmt.order_by(Issue.fixed_hint.asc(), Issue.rule_match_score.desc(), Issue.id.desc())
        elif sort == "latest":
            stmt = stmt.order_by(Issue.updated_at.desc(), Issue.id.desc())
        else:
            stmt = stmt.order_by(
                Issue.fixed_hint.asc(), Issue.match_score.is_(None).asc(), eff_score.desc(), Issue.id.desc()
            )
        total = session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = session.execute(stmt.limit(min(limit, 300)).offset(max(offset, 0))).all()
        return {
            "issues": [
                {
                    "id": issue.id,
                    "repo": r.full_name,
                    "number": issue.number,
                    "title": issue.title,
                    "url": issue.url,
                    "labels": issue.labels or [],
                    "difficulty": issue.difficulty,
                    "match_score": issue.match_score,
                    "rule_match_score": issue.rule_match_score,
                    "effective_score": round(float(issue.match_score if issue.match_score is not None
                                                     else issue.rule_match_score), 1),
                    "summary": issue.summary,
                    "action": issue.action,
                    "fixed_hint": issue.fixed_hint,
                    "state": issue.state,  # open|closed（复查节点回填）
                    "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
                    "fixed_by_pr": issue.fixed_by_pr,  # 已合并的修复 PR 号（深读查 timeline）
                    "body_excerpt": issue.body_excerpt,
                    "fit_reason": issue.fit_reason,
                    "screen_reason": issue.screen_reason,
                    "deep_read": issue.deep_read,
                    "learning_status": issue.learning_status,
                    "comments_count": issue.comments_count,
                    "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
                }
                for issue, r in rows
            ],
            "total": total,
        }


# ---------- 任务触发 ----------

class ContributeRequest(BaseModel):
    repo_ids: list[int]


def _submit(task_type: str, coro_fn, *args):
    """把流水线包成 TaskManager 需要的 coro_factory，并管理 GitHubClient 生命周期。"""
    payload = {"args": [str(a) for a in args]}

    async def _run(task_id: str, progress, log=None) -> dict:
        github = GitHubClient()
        try:
            return await coro_fn(github, *args, task_id, progress)
        finally:
            await github.close()

    return manager.submit(task_type, _run, payload)


@router.post("/refresh")
async def refresh():
    task_id = _submit("refresh", refresh_pipeline)
    return {"task_id": task_id}


@router.post("/contributions")
async def contribute(req: ContributeRequest):
    if not req.repo_ids:
        raise HTTPException(400, "请先勾选项目")
    if len(req.repo_ids) > MAX_CONTRIB_REPOS:
        raise HTTPException(400, f"每次最多分析 {MAX_CONTRIB_REPOS} 个项目")
    settings = get_settings()
    if not settings.llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    task_id = _submit("contribution", contribution_pipeline, req.repo_ids)
    return {"task_id": task_id}


@router.get("/tasks")
def tasks(limit: int = 20):
    return {"tasks": manager.list(limit)}


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    """单个任务（含 logs 时间线）。列表接口不带 logs 省流量，前端定向轮询与刷新恢复走这里。"""
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return {"task": task}


class AnswerRequest(BaseModel):
    """waiting 任务的应答请求：{id, answers} 或 {id, cancel: true}。

    三字段全 typing.Any、类型与形状校验都在端点内手工做——严格模型会把 id 传 int、
    answers 传字符串等变成 FastAPI 的 422 而非契约的 400。
    """

    id: Any = None
    answers: Any = None  # [{"question": str, "answer": str}]，元素形状端点内校验
    cancel: Any = None


@router.post("/tasks/{task_id}/answer")
def answer_task(task_id: str, req: AnswerRequest):
    """投递用户对 waiting 任务的应答（问题桥的 HTTP 侧入口）。

    只投递 + 留痕，**不写 status、不清 pending_question**——waiting→running 的翻转与
    pq 清除是工具侧 execute 清场的单写者职责（AD-1），端点抢写会在竞态窗口里复活终态。
    sync def：deliver/_append_log 是同步 SQLite I/O，走 FastAPI 线程池，不占事件循环。
    检查次序：400 形状/类型 → 404 → 409 非 waiting → 409 无桥 → 409 无等待问题 →
    409 id 不匹配 → TOCTOU 复核 → deliver False（已应答/已关闭/已过期）则 409——
    超时后迟到的答案不得误报 200。
    """
    if not isinstance(req.id, str) or not req.id.strip():
        raise HTTPException(400, "缺少问题 id")
    if req.cancel is not None and not isinstance(req.cancel, bool):
        raise HTTPException(400, "cancel 须为布尔值")
    if req.answers is not None and not isinstance(req.answers, list):
        raise HTTPException(400, "answers 须为数组")
    is_cancel = req.cancel is True  # cancel:false 视同缺省，绝不当取消执行
    has_answers = req.answers is not None
    if not is_cancel and not has_answers:  # 两无（false/缺省都不算已选 cancel）
        raise HTTPException(400, "answers 与 cancel 必须二选一")
    if req.cancel is not None and has_answers:  # 两有（false 与 answers 同传也是违例）
        raise HTTPException(400, "answers 与 cancel 必须二选一")
    if has_answers:
        if not req.answers:
            raise HTTPException(400, "answers 不能为空")
        if len(req.answers) > 4:
            raise HTTPException(400, "answers 最多 4 条（与问题数同口径）")
        for e in req.answers:
            if (not isinstance(e, dict)
                    or not isinstance(e.get("question"), str) or not e["question"].strip()
                    or not isinstance(e.get("answer"), str) or not e["answer"].strip()):
                raise HTTPException(400, "answers 元素须为 {question, answer} 且均为非空字符串")

    with SessionLocal() as s:
        row = s.get(TaskRun, task_id)
    if row is None:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    if row.status != "waiting":
        raise HTTPException(409, "任务不在等待应答状态")
    bridge = askuser.get_bridge(task_id)
    if bridge is None:  # 重启僵尸（桥是进程内的）或竞态窗口：问题已自然结束
        raise HTTPException(409, "应答桥不存在：任务可能已重启或问题已结束")
    pending = (row.payload or {}).get("pending_question")
    if not isinstance(pending, dict):
        raise HTTPException(409, "当前没有等待中的问题")
    if pending.get("id") != req.id:
        raise HTTPException(409, "应答 id 与当前等待的问题不匹配")
    # TOCTOU 复核：读行与取桥之间问题可能已轮替（超时→重问换新桥新 pq.id），旧 id 会把
    # 答案 latch 进下一问的桥——新鲜读一次，状态与 id 都对得上才投递
    with SessionLocal() as s:
        fresh = s.get(TaskRun, task_id)
    fresh_pending = (fresh.payload or {}).get("pending_question") if fresh is not None else None
    if (fresh is None or fresh.status != "waiting"
            or not isinstance(fresh_pending, dict) or fresh_pending.get("id") != req.id):
        raise HTTPException(409, "问题已轮替或已结束，请刷新后重试")
    if not bridge.deliver("cancelled" if is_cancel else "answered", req.answers):
        raise HTTPException(409, "该问题已应答、已关闭或已过期，不能再投递")

    # 投递是事实源，留痕尽力而为：失败不能 500（客户端重试会撞上面的 409）
    try:
        if is_cancel:
            _append_log(task_id, "⛔ 用户取消了应答")
        else:  # 摘要口径与 ❓ preview 一致：逐条 join 后截 120 字符
            summary = "；".join(f"{e['question']} => {e['answer']}" for e in req.answers)[:120]
            _append_log(task_id, f"✅ 应答：{summary}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


# ---------- 报告 ----------

@router.get("/reports/{report_id}")
def get_report(report_id: int):
    with SessionLocal() as session:
        report = session.get(ContributionReport, report_id)
        if report is None:
            raise HTTPException(404, "报告不存在")
        repo = session.get(Repo, report.repo_id)
        return {
            "id": report.id,
            "repo": repo.full_name if repo else "?",
            "status": report.status,
            "repo_verdict": report.repo_verdict or {},
            "issues": report.issues or [],
            "stats": report.stats or {},
            "error": report.error,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "finished_at": report.finished_at.isoformat() if report.finished_at else None,
        }


# ---------- 标签 / 定向行业分析 ----------

@router.get("/tags")
def list_tags():
    """项目标签汇总（tag → 项目数），按热度降序；行业分析与全量打标的结果都在这。"""
    with SessionLocal() as session:
        rows = session.execute(select(Repo.tags)).scalars().all()
    counter: dict[str, int] = {}
    for tags in rows:
        for t in tags or []:
            counter[t] = counter.get(t, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tags": [{"tag": t, "count": c} for t, c in ranked]}


@router.get("/industries")
def list_industries():
    """历史行业分析报告清单（不含正文全文，详情走 /industries/{id}）。"""
    with SessionLocal() as session:
        rows = session.execute(
            select(IndustryReport).order_by(IndustryReport.created_at.desc())
        ).scalars().all()
        return {
            "industries": [
                {
                    "id": r.id,
                    "name": r.name,
                    # 项目上实际打的 canonical 标签（「看项目」用它筛选）；老报告 stats 无 tag 则回退报告名
                    "tag": (r.stats or {}).get("tag") or r.name,
                    "keywords": r.keywords or [],
                    "stats": r.stats or {},
                    "model": r.model,
                    "project_count": len(r.projects or []),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        }


@router.delete("/industries/{industry_id}")
def delete_industry(industry_id: int):
    """删除一份行业报告（只删报告记录；repos.tags 是累积的知识，不动）。"""
    with SessionLocal() as session:
        row = session.get(IndustryReport, industry_id)
        if row is None:
            raise HTTPException(404, "行业报告不存在")
        session.delete(row)
        session.commit()
    return {"ok": True}


class IndustryParseRequest(BaseModel):
    text: str


@router.post("/industries/parse")
async def parse_industry_input(req: IndustryParseRequest):
    """行业分析输入解析（同步快接口，2-5 秒）：拆方向/项目 + 方向归一标签 + 项目定位候选。
    注册顺序须在 /industries/{industry_id} 之前，否则 "parse" 会被 int 路径参数吃掉。"""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "请输入内容（方向词 + 项目名，如：agent运行时 pi agentScope-java）")
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    github = GitHubClient()
    try:
        return await parse_input(github, text)
    finally:
        await github.close()


@router.get("/industries/{industry_id}")
def industry_detail(industry_id: int):
    with SessionLocal() as session:
        r = session.get(IndustryReport, industry_id)
        if r is None:
            raise HTTPException(404, "行业报告不存在")
        return {
            "id": r.id,
            "name": r.name,
            "keywords": r.keywords or [],
            "overview_md": r.overview_md,
            "projects": r.projects or [],
            "stats": r.stats or {},
            "model": r.model,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }


class IndustryRunRequest(BaseModel):
    directions: list[dict]  # [{raw: 用户原词, tag: 归一后 canonical}]
    repos: list[str] = []  # 种子项目 full_name（确认面板选定的）


@router.post("/industries")
async def create_industry(req: IndustryRunRequest):
    """定向行业分析（确认面板提交）：多方向串行跑完整流水线，种子项目无条件并入各方向候选。"""
    directions = [
        {"raw": str(d.get("raw", "")).strip(),
         "tag": str(d.get("tag", "")).strip() or str(d.get("raw", "")).strip()}
        for d in req.directions if str(d.get("raw", "")).strip()
    ]
    if not directions:
        raise HTTPException(400, "至少保留一个方向")
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    seeds = [r.strip() for r in req.repos if "/" in r.strip()]
    task_id = _submit("industry", multi_industry_pipeline, directions, seeds)
    return {"task_id": task_id}


@router.post("/tags/auto")
async def auto_tag():
    """全量自动分类：LLM 先提出分类体系，再给库内全部项目打标签。"""
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    task_id = _submit("tagging", tagging_pipeline)
    return {"task_id": task_id}


class AnalyzeRequest(BaseModel):
    repo_ids: list[int]


@router.post("/repos/analyze")
async def analyze_repos(req: AnalyzeRequest):
    """批量精析选中的项目（「未精析」队列的入口，跑完即入「已精析」tab）。"""
    if not req.repo_ids:
        raise HTTPException(400, "请先勾选项目")
    if len(req.repo_ids) > MAX_ANALYZE_REPOS:
        raise HTTPException(400, f"每次最多精析 {MAX_ANALYZE_REPOS} 个项目")
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    task_id = _submit("analyze", analyze_pipeline, req.repo_ids)
    return {"task_id": task_id}


@router.post("/repos/translate")
async def translate_repos():
    """为 zh_desc 缺失的项目批量生成中文一句话简介（已是中文的直接回填，其余 LLM 翻译）。"""
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    task_id = _submit("translate", translate_pipeline)
    return {"task_id": task_id}


# ---------- 脚手架：一句话需求 → 框架匹配 → （采用 / 拆条选型 / 生成 zip） ----------

class ScaffoldCreateRequest(BaseModel):
    text: str


def _scaffold_summary(r: ScaffoldRequest) -> dict:
    """列表卡片视图：状态 + 一句话原文 + 当前阶段最有信息量的摘要。"""
    top = (r.framework_candidates or [{}])[0] if r.framework_candidates else {}
    return {
        "id": r.id,
        "raw_text": r.raw_text,
        "status": r.status,
        "domain": (r.need_brief or {}).get("domain", ""),
        "tech_stack": r.tech_stack,
        "adopt_repo": r.adopt_repo,
        "candidate_count": len(r.framework_candidates or []),
        # 已采用显示采用的仓库，其余显示适配度最高的候选
        "top_candidate": r.adopt_repo if (r.status == "done_adopt" and r.adopt_repo)
                         else top.get("full_name", ""),
        "top_fit": None if r.status == "done_adopt" else top.get("fit_score"),
        "item_count": len(r.items or []),
        "zip_ready": bool((r.build or {}).get("zip_url")),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _latest_build_task(request_id: int) -> dict | None:
    """该需求最近一次 scaffold_build 任务（创建时间倒序取首条）。
    任务失败不改 status 是平台约定（models.TaskRun 注释），需求停在 building 态——
    前端要区分「生成中」与「已失败可重试」只能看任务行。payload.request_id 是 JSON 列，
    int 走 LIKE 会误匹配 #1/#11，scaffold_build 任务量小，类型过滤后 Python 拣选。"""
    with SessionLocal() as session:
        rows = (session.query(TaskRun)
                .filter(TaskRun.type == "scaffold_build")
                .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
                .all())
    for row in rows:
        if (row.payload or {}).get("request_id") == request_id:
            return {"id": row.id, "status": row.status, "error": row.error or ""}
    return None


def _scaffold_detail(r: ScaffoldRequest) -> dict:
    return {
        **_scaffold_summary(r),
        "need_brief": r.need_brief or {},
        "framework_candidates": r.framework_candidates or [],
        "items": r.items or [],
        "adopt_report_md": r.adopt_report_md,
        "build": r.build or {},
        "build_task": _latest_build_task(r.id),
    }


@router.post("/scaffold/requests")
async def create_scaffold_request(req: ScaffoldCreateRequest):
    """一句话需求 → 创建记录并提交整体匹配任务（候选 5-8 个，双轨适配度）。"""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "请输入一句话需求（如：想做个 RSS 聚合站）")
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    with SessionLocal() as session:
        row = ScaffoldRequest(raw_text=text)
        session.add(row)
        session.commit()
        request_id = row.id
    task_id = _submit("scaffold_match", match_pipeline, request_id)
    _tag_scaffold_task(task_id, request_id)
    return {"task_id": task_id, "request_id": request_id}


@router.get("/scaffold/requests")
def list_scaffold_requests(limit: int = 20, offset: int = 0):
    """脚手架需求卡片列表（分页 limit/offset + total，id 倒序）。"""
    with SessionLocal() as session:
        stmt = select(ScaffoldRequest)
        total = session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = session.execute(
            stmt.order_by(ScaffoldRequest.id.desc())
            .limit(min(limit, 100)).offset(max(offset, 0))
        ).scalars().all()
        return {"requests": [_scaffold_summary(r) for r in rows], "total": total}


@router.get("/scaffold/requests/{request_id}")
def scaffold_request_detail(request_id: int):
    with SessionLocal() as session:
        r = session.get(ScaffoldRequest, request_id)
        if r is None:
            raise HTTPException(404, "脚手架需求不存在")
        return _scaffold_detail(r)


class ScaffoldMatchRequest(BaseModel):
    text: str = ""  # 空 = 用原话重跑


@router.post("/scaffold/requests/{request_id}/match")
async def rematch_scaffold_request(request_id: int, req: ScaffoldMatchRequest):
    """改话重跑匹配（回退用）：更新原话、清掉本阶段产出、重置 matching 重新提交。"""
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    text = req.text.strip()
    with SessionLocal() as session:
        row = session.get(ScaffoldRequest, request_id)
        if row is None:
            raise HTTPException(404, "脚手架需求不存在")
        if row.status == "building":
            raise HTTPException(400, "脚手架生成中，请稍后再试")
        if text:
            row.raw_text = text
        row.status = "matching"
        row.need_brief = {}
        row.framework_candidates = []
        session.commit()
    task_id = _submit("scaffold_match", match_pipeline, request_id)
    _tag_scaffold_task(task_id, request_id)
    return {"task_id": task_id, "request_id": request_id}


def _tag_scaffold_task(task_id: str, request_id: int) -> None:
    """任务创建即挂 request_id（面板标识「🏗 需求 #n」运行中就可见，不用等管线收尾）。"""
    with SessionLocal() as session:
        run = session.get(TaskRun, task_id)
        if run is not None:
            run.payload = {**(run.payload or {}), "request_id": request_id}
            session.commit()


class ScaffoldAdoptRequest(BaseModel):
    full_name: str


@router.post("/scaffold/requests/{request_id}/adopt")
async def adopt_scaffold_request(request_id: int, req: ScaffoldAdoptRequest):
    """采用候选框架：置 done_adopt（终态）+ 同步生成评估报告（拉 README + 单次 LLM，5-15s）。
    同一仓库重复采用幂等（报告不重生成）；换仓库采用 = 重新生成报告。"""
    full_name = req.full_name.strip()
    with SessionLocal() as session:
        row = session.get(ScaffoldRequest, request_id)
        if row is None:
            raise HTTPException(404, "脚手架需求不存在")
        if row.status == "done_adopt" and row.adopt_repo == full_name and row.adopt_report_md:
            return {"ok": True, "request_id": request_id, "cached": True}  # 幂等
        cand = next((c for c in (row.framework_candidates or [])
                     if c.get("full_name") == full_name), None)
        if cand is None:
            raise HTTPException(400, f"{full_name} 不在该需求的候选清单中")
        if row.status == "building":
            raise HTTPException(400, "脚手架生成中，请稍后再试")
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")

    readme = ""
    github = GitHubClient()
    try:
        try:
            readme = await github.get_readme(full_name)
        except Exception:  # noqa: BLE001 README 拿不到也能出报告（凭候选元数据）
            pass
    finally:
        await github.close()
    llm = LLMClient()
    try:
        report_md = await scaffold_adopt_report(llm, row.raw_text, row.need_brief or {}, cand, readme)
    finally:
        await llm.close()

    with SessionLocal() as session:
        managed = session.get(ScaffoldRequest, request_id)
        managed.adopt_repo = full_name
        managed.adopt_report_md = report_md
        managed.status = "done_adopt"
        session.commit()
    return {"ok": True, "request_id": request_id, "cached": False}


@router.post("/scaffold/requests/{request_id}/split")
async def split_scaffold_request(request_id: int):
    """拆条（同步 2-10s）：需求 → 3-8 技术条目 + 主技术栈；同一句话重复拆条命中缓存秒回。"""
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    with SessionLocal() as session:
        row = session.get(ScaffoldRequest, request_id)
        if row is None:
            raise HTTPException(404, "脚手架需求不存在")
        if row.status not in ("matched", "split"):
            raise HTTPException(400, f"当前状态 {row.status} 不能拆条（需先匹配完成）")
    try:
        return await split_items(request_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


class ScaffoldItemsRequest(BaseModel):
    items: list[dict]  # [{name, desc, keywords}]
    tech_stack: str = ""


@router.put("/scaffold/requests/{request_id}/items")
async def confirm_scaffold_items(request_id: int, req: ScaffoldItemsRequest):
    """条目确认 → 提交条目级选型任务（每条目检索候选 + 双轨评分）。
    重复调用 = 改条目重选型（fit 缓存兜住重复评分成本）。"""
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY）")
    cleaned: list[dict] = []
    for it in req.items:
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        cleaned.append({
            "no": len(cleaned) + 1, "name": name,
            "desc": str(it.get("desc", "")).strip(),
            "keywords": [str(k).strip() for k in (it.get("keywords") or []) if str(k).strip()],
            "candidates": [], "selected": None, "self_dev": False,
        })
    if not cleaned:
        raise HTTPException(400, "至少保留一个条目（名称不能为空）")
    with SessionLocal() as session:
        row = session.get(ScaffoldRequest, request_id)
        if row is None:
            raise HTTPException(404, "脚手架需求不存在")
        if row.status not in ("split", "selecting", "selected"):
            raise HTTPException(400, f"当前状态 {row.status} 不能确认条目（需先拆条）")
        row.items = cleaned
        row.status = "selecting"
        if req.tech_stack.strip():
            row.tech_stack = req.tech_stack.strip()
        session.commit()
    task_id = _submit("scaffold_select", select_pipeline, request_id)
    _tag_scaffold_task(task_id, request_id)
    return {"task_id": task_id, "request_id": request_id}


class ScaffoldSelectRequest(BaseModel):
    selections: list[dict]  # [{no, full_name | null}]（null/空 = 该条目自研）


@router.post("/scaffold/requests/{request_id}/select")
async def select_scaffold_components(request_id: int, req: ScaffoldSelectRequest):
    """逐条选型提交 → 生成任务（Agent 整合产出 zip）。同组合重复提交命中产物缓存不重跑。"""
    with SessionLocal() as session:
        row = session.get(ScaffoldRequest, request_id)
        if row is None:
            raise HTTPException(404, "脚手架需求不存在")
        if row.status not in ("selected", "building", "built"):
            raise HTTPException(400, f"当前状态 {row.status} 不能提交选型（需先完成条目候选）")
        items = [dict(it) for it in (row.items or [])]
        # 回写每条目的选择：full_name 必须在该条目候选里；空 = 自研
        by_no = {s.get("no"): (str(s.get("full_name") or "").strip() or None) for s in req.selections}
        for it in items:
            choice = by_no.get(it.get("no"))
            if choice is None:
                it["self_dev"] = True
                it["selected"] = None
                continue
            if not any(c.get("full_name") == choice for c in (it.get("candidates") or [])):
                raise HTTPException(400, f"{choice} 不是条目「{it.get('name')}」的候选")
            it["selected"] = choice
            it["self_dev"] = False
        missing = [it.get("name") for it in items if not it.get("selected") and not it.get("self_dev")]
        if missing:
            raise HTTPException(400, f"条目未选完：{'、'.join(missing)}（不选即自研，请显式标记）")
        row.items = items
        row.status = "building"
        session.commit()
    task_id = _submit("scaffold_build", build_pipeline, request_id)
    _tag_scaffold_task(task_id, request_id)
    return {"task_id": task_id, "request_id": request_id}


# ---------- 配置状态（前端提示用） ----------

@router.get("/config")
def config_status():
    s = get_settings()
    return {
        "github_token": bool(s.github_token),
        "llm": s.llm_configured,
        "search": s.search_configured,
        "top_n_llm": s.top_n_llm,
        "sched_hour": s.sched_hour,
        "user_profile": s.user_profile,
        "user_skills": [x.strip() for x in s.user_skills.split(",") if x.strip()],
    }


# ---------- 通用技能调用 ----------

@router.get("/skills")
def skills():
    """现扫磁盘列出可用技能（项目级 + 用户级）——新增/修改 SKILL.md 即时生效。"""
    from .services.skill_runner import list_skills

    return {"skills": list_skills()}


class SkillInvokeRequest(BaseModel):
    args: str = ""


@router.post("/skills/{name}/invoke")
async def invoke_skill(name: str, req: SkillInvokeRequest):
    """以内置 Agent SDK 执行一个技能（正文注入 prompt），进度走任务轮询。

    注意必须是 async def：manager.submit 里用 asyncio.create_task，
    同步 def 会被丢进线程池导致「no running event loop」。
    """
    from .config import get_settings
    from .services.skill_runner import list_skills, run_skill

    skill = next((s for s in list_skills() if s["name"] == name), None)
    if skill is None:
        raise HTTPException(404, f"技能 {name} 不存在（检查 .claude/skills/ 或 ~/.claude/skills/）")
    if skill.get("requires") == "local":
        raise HTTPException(400, f"技能 /{name} 需要本地文件环境（requires: local），请在 Claude Code 中运行")
    timeout = get_settings().skill_run_timeout

    async def _run(task_id: str, progress, log=None) -> dict:
        result = await run_skill(name, req.args, task_id, progress, log=log, timeout=timeout)
        # TaskManager 不存返回值：结果自己挂到任务 payload 上，前端从 /api/tasks 取
        with SessionLocal() as s:
            row = s.get(TaskRun, task_id)
            row.payload = {**(row.payload or {}), "result": result}
            s.commit()
        return result

    task_id = manager.submit("skill", _run, payload={"skill": name, "args": req.args})
    return {"task_id": task_id}


class AgentRunRequest(BaseModel):
    """页面 AI 命令栏：自由指令，/开头解析为技能。"""
    prompt: str


# ---------- AI 分析沉淀入榜单 ----------

_GITHUB_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?=$|[/\s.,)'\"）」])")
_OWNER_REPO_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]{2,})(?![\w./-])")
_KNOWN_DIMS = ("enterprise_potential", "match", "learning_value",
               "star_momentum", "activity", "maintenance")


def _repo_candidates(prompt: str) -> list[str]:
    """从指令里提取候选 owner/repo：GitHub URL 优先，其次裸 owner/repo 词（交给 GitHub 校验）。"""
    seen: list[str] = []
    for m in _GITHUB_URL_RE.finditer(prompt):
        seen.append(m.group(1))
    for m in _OWNER_REPO_RE.finditer(prompt):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen[:3]


def _parse_llm_scores(result_text: str) -> dict:
    """按系统提示词约定，取报告末尾的 ```json 六维评分块。"""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", result_text or "", re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and any(k in data for k in _KNOWN_DIMS):
            return {k: v for k, v in data.items() if k in _KNOWN_DIMS and isinstance(v, dict)}
    return {}


async def _persist_ai_analysis(prompt: str, result_text: str, progress, log=None) -> str | None:
    """指令若指向某个 GitHub 项目，把分析沉淀进榜单（Repo + Analysis），返回 full_name。

    候选逐个到 GitHub 校验，全部落空则静默放弃——沉淀失败不能拖垮命令任务本身。
    """
    from datetime import datetime, timezone

    candidates = _repo_candidates(prompt)
    if not candidates:
        return None
    scores = _parse_llm_scores(result_text)
    gh = GitHubClient()
    try:
        meta = None
        for cand in candidates:
            try:
                raw = await gh.get_repo(cand)
            except Exception:  # noqa: BLE001  404/限流都换下一个候选
                continue
            meta = {
                "full_name": raw["full_name"],
                "description": raw.get("description") or "",
                "language": raw.get("language") or "",
                "stars": raw.get("stargazers_count") or 0,
                "topics": raw.get("topics") or [],
                "homepage": raw.get("homepage") or "",
                "license": (raw.get("license") or {}).get("spdx_id") or "",
                "open_issues": raw.get("open_issues_count") or 0,
                "pushed_at": raw.get("pushed_at"),
                "github_created_at": raw.get("created_at"),
            }
            break
        if meta is None:
            return None

        rule, rule_detail = scoring.score_repo(meta)
        total = scoring.total_score(rule_detail, scores)
        with SessionLocal() as s:
            repo = _upsert_repo(s, meta)
            s.flush()  # 新仓库要拿到自增 id 才能挂 Analysis
            repo.rule_score = rule
            repo.rule_detail = rule_detail
            repo.total_score = total
            analysis = s.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
            if analysis is None:
                analysis = Analysis(repo_id=repo.id)
                s.add(analysis)
            analysis.status = "done"
            analysis.core_idea = result_text.strip()[:300]
            if scores:
                analysis.llm_scores = scores
            analysis.model = get_settings().llm_model
            analysis.note = "AI 命令分析"
            analysis.report_md = result_text.strip()
            analysis.analyzed_at = datetime.now(timezone.utc)
            s.commit()
        (log or progress)(f"已沉淀入榜单：{meta['full_name']}（综合分 {total}）")
        return meta["full_name"]
    finally:
        await gh.close()


@router.post("/agent/run")
async def agent_run(req: AgentRunRequest):
    """以内置 Agent SDK 执行一段自由指令，进度与结果走任务轮询，机制同 /skills/{name}/invoke。"""
    from .services.skill_runner import run_prompt

    if not req.prompt.strip():
        raise HTTPException(400, "prompt 不能为空")
    timeout = get_settings().skill_run_timeout

    async def _run(task_id: str, progress, log=None) -> dict:
        emit = log or progress
        result = await run_prompt(req.prompt.strip(), task_id, progress, log=log, timeout=timeout)
        # 指令若指向某个 GitHub 项目，把分析沉淀进榜单（失败不影响命令结果）
        try:
            result["repo"] = await _persist_ai_analysis(req.prompt, result.get("result") or "",
                                                        progress, log=log)
        except Exception as e:  # noqa: BLE001
            emit(f"分析结果入库失败（不影响命令结果）：{e}")
        with SessionLocal() as s:
            row = s.get(TaskRun, task_id)
            row.payload = {**(row.payload or {}), "result": result}
            s.commit()
        return result

    task_id = manager.submit("agent", _run, payload={"prompt": req.prompt.strip()})
    return {"task_id": task_id}


# ---------- 学习闭环 ----------

@router.get("/learning-context/{issue_id}")
def learning_context(issue_id: int):
    """/tech 一次取全：issue + 仓库元数据 + README + 贡献报告 + 已有课程提示。"""
    with SessionLocal() as session:
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(404, "issue 不存在")
        repo = session.get(Repo, issue.repo_id)
        analysis = session.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
        report = session.scalar(
            select(ContributionReport)
            .where(ContributionReport.repo_id == repo.id)
            .order_by(ContributionReport.created_at.desc())
        )
        report_issue = next(
            (i for i in (report.issues or []) if i.get("number") == issue.number), None
        ) if report else None
        existing = session.scalar(
            select(Course).where(Course.issue_id == issue.id)
            .order_by(Course.created_at.desc())
        )
        return {
            "issue": {
                "id": issue.id,
                "number": issue.number,
                "title": issue.title,
                "url": issue.url,
                "labels": issue.labels or [],
                "difficulty": issue.difficulty,
                "summary": issue.summary,
                "action": issue.action,
                "fit_reason": issue.fit_reason,
                "body_excerpt": issue.body_excerpt,
                "comments_count": issue.comments_count,
                "learning_status": issue.learning_status,
            },
            "repo": {
                "id": repo.id,
                "full_name": repo.full_name,
                "description": repo.description,
                "language": repo.language,
                "topics": repo.topics or [],
                "homepage": repo.homepage,
                "license": repo.license,
                "stars": repo.stars,
                "forks": repo.forks,
                "open_issues": repo.open_issues,
                "contributors_count": repo.contributors_count,
            },
            "analysis": None if analysis is None else {
                "core_idea": analysis.core_idea,
                "enterprise_cases": analysis.enterprise_cases or [],
                "llm_scores": analysis.llm_scores or {},
                "readme": analysis.readme,  # 生成「项目导览」章的底料，可能很长
            },
            "report": None if report is None else {
                "repo_verdict": report.repo_verdict or {},
                "this_issue": report_issue,  # 深读报告里对该 issue 的背景/切入方式（若有）
            },
            "existing_course": None if existing is None else {
                "id": existing.id,
                "title": existing.title,
                "status": existing.status,
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
            },
        }


@router.get("/learning-context/repo/{repo_id}")
def learning_context_repo(repo_id: int):
    """/tech-repo 一次取全：仓库元数据 + 精析（含 README 全文）+ 最新贡献报告 + 高分 issue。"""
    with SessionLocal() as session:
        repo = session.get(Repo, repo_id)
        if repo is None:
            raise HTTPException(404, "项目不存在")
        analysis = session.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
        report = session.scalar(
            select(ContributionReport)
            .where(ContributionReport.repo_id == repo.id)
            .order_by(ContributionReport.created_at.desc())
        )
        top_issues = session.execute(
            select(Issue).where(Issue.repo_id == repo.id, Issue.match_score.is_not(None))
            .order_by(Issue.match_score.desc(), Issue.id.desc()).limit(5)
        ).scalars().all()
        existing = session.scalars(
            select(Course).where(Course.source_type == "repo", Course.repo_id == repo.id)
            .order_by(Course.created_at.desc())
        ).all()
        return {
            "repo": {
                "id": repo.id,
                "full_name": repo.full_name,
                "description": repo.description,
                "zh_desc": repo.zh_desc,
                "language": repo.language,
                "topics": repo.topics or [],
                "homepage": repo.homepage,
                "license": repo.license,
                "stars": repo.stars,
                "forks": repo.forks,
                "open_issues": repo.open_issues,
                "contributors_count": repo.contributors_count,
            },
            "analysis": None if analysis is None else {
                "core_idea": analysis.core_idea,
                "enterprise_cases": analysis.enterprise_cases or [],
                "llm_scores": analysis.llm_scores or {},
                "readme": analysis.readme,  # 「项目导览」章的底料，可能很长
            },
            "report": None if report is None else {
                "repo_verdict": report.repo_verdict or {},
                "stats": report.stats or {},
            },
            "top_issues": [{
                "id": i.id, "number": i.number, "title": i.title, "url": i.url,
                "difficulty": i.difficulty, "match_score": i.match_score,
                "summary": i.summary, "learning_status": i.learning_status,
            } for i in top_issues],
            "existing_courses": [{
                "id": c.id, "title": c.title, "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            } for c in existing[:5]],
        }


@router.get("/learning-context/book/{book_id}")
def learning_context_book(book_id: int):
    """/tech-book 一次取全：教材元数据 + 学习大纲（章 → 页范围 → 逐页文本文件清单）。"""
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None:
            raise HTTPException(404, "教材不存在")
        if book.status != "ready":
            raise HTTPException(409, f"教材尚未解析完成（{book.status}）")
        chapters = []
        for c in (book.outline or {}).get("chapters", []):
            start, end = int(c.get("start") or 1), int(c.get("end") or book.pages)
            chapters.append({
                **c,
                "files": [f"p{n:04d}.txt" for n in range(start, min(end, book.pages) + 1)],
            })
        existing = session.scalars(
            select(Course).where(Course.source_type == "book", Course.book_id == book.id)
            .order_by(Course.created_at.desc())
        ).all()
        return {
            "book": {
                "id": book.id,
                "title": book.title,
                "filename": book.filename,
                "pages": book.pages,
                "stats": book.stats or {},
            },
            "chapters": chapters,
            "text_dir": f"uploads/books/{book.id}/text",  # ReadFile 白名单内，页文件 pNNNN.txt
            "existing_courses": [{
                "id": c.id, "title": c.title, "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            } for c in existing[:10]],
        }


def _course_view(session, course: Course, full: bool = False) -> dict:
    """课程视图：lessons 摊平并附每节 quiz 提交进度。

    full=True 时附带发布的逐文件明细（详情页用；列表里不带，避免每门课都驮一份清单）。
    来源四类（source_type）：issue / repo / book / import——卡片来源行按它分支渲染。
    """
    results = session.scalars(
        select(QuizResult).where(QuizResult.course_id == course.id)
    ).all()
    by_lesson = {r.lesson_id: r for r in results}
    lessons = []
    for l in course.lessons or []:
        r = by_lesson.get(l.get("lesson_id"))
        lessons.append({
            **l,
            "submitted": r is not None,
            "score": r.score if r else None,
            "total": r.total if r else None,
            "submitted_at": r.created_at.isoformat() if r else None,
        })
    pub = course.publish or {}
    publish = {
        k: pub[k]
        for k in ("published_at", "folder", "entry_url", "file_count", "total_bytes", "renamed")
        if k in pub
    }
    if full and "files" in pub:
        publish["files"] = pub["files"]
    source_type = course.source_type or "issue"
    repo_name = course.repo.full_name if course.repo else None
    book = session.get(Book, course.book_id) if course.book_id else None
    if source_type == "issue":
        source_label = f"{repo_name or '?'} #{course.issue.number}" if course.issue else (repo_name or "issue")
    elif source_type == "repo":
        source_label = repo_name or "项目"
    elif source_type == "book":
        source_label = book.title if book else "教材"
    else:
        source_label = "导入的课程"
    return {
        "id": course.id,
        "user_id": course.user_id,
        "source_type": source_type,
        "repo": repo_name,
        "repo_id": course.repo_id,
        "issue_id": course.issue_id,
        "issue_number": course.issue.number if course.issue else None,
        "issue_title": course.issue.title if course.issue else "",
        "book_id": course.book_id or None,
        "book_title": book.title if book else None,
        "series": course.series or None,
        "source_label": source_label,
        "title": course.title,
        "status": course.status,
        "created_at": course.created_at.isoformat() if course.created_at else None,
        "lessons": lessons,
        "done_lessons": sum(1 for l in lessons if l["submitted"]),
        "total_lessons": len(lessons),
        # 发布状态（逐文件明细只在详情里带）
        "published": bool(pub.get("entry_url")),
        "publish": publish or None,
    }


@router.get("/courses")
def list_courses():
    with SessionLocal() as session:
        courses = session.scalars(
            select(Course).order_by(Course.created_at.desc())
        ).all()
        # 系列下拉选项：去重、按最近一次使用在前（列表本就是创建时间倒序，首次出现即最新）
        series_options = []
        for c in courses:
            if c.series and c.series not in series_options:
                series_options.append(c.series)
        return {
            "courses": [_course_view(session, c) for c in courses],
            "series_options": series_options,
        }


@router.get("/courses/{course_id}")
def get_course(course_id: int):
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise HTTPException(404, "课程不存在")
        return _course_view(session, course, full=True)


class CourseCreate(BaseModel):
    title: str
    # [{"lesson_id", "title", "file", "quiz_count"}]
    lessons: list[dict]
    issue_id: int | None = None  # issue 课必填
    repo_id: int | None = None  # 项目课必填；issue 课自动取 issue 所属仓库
    book_id: int | None = None  # 教材课带书 id（软引用）
    source_type: str = "issue"  # issue|repo|book|import；空/缺省时按已给的 id 推断
    replace: bool = False  # 覆盖已有课程（连 quiz 记录一起清）——目前仅 issue 课支持


@router.post("/courses")
def create_course(req: CourseCreate):
    """注册课程元数据返回 course_id。课程 HTML 由 /tech、/tech-repo、/tech-book 写盘。

    source_type 缺省时按入参推断（issue_id→issue、repo_id→repo、book_id→book、都无→import）。
    issue 课副作用：issue → learning。repo/book/import 课不做联动（没有对应的学习状态机）。

    replace=True（覆盖重生成）：仅 issue 课支持，清掉该 issue 的旧课程记录（连 quiz）
    **和旧课程目录 courses/{旧id}/**——SDK 模式下 agent 没有删除权限，清理由平台做，
    本地与 web 两条路行为一致。repo/book 课想重来就另起新课（同源多课并存）。
    """
    source = req.source_type or ""
    if source not in ("issue", "repo", "book", "import"):
        source = ("issue" if req.issue_id else "repo" if req.repo_id
                  else "book" if req.book_id else "import")
    if not req.lessons:
        raise HTTPException(400, "lessons 不能为空")
    repo_id: int | None = req.repo_id
    issue_id: int | None = req.issue_id
    with SessionLocal() as session:
        if source == "issue":
            if req.issue_id is None:
                raise HTTPException(400, "issue 课必须带 issue_id")
            issue = session.get(Issue, req.issue_id)
            if issue is None:
                raise HTTPException(404, "issue 不存在")
            issue_id = issue.id
            repo_id = issue.repo_id
        elif source == "repo":
            if req.repo_id is None:
                raise HTTPException(400, "项目课必须带 repo_id")
            repo = session.get(Repo, req.repo_id)
            if repo is None:
                raise HTTPException(404, "项目不存在")
        elif source == "book" and req.book_id:
            if session.get(Book, req.book_id) is None:
                raise HTTPException(404, "教材不存在")
        old_ids: list[int] = []
        if req.replace and source == "issue" and issue_id is not None:
            old_ids = session.scalars(
                select(Course.id).where(Course.issue_id == issue_id)
            ).all()
            if old_ids:
                session.execute(delete(QuizResult).where(QuizResult.course_id.in_(old_ids)))
                session.execute(delete(Course).where(Course.id.in_(old_ids)))
        course = Course(
            user_id=1,
            source_type=source,
            repo_id=repo_id,
            issue_id=issue_id,
            book_id=req.book_id or 0,
            title=req.title,
            lessons=req.lessons,
            status="learning",
        )
        session.add(course)
        if source == "issue" and issue_id is not None:
            issue = session.get(Issue, issue_id)
            if issue is not None:
                issue.learning_status = "learning"
        session.commit()
    for old_id in old_ids:  # DB 已提交，目录清理失败只留孤儿文件、不影响新课
        shutil.rmtree(course_publish.course_dir_for(old_id), ignore_errors=True)
    return {"course_id": course.id, "status": course.status}


class LessonDeleteRequest(BaseModel):
    lesson_ids: list[str]


@router.post("/courses/{course_id}/lessons/delete")
def delete_lessons(course_id: int, req: LessonDeleteRequest):
    """删除课程里选中的小节：清单、课件 HTML 文件、quiz 存档一并清。

    删空（含一次全选）则整门课连 courses/{id}/ 目录一起删——导入包里常有凑数页，
    删到最后一节时课程本身也没了存在意义。发布目录（/published）是静态分享副本，不动。
    """
    if not req.lesson_ids:
        raise HTTPException(400, "lesson_ids 不能为空")
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise HTTPException(404, "课程不存在")
        wanted = set(req.lesson_ids)
        kept = [l for l in (course.lessons or []) if l.get("lesson_id") not in wanted]
        removed = [l for l in (course.lessons or []) if l.get("lesson_id") in wanted]
        if not removed:
            raise HTTPException(404, "没有匹配的小节（可能已被删除，刷新列表）")
        course_dir = course_publish.course_dir_for(course_id)
        for l in removed:
            name = str(l.get("file") or "")
            if name and Path(name).name == name:  # 只删课程目录一层内的文件，防 lessons JSON 混进路径
                (course_dir / name).unlink(missing_ok=True)
            session.execute(
                delete(QuizResult).where(
                    QuizResult.course_id == course_id,
                    QuizResult.lesson_id == str(l.get("lesson_id") or ""),
                )
            )
        if not kept:  # 最后一节也删了：整门课（记录 + 目录）一并删
            session.delete(course)
            session.commit()
            shutil.rmtree(course_dir, ignore_errors=True)
            return {"deleted_lessons": len(removed), "course_deleted": True}
        course.lessons = kept
        session.commit()
        return {"deleted_lessons": len(removed), "course_deleted": False, "remaining_lessons": len(kept)}


class CoursePublish(BaseModel):
    prune: bool = False  # 顺带删掉该课程文件夹下本次没上传的旧文件（重生成课程后用）


@router.post("/courses/{course_id}/publish")
def publish_course(course_id: int, req: CoursePublish | None = None):
    """把 courses/{id}/ 发布到服务器本地磁盘（LOCAL_PUBLISH_DIR）。

    按课程分文件夹（{id}-{repo}-issue{n}-{课程标题}/），课件文件用中文课标题重命名，
    页面之间的相对链接同步改写，静态副本额外注入 CELESTIAL_PUBLISHED 标记。
    同 key 覆盖，可重复执行；失败如实报错，不写入半成品发布记录。
    """
    prune = bool(req.prune) if req else False
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise HTTPException(404, "课程不存在")
        try:
            manifest = course_publish.publish_course(course, prune=prune)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except StoreError as exc:  # noqa: BLE001 磁盘侧异常原样转达，便于排查权限/空间
            raise HTTPException(502, f"发布目录写入失败：{exc}") from exc
        except Exception as exc:
            raise HTTPException(502, f"发布失败：{exc}") from exc
        manifest["published_at"] = utcnow().isoformat()
        course.publish = manifest
        session.commit()
        return manifest


class QuizResultCreate(BaseModel):
    course_id: int
    lesson_id: str
    score: int
    total: int
    detail: list[dict] = []  # 每题对错明细


@router.post("/quiz-results")
def create_quiz_result(req: QuizResultCreate):
    """回传一节 quiz（页面即时反馈，平台只存档）。全部 lesson 均有提交 → 课程与 issue 置 done。"""
    with SessionLocal() as session:
        course = session.get(Course, req.course_id)
        if course is None:
            raise HTTPException(404, "课程不存在")
        lesson_ids = {l.get("lesson_id") for l in (course.lessons or [])}
        if req.lesson_id not in lesson_ids:
            raise HTTPException(400, f"lesson_id {req.lesson_id} 不属于课程 {course.id}")
        session.add(QuizResult(
            user_id=course.user_id,
            course_id=course.id,
            lesson_id=req.lesson_id,
            score=req.score,
            total=req.total,
            detail=req.detail,
        ))
        # SessionLocal 关了 autoflush，上面的 add 还没落库，这里手动并入再对账
        submitted = set(session.scalars(
            select(QuizResult.lesson_id).where(QuizResult.course_id == course.id)
        ).all()) | {req.lesson_id}
        finished = lesson_ids and lesson_ids <= submitted and course.status != "done"
        if finished:
            course.status = "done"
            issue = session.get(Issue, course.issue_id)
            if issue:
                issue.learning_status = "done"
        session.commit()
        return {"ok": True, "course_status": course.status}


# ---------- 教材（books）与教程导入 ----------

MAX_BOOK_BYTES = 200 * 1024 * 1024  # 教材 PDF 上限 200MB（几百页的影印教材够放）
MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 教程包上限 100MB


def _book_view(book: Book, with_outline: bool = False) -> dict:
    view = {
        "id": book.id,
        "title": book.title,
        "filename": book.filename,
        "pages": book.pages,
        "status": book.status,
        "note": book.note,
        "stats": book.stats or {},
        "created_at": book.created_at.isoformat() if book.created_at else None,
    }
    if with_outline:
        view["outline"] = book.outline or {}
    else:
        view["chapters"] = len((book.outline or {}).get("chapters", []))
    return view


@router.post("/books")
async def upload_book(file: UploadFile = File(...), title: str = Form("")):
    """上传教材 PDF：落盘 + 建记录 + 后台解析任务（抽文本/扫描页视觉转录/归纳大纲）。"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "只接受 .pdf 文件")
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > MAX_BOOK_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_BOOK_BYTES // 1024 // 1024}MB 上限")
    if not get_settings().llm_configured:
        raise HTTPException(400, "LLM 未配置（解析大纲与扫描页转录都依赖它）")
    safe_title = (title or "").strip()[:200]
    with SessionLocal() as session:
        book = Book(title=safe_title, filename=file.filename[:500], status="extracting")
        session.add(book)
        session.commit()
        book_id = book.id
    target_dir = book_dir(book_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "book.pdf").write_bytes(data)

    async def _run(task_id: str, progress, log):
        return await extract_pipeline(book_id, task_id, progress, log)

    task_id = manager.submit("book_extract", _run, payload={"book_id": book_id, "filename": book.filename})
    return {"book_id": book_id, "task_id": task_id, "status": book.status}


@router.get("/books")
def list_books():
    with SessionLocal() as session:
        books = session.scalars(select(Book).order_by(Book.created_at.desc())).all()
        return {"books": [_book_view(b) for b in books]}


@router.get("/books/{book_id}")
def get_book(book_id: int):
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None:
            raise HTTPException(404, "教材不存在")
        return _book_view(book, with_outline=True)


@router.delete("/books/{book_id}")
def delete_book(book_id: int):
    """删一本教材（记录 + PDF/文本文件）。已生成的课程不受影响，只丢书名回显。"""
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None:
            raise HTTPException(404, "教材不存在")
        session.delete(book)
        session.commit()
    shutil.rmtree(book_dir(book_id), ignore_errors=True)
    return {"ok": True}


class BookSeriesRequest(BaseModel):
    chapter_nos: list[int]  # 要生成章节课的章号（大纲里的 no）


@router.post("/books/{book_id}/series")
def create_book_series(book_id: int, req: BookSeriesRequest):
    """章节系列课：对选中章逐章跑 /tech-book（单章模式），一章一门课，串行不问询。"""
    chapter_nos = sorted({n for n in req.chapter_nos if n > 0})
    if not chapter_nos:
        raise HTTPException(400, "chapter_nos 不能为空")
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None:
            raise HTTPException(404, "教材不存在")
        if book.status != "ready":
            raise HTTPException(409, f"教材尚未解析完成（{book.status}），先等解析任务成功")
        known = {c.get("no") for c in (book.outline or {}).get("chapters", [])}
        unknown = [n for n in chapter_nos if n not in known]
        if unknown:
            raise HTTPException(400, f"章号 {unknown} 不在大纲里（共 {len(known)} 章）")

    async def _run(task_id: str, progress, log):
        return await series_pipeline(book_id, chapter_nos, task_id, progress, log)

    task_id = manager.submit("book_series", _run, payload={"book_id": book_id, "chapter_nos": chapter_nos})
    return {"task_id": task_id}


_QUIZ_SPEC_RE = re.compile(r'<script[^>]+id="quiz-spec"[^>]*>(\{.*?\})</script>', re.S)


def _parse_lesson_html(html: str, fallback_id: str) -> tuple[str, int]:
    """从课件 HTML 提取 (lesson_id, 题数)：优先 quiz-spec 内嵌的 lesson_id，回退文件名。

    导入的多是发布副本（课件已改中文名），文件名当 lesson_id 会对不上 quiz 回传——
    quiz-spec 里存着生成时的原始 lesson_id，以它为准。
    """
    m = _QUIZ_SPEC_RE.search(html)
    if m:
        try:
            spec = json.loads(m.group(1))
            questions = spec.get("questions") or []
            if isinstance(questions, list):
                return str(spec.get("lesson_id") or fallback_id), len(questions)
        except (ValueError, TypeError):
            pass
    return fallback_id, 0


def _rewrite_course_ids(html: str, new_id: int) -> str:
    """导入课的页面里写死了生成时的旧 course_id——统一改写成新 id（进度才能对上）。

    顺带剥掉发布副本注入的 CELESTIAL_PUBLISHED 标记（本地这份要正常回传 quiz）。
    """
    html = re.sub(r"window\.COURSE_ID\s*=\s*\d+", f"window.COURSE_ID = {new_id}", html)
    html = re.sub(r'("course_id"\s*:\s*)\d+', rf"\g<1>{new_id}", html)
    html = re.sub(r"<script>\s*window\.CELESTIAL_PUBLISHED\s*=\s*true;?\s*</script>\s*", "", html)
    return html


def _extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """逐成员解压：防 zip-slip 越界；未打 UTF-8 旗标的文件名按 cp437→gbk 重解码
    （Windows「发送到压缩文件夹」打出来的中文文件名是 GBK，直接 extractall 会乱码）。"""
    dest = dest.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if not info.flag_bits & 0x800:
            try:
                name = name.encode("cp437").decode("gbk")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        target = (dest / name).resolve()
        if not target.is_relative_to(dest):
            raise HTTPException(400, f"压缩包含越界路径：{info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _top_up_course_assets(course_dir: Path) -> None:
    """补齐缺省共享件：assets/quiz.js 与 assets/style.css。

    导入包/单页课件常只带页面不带 assets（发布副本带、手工导出的单页通常没有）——
    缺 quiz.js 随堂检测回传不了，缺 style.css 目录页没有样式，统一从技能模板补默认件。
    """
    assets = course_dir / "assets"
    for name in ("quiz.js", "style.css"):
        if (assets / name).is_file():
            continue
        default = skill_runner.PROJECT_SKILLS_DIR / "tech" / "assets" / name
        if default.is_file():
            assets.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(default, assets / name)


def _catalog_html(course_title: str, course_id: int, lessons: list[dict]) -> str:
    """单页导入时生成的课程目录首页：复用 /tech 目录模板结构（进度组件 + 静态目录兜底）。"""
    items = "\n".join(
        f'      <li><a href="{escape(str(l["file"]))}">{escape(str(l["title"]))}</a></li>'
        for l in lessons
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(course_title)}</title>
  <link rel="stylesheet" href="assets/style.css">
</head>
<body>
  <main class="course-home">
    <header class="course-header">
      <div class="course-kicker">celestial-snow 学习闭环</div>
      <h1>{escape(course_title)}</h1>
      <p class="lesson-meta">导入课件 · 共 {len(lessons)} 课</p>
    </header>

    <!-- 学习进度：动态拉取自平台（quiz 已提交的课会打勾） -->
    <div id="course-progress"></div>
    <script>window.COURSE_ID = {course_id};</script>
    <script src="assets/quiz.js"></script>

    <h2>课程目录</h2>
    <ul>
{items}
    </ul>
  </main>
</body>
</html>
"""


def _purge_duplicate_imports(session, title: str) -> list[int]:
    """同名导入课顶掉旧课：删除同标题 import 课的记录与 courses/{id}/ 目录（连带 quiz 存档）。

    同一份 zip 修好重传是常态，不顶掉只会堆一列同名旧卡（旧卡往往还是点不开的）。
    与 create_course 的 replace 同套路：先提交 DB 删记录，再清目录，返回被顶掉的 id。
    """
    old_ids = session.scalars(
        select(Course.id).where(Course.title == title, Course.source_type == "import")
    ).all()
    if not old_ids:
        return []
    session.execute(delete(QuizResult).where(QuizResult.course_id.in_(old_ids)))
    session.execute(delete(Course).where(Course.id.in_(old_ids)))
    session.commit()
    for old_id in old_ids:
        shutil.rmtree(course_publish.course_dir_for(old_id), ignore_errors=True)
    return old_ids


async def _import_single_html(data: bytes, filename: str, title: str, series: str):
    """单个 HTML 课件页直接成课：该页即唯一课件，课程目录首页由平台生成。"""
    html = data.decode("utf-8", errors="replace")
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    page_title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    raw_name = Path(filename).name
    stem = Path(raw_name).stem or "lesson"
    lesson_id, quiz_count = _parse_lesson_html(html, stem)
    # 课件落盘名沿用上传名（只做文件名安全清洗）；index.html 是平台生成的首页，撞名则改序号名
    file_name = f"{course_publish.safe_name(stem, 'lesson', 80)}{Path(raw_name).suffix or '.html'}"
    if file_name.lower() == "index.html":
        file_name = f"01-{course_publish.safe_name(page_title or stem, stem, 48)}.html"
    lessons = [{
        "lesson_id": lesson_id[:64],
        "title": (page_title or stem)[:200],
        "file": file_name,
        "quiz_count": quiz_count,
    }]
    course_title = (title or "").strip() or page_title or stem or "导入的课程"
    series_name = (series or "").strip()[:200] or None
    with SessionLocal() as session:
        replaced_ids = _purge_duplicate_imports(session, course_title[:500])
        course = Course(
            user_id=1, source_type="import", series=series_name,
            title=course_title[:500], lessons=lessons, status="learning",
        )
        session.add(course)
        session.commit()
        course_id = course.id
    course_dir = course_publish.course_dir_for(course_id)
    shutil.rmtree(course_dir, ignore_errors=True)
    course_dir.mkdir(parents=True)
    # 课件页改写旧 course_id（quiz 回传才能对上），首页是新生成的、直接嵌新 id
    (course_dir / file_name).write_text(_rewrite_course_ids(html, course_id), encoding="utf-8")
    (course_dir / "index.html").write_text(_catalog_html(course_title, course_id, lessons), encoding="utf-8")
    _top_up_course_assets(course_dir)
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        view = _course_view(session, course, full=True)
    view["replaced_course_ids"] = replaced_ids
    return view


@router.post("/courses/import")
async def import_course(file: UploadFile = File(...), title: str = Form(""), series: str = Form("")):
    """上传已生成好的教程导入课程列表（source_type=import），两种形态：

    - zip 压缩包：接受 /tech 系技能的产物结构（index.html + NN-*.html + assets/…）及其发布副本
      （00-课程目录.html + 中文课标题文件名），解压后整体注册；
    - 单个 HTML 课件页：直接成一门 1 课的课，课程目录首页由平台生成（带学习进度组件）。

    课件里写死的旧 course_id 会被改写成新 id；缺 assets/quiz.js、style.css 时补平台默认件
    （发布副本通常带，手工打包/导出的单页可能没有）。
    series 可空：填了则归入该系列（多次导入同名系列即成一组，列表里聚拢、按导入顺序编「第N门」）。
    课程标题与已有导入课重复时直接顶掉旧课（旧记录与目录删除，返回 replaced_course_ids）。
    """
    name = (file.filename or "").lower()
    if name.endswith((".html", ".htm")):
        data = await file.read()
        if not data:
            raise HTTPException(400, "空文件")
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(413, f"文件超过 {MAX_IMPORT_BYTES // 1024 // 1024}MB 上限")
        return await _import_single_html(data, file.filename, title, series)
    if not name.endswith(".zip"):
        raise HTTPException(400, "只接受 .zip 压缩包或单个 .html 课件页")
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_IMPORT_BYTES // 1024 // 1024}MB 上限")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise HTTPException(400, f"不是有效的 zip：{e}") from e
    with tempfile.TemporaryDirectory(prefix="course-import-") as tmp:
        tmp_dir = Path(tmp)
        _extract_zip(zf, tmp_dir)
        # 根目录：zip 里若只有一层目录且课件都在其中，下钻一层
        root = tmp_dir
        entries = [p for p in root.iterdir() if not p.name.startswith(("__MACOSX", "."))]
        if len(entries) == 1 and entries[0].is_dir():
            root = entries[0]
        htmls = sorted((p for p in root.glob("*.html") if p.is_file()), key=lambda p: p.name)
        if not htmls:
            raise HTTPException(400, "压缩包顶层没有 .html 课件（应为 index.html + NN-*.html 结构）")
        # 课程首页：index.html 优先，其次 00-*.html（发布副本命名）
        index_file = next(
            (p for p in htmls if p.name.lower() == "index.html"),
            next((p for p in htmls if re.match(r"^0\d", p.name)), htmls[0]),
        )
        lesson_files = [p for p in htmls if p is not index_file]
        lessons: list[dict] = []
        index_title = ""
        for p in [index_file, *lesson_files]:
            html = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            page_title = re.sub(r"\s+", " ", m.group(1)).strip() if m else p.stem
            if p is index_file:
                index_title = page_title
                continue
            lesson_id, quiz_count = _parse_lesson_html(html, p.stem)
            lessons.append({
                "lesson_id": lesson_id[:64],
                "title": page_title[:200] or p.stem,
                "file": p.name,
                "quiz_count": quiz_count,
            })
        if not lessons:
            raise HTTPException(400, "压缩包里除首页外没有课件页，构不成一门课")
        course_title = (title or "").strip() or index_title or "导入的课程"
        series_name = (series or "").strip()[:200] or None
        with SessionLocal() as session:
            replaced_ids = _purge_duplicate_imports(session, course_title[:500])
            course = Course(
                user_id=1, source_type="import", series=series_name,
                title=course_title[:500], lessons=lessons, status="learning",
            )
            session.add(course)
            session.commit()
            course_id = course.id
        # 先注册拿 id，再改写 course_id 落盘到正式目录
        course_dir = course_publish.course_dir_for(course_id)
        shutil.rmtree(course_dir, ignore_errors=True)
        shutil.copytree(root, course_dir)
        for p in course_dir.glob("*.html"):
            p.write_text(_rewrite_course_ids(
                p.read_text(encoding="utf-8", errors="replace"), course_id
            ), encoding="utf-8")
        _top_up_course_assets(course_dir)
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        view = _course_view(session, course, full=True)
    view["replaced_course_ids"] = replaced_ids
    return view
