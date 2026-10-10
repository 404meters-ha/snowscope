"""两条主流水线：每日刷新（抓取→入库→规则分→LLM 精析→总分）与贡献分析（issues→预筛→深读→报告）。

由 TaskManager 异步驱动（coro_factory(task_id, progress_cb)），LLM/搜索未配置时按设计共识降级：
LLM 缺席 → 榜单只有规则分；搜索缺席 → 企业案例只认 README 明确声明。
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Analysis, ContributionReport, Issue, Repo, Snapshot
from .github_client import GitHubClient, GitHubRateLimitError
from .llm import (
    LLMClient,
    LLMNotConfigured,
    analyze_repo,
    repo_verdict,
    report_issue,
    screen_issues,
    summarize_issues,
)
from .scoring import issue_rule_score, refresh_fixed_hint, score_repo, total_score
from .search import SearchClient
from .trending import TrendingFetcher

logger = logging.getLogger(__name__)

MAX_CONTRIB_REPOS = 5
MAX_SCREENED_ISSUES = 8
SUMMARY_TOP_N = 60  # 每次刷新为规则分最高的 N 条 issue 批量生成摘要（省 LLM 预算）
SUMMARY_BATCH = 15


# ---------- 数据入库 ----------

def _upsert_repo(session, item: dict) -> Repo:
    """按 full_name 去重入库；item 兼容 trending 抓取与 GitHub API 两种来源。"""
    full_name = item["full_name"]
    repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
    if repo is None:
        repo = Repo(full_name=full_name)
        session.add(repo)
    repo.description = item.get("description") or repo.description
    repo.language = item.get("language") or repo.language
    repo.stars = int(item.get("stars") or repo.stars or 0)
    if item.get("topics") is not None:
        repo.topics = item["topics"]
    if item.get("homepage"):
        repo.homepage = item["homepage"]
    if item.get("license"):
        repo.license = item["license"]
    if item.get("open_issues") is not None:
        repo.open_issues = int(item["open_issues"])
    if item.get("pushed_at"):
        repo.pushed_at = _parse_dt(item["pushed_at"])
    if item.get("github_created_at"):
        repo.github_created_at = _parse_dt(item["github_created_at"])
    repo.last_seen_at = datetime.now(timezone.utc)
    return repo


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _add_snapshot(session, repo: Repo) -> None:
    session.add(Snapshot(repo_id=repo.id, stars=repo.stars))


# ---------- 流水线 1：每日刷新 ----------

async def refresh_pipeline(github: GitHubClient, task_id: str, progress) -> dict:
    settings = get_settings()
    stats = {"weekly": 0, "monthly": 0, "enriched": 0, "analyzed": 0, "errors": []}
    fetcher = TrendingFetcher(github)
    llm = LLMClient()
    search = SearchClient()

    try:
        # 1. 抓取 weekly + monthly，合并去重（同一项目可能双榜在列）
        progress("抓取 GitHub Trending（weekly/monthly）…")
        by_name: dict[str, dict] = {}
        period_of: dict[str, str] = {}
        periods_seen: dict[str, set[str]] = {}
        for since, days in (("weekly", 7), ("monthly", 30)):
            for item in await fetcher.fetch(since, limit=settings.candidate_pool // 2):
                name = item["full_name"]
                periods_seen.setdefault(name, set()).add(since)
                if name not in by_name:
                    by_name[name] = item
                    period_of[name] = since
                elif item["period_stars"] > by_name[name]["period_stars"]:
                    by_name[name].update(item)  # 双榜都在时保留增量更猛的口径
                    period_of[name] = since
                stats[since] += 1
        stats["candidates"] = len(by_name)

        # 2. 入库 + 快照 + 初步规则分（此时只有 trending 页面数据）
        progress(f"入库 {len(by_name)} 个项目…")
        with SessionLocal() as session:
            repos: dict[str, Repo] = {}
            for name, item in by_name.items():
                repo = _upsert_repo(session, item)
                # 记录榜单期次（双榜都在则累积）
                repo.periods = sorted(periods_seen.get(name, {period_of[name]}))
                repos[name] = repo
            session.flush()  # 让新 Repo 拿到自增 id，快照外键需要
            for repo in repos.values():
                _add_snapshot(session, repo)
            session.commit()
            prelim = {
                name: score_repo(
                    {
                        "stars": repo.stars,
                        "open_issues": 0,
                        "pushed_at": None,
                        "contributors_count": 0,
                        "license": "",
                        "github_created_at": None,
                    },
                    period_stars=by_name[name]["period_stars"],
                    period_days=7 if period_of[name] == "weekly" else 30,
                )
                for name, repo in repos.items()
            }
        # 初筛排序：纯 star 动量
        ranked = sorted(prelim, key=lambda n: prelim[n][1]["star_momentum"]["score"], reverse=True)
        top_names = ranked[: settings.top_n_llm]

        # 3. issue 榜基础数据优先抓（仅 10 次调用；无 token 的 60 次/小时配额很珍贵，
        #    不能先被详情补全耗光——issue 榜是用户核心入口）
        progress("抓取 top 仓库的 open issues（建立 Issue 榜）…")
        issue_skills = [s.strip() for s in settings.user_skills.split(",")]
        stats["issues"] = 0
        for name in top_names[:10]:
            try:
                raw_issues = await github.get_open_issues(name, limit=100)
            except GitHubRateLimitError as e:
                stats["errors"].append(str(e))
                break
            except Exception as e:  # noqa: BLE001 单仓库失败继续
                stats["errors"].append(f"issues {name}: {e}")
                continue
            with SessionLocal() as session:
                repo = session.scalar(select(Repo).where(Repo.full_name == name))
                repo_id = repo.id if repo else None
                for raw in raw_issues:
                    _upsert_issue(session, repo, raw, issue_skills)
                session.commit()
            if repo_id:
                closed_n = await _refresh_issue_states(
                    github, name, repo_id, {r["number"] for r in raw_issues})
                if closed_n:
                    stats["issues_closed"] = stats.get("issues_closed", 0) + closed_n
                    progress(f"{name}：{closed_n} 条库内 issue 已在 GitHub 关闭，沉底处理")
            stats["issues"] += len(raw_issues)
        progress(f"Issue 榜基础数据：{stats['issues']} 条")

        # 3.5 为规则分最高的 top N issue 批量生成「什么事/做什么」摘要（无 LLM 时跳过，界面显示正文截断）
        if llm.configured:
            await _summarize_top_issues(llm, progress, stats)



        # 4. 补 API 详情（rate limit 容忍：逐个 try，挂了就用已有数据）
        # 无 token 时只有 60 次/h（issues 已用 10 次），详情补全自动砍到 8 个，避免中途全断
        enrich_n = len(top_names) if settings.github_token else min(len(top_names), 8)
        progress(f"粗筛完成，补全 top {enrich_n} 的仓库详情…")
        enriched: dict[str, dict] = {}
        for i, name in enumerate(top_names[:enrich_n]):
            try:
                meta = await github.get_repo(name)
                enriched[name] = {
                    "stars": meta.get("stargazers_count", 0),
                    "open_issues": meta.get("open_issues_count", 0),
                    "pushed_at": meta.get("pushed_at"),
                    "github_created_at": meta.get("created_at"),
                    "license": (meta.get("license") or {}).get("spdx_id") or "",
                    "homepage": meta.get("homepage") or "",
                    "topics": meta.get("topics") or [],
                    "description": meta.get("description") or "",
                    "contributors_count": await github.get_contributors_count(name),
                }
                stats["enriched"] += 1
            except GitHubRateLimitError as e:
                stats["errors"].append(str(e))
                break  # 限流后继续也只是白打请求
            except Exception as e:  # noqa: BLE001 单仓库失败不断流水线
                stats["errors"].append(f"{name}: {e}")
            if (i + 1) % 10 == 0:
                progress(f"详情补全 {i + 1}/{len(top_names)}…")

        # 5. 规则分定稿（有详情的用全维度，没详情的保持初筛分）
        with SessionLocal() as session:
            for name in top_names:
                repo = session.scalar(select(Repo).where(Repo.full_name == name))
                if repo is None:
                    continue
                if name in enriched:
                    period_days = 7 if period_of[name] == "weekly" else 30
                    rule_score, rule_detail = score_repo(
                        enriched[name], period_stars=by_name[name]["period_stars"], period_days=period_days
                    )
                    repo.rule_score, repo.rule_detail = rule_score, rule_detail
                    _apply_enrichment(session, repo, enriched[name])
                else:
                    repo.rule_score, repo.rule_detail = prelim[name]
            session.commit()

        # 6. LLM 精析（未配置则整段跳过，榜单只有规则分）
        if llm.configured:
            for i, name in enumerate(top_names):
                progress(f"LLM 精析 {i + 1}/{len(top_names)}：{name}")
                await _analyze_one(session_factory=SessionLocal, github=github, llm=llm, search=search,
                                   profile=settings.user_profile, full_name=name, stats=stats)
        else:
            stats["errors"].append("LLM 未配置，本次只有规则分（填 LLM_BASE_URL/LLM_API_KEY 后重新分析）")

        progress("刷新完成")
        return stats
    finally:
        await llm.close()


def _apply_enrichment(session, repo: Repo, meta: dict) -> None:
    """把 API 补全的字段写回 Repo（不覆盖已有非空值以外的逻辑见 _upsert_repo）。"""
    _upsert_repo(session, {**meta, "full_name": repo.full_name})


async def _analyze_one(*, session_factory, github: GitHubClient, llm: LLMClient, search: SearchClient,
                       profile: str, full_name: str, stats: dict) -> None:
    """LLM 精析单个仓库并落库（Analysis 一仓一份，重跑覆盖）。"""
    with session_factory() as session:
        repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
        if repo is None:
            return
        analysis = session.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
    try:
        readme = await github.get_readme(full_name)
        data = await analyze_repo(llm, profile, full_name, readme)

        # 企业案例搜索验证：README 有声明的标记 verified；搜索只做补充提示，不凭空造案例
        if search.configured and not data.get("enterprise_cases"):
            hits = await search.search(f'"{full_name}" production use case company')
            if hits:
                data.setdefault("enterprise_cases", [])
        if search.configured:
            for case in data.get("enterprise_cases", []):
                case.setdefault("verified", False)

        with session_factory() as session:
            repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
            analysis = session.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
            if analysis is None:
                analysis = Analysis(repo_id=repo.id)
                session.add(analysis)
            analysis.status = "done"
            analysis.core_idea = data.get("core_idea", "")
            analysis.enterprise_cases = data.get("enterprise_cases", [])
            analysis.llm_scores = {
                k: data.get(k, {}) for k in ("enterprise_potential", "match", "learning_value")
            }
            analysis.model = get_settings().llm_model
            analysis.readme = readme[:50000]
            analysis.analyzed_at = datetime.now(timezone.utc)
            repo.total_score = total_score(repo.rule_detail, analysis.llm_scores)
            # 中文简介兜底：精析产出的 core_idea 本身就是中文描述，列表「中文简介」列直接可用
            if not repo.zh_desc and data.get("core_idea"):
                repo.zh_desc = str(data["core_idea"])[:120]
            session.commit()
        stats["analyzed"] += 1
    except LLMNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001 单仓库分析失败不拖垮整批
        logger.warning("分析 %s 失败: %s", full_name, e)
        stats["errors"].append(f"分析 {full_name}: {e}")
        with session_factory() as session:
            repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
            if repo is not None:
                analysis = session.scalar(select(Analysis).where(Analysis.repo_id == repo.id))
                if analysis is None:
                    analysis = Analysis(repo_id=repo.id)
                    session.add(analysis)
                analysis.status = "failed"
                analysis.note = f"{type(e).__name__}: {e}"
                session.commit()


# ---------- 流水线 1.5：批量精析选中的项目（「未精析」tab 的配套动作） ----------

MAX_ANALYZE_REPOS = 10

async def analyze_pipeline(github: GitHubClient, repo_ids: list[int], task_id: str, progress) -> dict:
    """对选中的仓库逐个跑 LLM 精析（复用刷新流水线的 _analyze_one，重跑覆盖）。"""
    if len(repo_ids) > MAX_ANALYZE_REPOS:
        raise ValueError(f"每次最多精析 {MAX_ANALYZE_REPOS} 个项目")
    settings = get_settings()
    llm = LLMClient()
    if not llm.configured:
        await llm.close()
        raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY），无法精析")
    search = SearchClient()
    stats: dict = {"analyzed": 0, "errors": []}
    try:
        with SessionLocal() as session:
            names = [n for (n,) in session.execute(
                select(Repo.full_name).where(Repo.id.in_(repo_ids))).all()]
        if not names:
            raise ValueError("没有有效的仓库")
        for i, name in enumerate(names, 1):
            progress(f"LLM 精析 {i}/{len(names)}：{name}")
            await _analyze_one(session_factory=SessionLocal, github=github, llm=llm, search=search,
                               profile=settings.user_profile, full_name=name, stats=stats)
        if stats["errors"]:
            progress(f"精析结束：成功 {stats['analyzed']}，失败 {len(stats['errors'])}——{stats['errors'][-1][:80]}")
        else:
            progress(f"精析结束：{stats['analyzed']} 个项目完成")
        return stats
    finally:
        await llm.close()


# ---------- 流水线 2：贡献机会分析 ----------

async def _summarize_top_issues(llm: LLMClient, progress, stats: dict) -> None:
    """为规则分最高的 top N issue 批量生成 summary/action（15 条一次调用，省预算）。

    送给模型的 number 用批内顺序号重映射——不同仓库的 issue number 会撞号，
    不能让 LLM 原样回显真实 number 做对账。
    """
    with SessionLocal() as session:
        rows = session.execute(
            select(Issue)
            .where(Issue.summary == "")
            .order_by(Issue.rule_match_score.desc())
            .limit(SUMMARY_TOP_N)
        ).scalars().all()
        rows = list(rows)
    done = 0
    for start in range(0, len(rows), SUMMARY_BATCH):
        batch = rows[start : start + SUMMARY_BATCH]
        try:
            result = await summarize_issues(
                llm,
                [{"number": i + 1, "title": row.title, "body": row.body_excerpt} for i, row in enumerate(batch)],
            )
        except Exception as e:  # noqa: BLE001 摘要是增强项，失败不阻塞刷新
            stats["errors"].append(f"issue 摘要批次失败: {e}")
            continue
        with SessionLocal() as session:
            for i, row in enumerate(batch):
                item = result.get(i + 1)
                if not item:
                    continue
                managed = session.get(Issue, row.id)
                managed.summary = item.get("summary") or ""
                managed.action = item.get("action") or ""
                refresh_fixed_hint(managed)
            session.commit()
        done += len(batch)
        progress(f"issue 摘要 {done}/{len(rows)}…")
    stats["summarized"] = done


def _upsert_issue(session, repo: Repo, raw: dict, skills: list[str]) -> Issue:
    """open issue 落库（规则预分）；已存在则刷新元数据。"""
    issue = session.scalar(select(Issue).where(Issue.repo_id == repo.id, Issue.number == raw["number"]))
    if issue is None:
        issue = Issue(repo_id=repo.id, number=raw["number"])
        session.add(issue)
    labels = [lb.get("name", "") for lb in raw.get("labels", [])]
    rule_score, _ = issue_rule_score(
        {"title": raw.get("title"), "labels": labels, "body": raw.get("body"), "comments": raw.get("comments", 0)},
        skills,
    )
    issue.title = raw.get("title") or issue.title
    issue.url = raw.get("html_url") or issue.url
    issue.labels = labels
    issue.comments_count = int(raw.get("comments") or 0)
    issue.body_excerpt = (raw.get("body") or "")[:1000]
    issue.state = raw.get("state") or "open"  # 直接赋值：重开的 issue 也能翻回 open
    issue.closed_at = _parse_dt(raw.get("closed_at"))
    issue.rule_match_score = rule_score
    refresh_fixed_hint(issue)
    return issue


async def _refresh_issue_states(github: GitHubClient, full_name: str, repo_id: int, open_numbers: set[int]) -> int:
    """复查库内 issue 的 GitHub 状态：仍标 open 但本次 open 抓取缺席的，拉「最近关闭」清单核对。

    抓取缺席 ≠ 已关闭（open 超 100 条时老 issue 会掉出抓取窗口），以 closed 清单命中为准，
    未命中的留待下轮；复查是增强项，API 失败（含限流）静默跳过不阻塞主流程。
    命中回填 state/closed_at，refresh_fixed_hint 汇总后沉底。
    """
    with SessionLocal() as session:
        stmt = select(Issue.number).where(Issue.repo_id == repo_id, Issue.state == "open")
        if open_numbers:
            stmt = stmt.where(Issue.number.not_in(list(open_numbers)))
        stale_numbers = set(session.scalars(stmt).all())
    if not stale_numbers:
        return 0
    try:
        closed = await github.get_closed_issues(full_name, limit=100)
    except Exception:  # noqa: BLE001 复查失败留待下轮
        return 0
    hits = {c["number"]: c for c in closed if c.get("number") in stale_numbers}
    if not hits:
        return 0
    with SessionLocal() as session:
        for number, raw in hits.items():
            row = session.scalar(select(Issue).where(Issue.repo_id == repo_id, Issue.number == number))
            if row is None:
                continue
            row.state = "closed"
            row.closed_at = _parse_dt(raw.get("closed_at"))
            refresh_fixed_hint(row)
        session.commit()
    return len(hits)


async def _find_merged_fix_pr(github: GitHubClient, full_name: str, number: int) -> int | None:
    """搜已合并、正文提到该 issue 的修复 PR（实现见 GitHubClient.search_merged_fix_prs）。

    search 限流独立（30 次/分），深读每轮 ≤ 10 个 issue 不会触顶；失败返回 None 不影响深读。
    """
    try:
        return await github.search_merged_fix_prs(full_name, number)
    except Exception:  # noqa: BLE001 增强项，失败不影响深读
        return None


async def contribution_pipeline(github: GitHubClient, repo_ids: list[int], task_id: str, progress) -> dict:
    if len(repo_ids) > MAX_CONTRIB_REPOS:
        raise ValueError(f"每次最多分析 {MAX_CONTRIB_REPOS} 个项目")
    llm = LLMClient()
    if not llm.configured:
        await llm.close()
        raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY），无法做贡献分析")
    settings = get_settings()
    profile = settings.user_profile
    result: dict = {"reports": [], "errors": []}
    created: list[int] = []

    with SessionLocal() as session:
        names = [n for (n,) in session.execute(
            select(Repo.full_name).where(Repo.id.in_(repo_ids))).all()]
    if not names:
        raise ValueError("没有有效的仓库")

    try:
        for idx, full_name in enumerate(names, 1):
            progress(f"[{idx}/{len(names)}] {full_name}：拉取 issues 与贡献指南…")
            with SessionLocal() as session:
                repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
                report = ContributionReport(repo_id=repo.id, status="running")
                session.add(report)
                session.commit()
                created.append(report.id)

            report_issues: list[dict] = []
            verdict: dict = {}
            issues: list[dict] = []
            picked: list[dict] = []
            repo_meta: dict = {}
            try:
                issues = await github.get_open_issues(full_name, limit=100)
                contributing = await github.get_contributing(full_name)
                contributors_count = await github.get_contributors_count(full_name)

                # 全量 open issues 先落库（规则预分）——issue 榜的基础数据，LLM 挂了也有排序
                skills = [s.strip() for s in settings.user_skills.split(",")]
                with SessionLocal() as session:
                    repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
                    repo_id = repo.id if repo else None
                    for raw in issues:
                        _upsert_issue(session, repo, raw, skills)
                    session.commit()
                if repo_id:
                    closed_n = await _refresh_issue_states(
                        github, full_name, repo_id, {i["number"] for i in issues})
                    if closed_n:
                        progress(f"[{idx}/{len(names)}] {full_name}：{closed_n} 条库内 issue 已在 GitHub 关闭，沉底处理")

                picked = await screen_issues(llm, full_name, contributing, issues, settings.user_skills)
                picked = picked[:MAX_SCREENED_ISSUES]
                progress(f"[{idx}/{len(names)}] 预筛出 {len(picked)} 个 issue，逐个深读…")

                # LLM 匹配分写回 issue 榜
                with SessionLocal() as session:
                    repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
                    for p in picked:
                        row = session.scalar(
                            select(Issue).where(Issue.repo_id == repo.id, Issue.number == p.get("number")))
                        if row is not None:
                            row.match_score = float(p.get("match") or 0)
                            row.difficulty = p.get("difficulty") or ""
                            row.screen_reason = p.get("reason") or ""
                            refresh_fixed_hint(row)
                    session.commit()

                by_number = {i["number"]: i for i in issues}
                for j, p in enumerate(picked, 1):
                    number = p.get("number")
                    issue = by_number.get(number)
                    if issue is None:
                        continue
                    try:
                        comments = await github.get_issue_comments(full_name, number)
                        detail = await report_issue(llm, profile, full_name, issue, comments, contributing)
                        detail.setdefault("number", number)
                        detail.setdefault("title", issue.get("title", ""))
                        detail.setdefault("url", issue.get("html_url", ""))
                        detail.setdefault("labels", [lb.get("name", "") for lb in issue.get("labels", [])])
                        detail["screen_reason"] = p.get("reason", "")
                        report_issues.append(detail)
                        # 深读顺带查修复 PR：search 已合并且正文提到该 issue 的 PR → fixed_by_pr
                        merged_fix = await _find_merged_fix_pr(github, full_name, number)
                        # 深读结果同步回 issue 榜
                        with SessionLocal() as session:
                            repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
                            row = session.scalar(
                                select(Issue).where(Issue.repo_id == repo.id, Issue.number == number))
                            if row is not None:
                                row.deep_read = True
                                row.fit_reason = detail.get("fit_reason") or ""
                                row.difficulty = detail.get("difficulty") or row.difficulty
                                row.summary = detail.get("background") or row.summary
                                row.action = detail.get("approach") or row.action
                                if merged_fix:
                                    row.fixed_by_pr = merged_fix
                                refresh_fixed_hint(row)
                            session.commit()
                    except Exception as e:  # noqa: BLE001 单 issue 深读失败跳过
                        result["errors"].append(f"{full_name}#{number}: {e}")
                    progress(f"[{idx}/{len(names)}] issue 深读 {j}/{len(picked)}…")

                with SessionLocal() as session:
                    repo = session.scalar(select(Repo).where(Repo.full_name == full_name))
                    if repo is not None:
                        repo_meta = {"description": repo.description, "stars": repo.stars,
                                     "language": repo.language, "open_issues": repo.open_issues}
                verdict = await repo_verdict(
                    llm, full_name, repo_meta, picked, contributing, contributors_count)
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"{full_name}: {e}")

            with SessionLocal() as session:
                report = session.get(ContributionReport, created[-1])
                report.status = "done" if report_issues or verdict else "failed"
                report.issues = report_issues
                report.repo_verdict = verdict
                report.stats = {"open_issues_fetched": len(issues),
                                "screened": len(picked),
                                "deep_read": len(report_issues)}
                report.error = "; ".join(result["errors"][-3:])
                report.finished_at = datetime.now(timezone.utc)
                session.commit()
            result["reports"].append({"repo": full_name, "report_id": created[-1],
                                      "issues": len(report_issues)})
            # 失败也要在任务进度里喊出来——报告 failed 而任务静默 success，用户会以为成了
            tail = "报告完成" if report.status == "done" else f"分析失败：{(report.error or '')[:80]}"
            progress(f"[{idx}/{len(names)}] {full_name} {tail}")
    finally:
        await llm.close()
    return result
