"""标签库：canonical 登记、别名归一、脏标签一次性清洗。

所有写 repos.tags 的路径（行业分析、全量打标、清洗）都经 ensure_tags 归一：
alias/canonical 精确命中直接映射；未命中的 LLM 只做翻译级/缩写级同义合并
（拿不准保持独立——宁可多一个标签，不可错并）；全新词登记为新 canonical。

cleanup_pipeline：历史整串脏标签（「agent运行时/pi/deer-flow」一类）拆串——
中文段是方向词（归一后重新打回），纯 ASCII 段是项目名片段（丢弃）。
启动时经 main.py 检测触发：有脏特征 + 无成功记录（哨兵）才跑，成功一次永不重跑。
"""
import logging
import re

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Repo, Tag, TaskRun
from ..tasks import _append_log
from .llm import LLMClient, LLMNotConfigured, normalize_tags

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[一-鿿]")
# 脏标签特征：紧贴式斜杠（「agent运行时/pi/deer-flow」这类整串输入）。
# 「其他 / 杂项」这种带空格的斜杠是合法分类名，不算脏。
_DIRTY_SLASH = re.compile(r"\S/\S")


def all_tag_names() -> list[str]:
    """标签库全部 canonical 名（供 prompt 注入与归一候选）。"""
    with SessionLocal() as session:
        return sorted(r.name for (r,) in session.execute(select(Tag)).all())


def _alias_index() -> dict[str, str]:
    """name 与全部 alias → canonical 的映射表。"""
    index: dict[str, str] = {}
    with SessionLocal() as session:
        for (row,) in session.execute(select(Tag)).all():
            index[row.name] = row.name
            for a in row.aliases or []:
                index[a] = row.name
    return index


async def ensure_tags(llm: LLMClient, words: list[str], source: str, log=None) -> dict[str, str]:
    """词列表 → canonical 映射（副作用：命中 alias 登记、新词建卡）。

    返回 {原词: canonical}；调用方拿映射后的值写 repos.tags。
    """
    words = [w.strip() for w in words if w.strip()]
    mapping: dict[str, str] = {}
    if not words:
        return mapping

    index = _alias_index()
    unresolved: list[str] = []
    for w in words:
        if w in index:
            mapping[w] = index[w]
        else:
            unresolved.append(w)

    if unresolved:
        # LLM 只做高置信同义合并（翻译级/缩写级），拿不准的返回里没有 → 新建
        try:
            merges = await normalize_tags(llm, unresolved, sorted(set(index.values())))
        except Exception as e:  # noqa: BLE001 归一失败退化为「全部新建」，不阻塞打标
            logger.warning("标签归一 LLM 调用失败（退化为新建）: %s", e)
            merges = {}
        with SessionLocal() as session:
            for w in unresolved:
                canon = merges.get(w, "")
                if canon:
                    row = session.scalar(select(Tag).where(Tag.name == canon))
                    if row is not None:
                        row.aliases = sorted(set((row.aliases or []) + [w]))
                        mapping[w] = canon
                        continue
                # 没并入（或并入目标不在库里，理论不该发生）：新词自己当 canonical
                if session.scalar(select(Tag).where(Tag.name == w)) is None:
                    session.add(Tag(name=w, aliases=[], source=source))
                mapping[w] = w
            session.commit()
        if log:
            merged = {w: c for w, c in merges.items() if c and c != w}
            log(f"标签库归一：{len(unresolved)} 个新词，并入 {len(merged)} 个"
                + (f"（{'、'.join(f'{k}→{v}' for k, v in merged.items())}）" if merged else ""))
    return mapping


def has_dirty_tags() -> bool:
    """脏标签特征：某项目的 tags 里出现紧贴式斜杠标签（历史整串输入）。"""
    with SessionLocal() as session:
        rows = session.execute(select(Repo.tags)).all()
        return any(_DIRTY_SLASH.search(t) for (tags,) in rows for t in (tags or []))


def cleanup_done() -> bool:
    """成功哨兵：跑成过一次清洗就永不重跑。"""
    with SessionLocal() as session:
        n = session.scalar(
            select(func.count()).select_from(TaskRun)
            .where(TaskRun.type == "tag_cleanup", TaskRun.status == "success")
        )
        return bool(n)


async def cleanup_pipeline(task_id: str, progress, log=None) -> dict:
    """拆历史整串脏标签：中文段归一后打回原项目，纯 ASCII 段（项目名）丢弃。"""
    llm = LLMClient()
    if not llm.configured:
        await llm.close()
        raise LLMNotConfigured("LLM 未配置（.env 里填 LLM_BASE_URL / LLM_API_KEY），无法归一标签")
    emit = log or progress

    try:
        # 1. 拆串：收集脏标签 → 方向词片段；同时记下受影响的项目
        direction_words: set[str] = set()
        touched: dict[int, list[str]] = {}  # repo_id -> 保留的干净标签
        with SessionLocal() as session:
            rows = session.execute(select(Repo)).scalars().all()
            for repo in rows:
                tags = repo.tags or []
                dirty = [t for t in tags if _DIRTY_SLASH.search(t)]
                if not dirty:
                    continue
                keep = [t for t in tags if "/" not in t]
                for d in dirty:
                    for seg in d.split("/"):
                        seg = seg.strip()
                        if not seg:
                            continue
                        if _CJK_RE.search(seg):  # 中文段=方向词；纯 ASCII 段=项目名，丢弃
                            direction_words.add(seg)
                repo.tags = keep
                touched[repo.id] = keep
            session.commit()
        if not touched:
            emit("没有脏标签需要清洗")
            return {"cleaned": 0, "tags": []}
        emit(f"拆串完成：{len(touched)} 个项目带过脏标签，拆出方向词 {len(direction_words)} 个")

        # 2. 方向词过标签库归一（LLM 同义合并 + 登记）
        mapping = await ensure_tags(llm, sorted(direction_words), source="cleanup", log=emit)
        canonicals = sorted(set(mapping.values()))
        if canonicals:
            with SessionLocal() as session:
                for repo_id, keep in touched.items():
                    repo = session.get(Repo, repo_id)
                    if repo is not None:
                        repo.tags = sorted(set(keep + canonicals))
                session.commit()
            emit(f"清洗完成：{len(touched)} 个项目重打「{'、'.join(canonicals)}」标签")
        else:
            emit("清洗完成：无方向词可归一（项目名片段全部丢弃）")
        return {"cleaned": len(touched), "tags": canonicals}
    finally:
        await llm.close()
