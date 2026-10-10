"""规则打分：可计算维度（star 增速 15% / 活跃度 15% / 维护健康度 10%），每项带理由。

设计共识的六维权重：企业落地潜力 25%、匹配度 20%、star 增速 15%、活跃度 15%、
核心思想/学习价值 15%、维护健康度 10%。本模块只算规则三维（合计 40 分封顶），
LLM 三维由分析阶段补足——未分析的项目总分天然低于已分析的，正是想要的排序。
"""
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WEIGHT_STAR_MOMENTUM = 0.15
WEIGHT_ACTIVITY = 0.15
WEIGHT_MAINTENANCE = 0.10


def _clamp(value: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, value))


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # SQLite DateTime 读出是 naive（无时区 UTC 字符串），补齐再减
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def score_repo(repo: dict, *, period_stars: int = 0, period_days: int = 7) -> tuple[float, dict]:
    """对 GitHub API 风格的 repo dict 打规则分。

    repo 需含 stars/open_issues/pushed_at/contributors_count（缺的维度按 0 处理）。
    返回 (rule_score 0-40, rule_detail)。
    """
    stars = int(repo.get("stars") or 0)
    open_issues = int(repo.get("open_issues") or 0)
    contributors = int(repo.get("contributors_count") or 0)
    pushed_days = _days_since(repo.get("pushed_at"))
    created_days = _days_since(repo.get("github_created_at"))

    detail: dict = {}

    # --- star 增速：期间增量优先，缺失用「存量/仓库年龄」估算 ---
    momentum = period_stars
    if momentum <= 0 and created_days and created_days > 0:
        momentum = stars / max(created_days, 1) * period_days  # 均摊到期间
    if stars > 0:
        ratio = momentum / stars
        score = _clamp(100 * min(ratio / 0.05, 1.0))  # 期间增量达存量 5% 即满分
        reason = f"期间新增 {momentum} star（存量 {stars}，{ratio:.1%}）"
    elif momentum > 0:
        score = _clamp(momentum / 2)  # 新仓库无存量，纯看增量
        reason = f"新仓库，期间新增 {momentum} star"
    else:
        score = 0.0
        reason = "无 star 增量数据"
    detail["star_momentum"] = {"score": round(score, 1), "reason": reason}

    # --- 活跃度：issue 流水 + 贡献者规模 + 最近推送 ---
    parts: list[str] = []
    issue_score = _clamp(open_issues / 5)  # 500 个 open issues 封顶满分
    parts.append(f"open issues {open_issues}")
    contrib_score = _clamp(contributors / 30)  # 300 贡献者封顶
    parts.append(f"贡献者 {contributors}")
    activity = (issue_score + contrib_score) / 2
    if pushed_days is not None:
        fresh = _clamp(100 - (pushed_days - 1) * 10)  # 1 天内 100 分，之后每天 -10
        activity = (activity * 2 + fresh) / 3
        parts.append(f"最近推送 {pushed_days:.0f} 天前")
    detail["activity"] = {"score": round(activity, 1), "reason": "，".join(parts)}

    # --- 维护健康度：推送新鲜度 + 仓库完整度 ---
    health_parts: list[str] = []
    if pushed_days is not None:
        health = _clamp(100 - (pushed_days - 3) * 5)  # 3 天内满分，之后每 5 天 -25
        health_parts.append(f"最近推送 {pushed_days:.0f} 天前")
    else:
        health = 30.0
        health_parts.append("无推送时间数据")
    if repo.get("license"):
        health = min(health + 10, 100)
        health_parts.append(f"有 {repo['license']} 许可证")
    else:
        health_parts.append("无许可证")
    detail["maintenance"] = {"score": round(health, 1), "reason": "，".join(health_parts)}

    total = (
        detail["star_momentum"]["score"] * WEIGHT_STAR_MOMENTUM
        + detail["activity"]["score"] * WEIGHT_ACTIVITY
        + detail["maintenance"]["score"] * WEIGHT_MAINTENANCE
    )
    return round(total, 1), detail


def total_score(rule_detail: dict, llm_scores: dict) -> float:
    """六维加权总分：规则三维（已存 rule_detail）+ LLM 三维（llm_scores 缺则跳过）。"""
    llm_weights = {"enterprise_potential": 0.25, "match": 0.20, "learning_value": 0.15}
    total = (
        _dim(rule_detail, "star_momentum") * WEIGHT_STAR_MOMENTUM
        + _dim(rule_detail, "activity") * WEIGHT_ACTIVITY
        + _dim(rule_detail, "maintenance") * WEIGHT_MAINTENANCE
    )
    for key, weight in llm_weights.items():
        total += _dim(llm_scores, key) * weight
    return round(total, 1)


def _dim(source: dict, key: str) -> float:
    node = (source or {}).get(key) or {}
    try:
        return float(node.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------- 「疑似已修复」提示（issue 排序沉底用） ----------

# 合入/合并只在带 main/master/主分支 上下文时才算提示（「检查 PR 是否已合并」是行动指引，不是过期信号）
FIXED_HINT_RE = re.compile(
    r"疑似已"  # 疑似已在 main 分支由 #xxx 修复
    r"|(可能|或许|估计|应)已?在\s*(main|master|主分支)"
    r"|已(经)?(修复|解决|关闭)"
    r"|已(经)?在\s*(main|master|主分支)[^。]{0,20}(合入|合并)"
    r"|already\s+(been\s+)?(fix\w*|merged?)|fix(ed)?\s+in\s+(main|master|#\d)|duplicate\s+of"
    r"|与\s*#\d+\s*重复",
    re.IGNORECASE,
)


def refresh_fixed_hint(issue) -> bool:
    """过期信号汇总 → True，写回物化列 fixed_hint（排序沉底用）。

    三类信号取或：
    1. 文本正则：title/summary/action/screen_reason 带过期措辞（FIXED_HINT_RE）；
    2. GitHub 侧已关闭（state == "closed"，复查节点回填）；
    3. 修复 PR 已合并（fixed_by_pr 非空，深读查 timeline 的 cross-referenced）。

    在摄入与 LLM 回写 summary/action/screen_reason 的节点顺带重算；
    之前 API 层取 300 条再 Python 排序，分页（OFFSET）下会破坏全局排序。
    """
    text = " ".join(
        filter(None, [issue.title, issue.summary, issue.action, issue.screen_reason])
    )
    issue.fixed_hint = (
        bool(FIXED_HINT_RE.search(text))
        or issue.state == "closed"
        or bool(issue.fixed_by_pr)
    )
    return issue.fixed_hint


# ---------- 脚手架候选的规则适配分（无 LLM 也能粗排） ----------

def scaffold_rule_fit(keywords: list[str], repo: dict, skills: list[str]) -> tuple[float, str]:
    """脚手架候选的规则分（0-40）：语言命中用户技能 / 检索关键词命中 / 活跃度。

    keywords 是 LLM 规划的 search_keywords + tech_hints（英文小写）；
    repo 为 GitHub API 风格 dict（language/topics/description/stars/pushed_at）。
    与 scaffold_fit_batch 的 LLM 分（0-60）合成 fit_score 0-100。
    """
    reasons: list[str] = []
    score = 0.0

    lang = (repo.get("language") or "").lower()
    lang_aliases = {  # 语言 → 技能画像里可能的写法
        "python": "python", "java": "java", "javascript": "javascript",
        "typescript": "typescript", "go": "go", "rust": "rust", "c#": "c#", "c++": "c++",
    }
    skill_words = {s.strip().lower() for s in skills if s.strip()}
    if lang and lang_aliases.get(lang, lang) in skill_words:
        score += 15
        reasons.append(f"语言 {repo.get('language')} 命中技能画像 +15")

    haystack = " ".join(
        [(t or "") for t in (repo.get("topics") or [])] + [repo.get("description") or ""]
    ).lower()
    hits = [k for k in keywords if k and k.lower() in haystack]
    if hits:
        bonus = min(len(hits) * 5, 15)
        score += bonus
        reasons.append(f"关键词命中 {'、'.join(hits[:3])} +{bonus}")

    pushed_days = _days_since(repo.get("pushed_at"))
    if pushed_days is not None:
        fresh = _clamp(100 - pushed_days * 3) / 100 * 10  # 33 天内满分 10，线性衰减
        score += fresh
        reasons.append(f"最近推送 {pushed_days:.0f} 天前 +{fresh:.0f}")
    stars = int(repo.get("stars") or 0)
    if stars >= 5000:
        score += 5
        reasons.append(f"高星 {stars} +5")

    return round(_clamp(score, 0, 40), 1), "；".join(reasons) or "无规则命中"


# ---------- issue 规则预分（无 LLM 也能出 issue 排行） ----------

LABEL_BOOSTS = {
    "good first issue": 30,
    "good-first-issue": 30,
    "help wanted": 25,
    "help-wanted": 25,
    "documentation": 15,
    "docs": 15,
    "bug": 10,
    "enhancement": 8,
    "feature": 8,
    "beginner": 20,
    "beginner-friendly": 20,
    "question": -10,
    "wontfix": -50,
}


def issue_rule_score(issue: dict, skills: list[str]) -> tuple[float, str]:
    """本地规则预判「这个 issue 适不适合外部贡献者、是否贴合用户技能」。

    issue: {title, labels, body, comments}；返回 (0-100 分, 理由)。
    """
    labels = [str(lb).lower().strip() for lb in (issue.get("labels") or [])]
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    score = 30.0  # 底分
    reasons: list[str] = []

    for lb in labels:
        boost = LABEL_BOOSTS.get(lb)
        if boost:
            score += boost
            reasons.append(f"标签 {lb} {'+' if boost >= 0 else ''}{boost}")
        elif "first" in lb or "beginner" in lb or "starter" in lb:
            score += 20
            reasons.append(f"疑似新手友好标签 {lb} +20")

    matched = [s for s in skills if s and (s.lower() in title or s.lower() in body)]
    if matched:
        score += min(len(matched) * 12, 30)
        reasons.append(f"命中技能关键词 {'、'.join(matched[:4])} +{min(len(matched) * 12, 30)}")

    if issue.get("comments", 0) == 0:
        score += 8  # 无人认领，机会更大
        reasons.append("暂无评论（易认领）+8")
    elif issue.get("comments", 0) > 15:
        score -= 10  # 长期争论，难插入
        reasons.append("评论过多（竞争激烈）-10")

    return round(_clamp(score), 1), "；".join(reasons) or "基础分"
