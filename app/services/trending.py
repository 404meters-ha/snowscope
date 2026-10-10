"""GitHub Trending 抓取：HTML 解析（weekly/monthly 各前 50）+ Search API 兜底。

GitHub 没有官方 Trending API，页面改版可能挂——解析失败或条目不足时自动降级
到 Search API（按 star 排序的近期高星仓库），并在返回里标注 source 便于排查。
"""
import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from ..config import get_settings
from .github_client import GitHubClient

logger = logging.getLogger(__name__)

TRENDING_URL = "https://github.com/trending"


class TrendingFetcher:
    def __init__(self, github: GitHubClient) -> None:
        self._github = github

    async def fetch(self, since: str, *, limit: int = 50) -> list[dict]:
        """since: weekly | monthly。返回 [{full_name, description, language, stars, period_stars, source}]。"""
        items = await self._scrape(since, limit=limit)
        if items:
            return items
        logger.warning("trending 页面解析为空（%s），降级 Search API 兜底", since)
        return await self._fallback(since, limit=limit)

    async def _scrape(self, since: str, *, limit: int) -> list[dict]:
        settings = get_settings()
        proxy = settings.github_proxy or None
        seen: dict[str, dict] = {}
        # trending 每页 25 条，翻 2 页凑前 50；第 2 页挂了不影响第 1 页
        for page in (1, 2):
            if len(seen) >= limit:
                break
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=30, follow_redirects=True) as client:
                    resp = await client.get(
                        TRENDING_URL,
                        params={"since": since, "page": page} if page > 1 else {"since": since},
                        headers={"User-Agent": "Mozilla/5.0 (celestial-snow)"},
                    )
                    if resp.status_code != 200:
                        logger.warning("trending 第 %s 页返回 %s", page, resp.status_code)
                        continue
                    self._parse_html(resp.text, since, seen)
            except httpx.HTTPError as e:
                logger.warning("trending 第 %s 页抓取失败: %s", page, e)
        return list(seen.values())[:limit]

    def _parse_html(self, html: str, since: str, seen: dict[str, dict]) -> None:
        soup = BeautifulSoup(html, "html.parser")
        for article in soup.select("article.Box-row"):
            a = article.select_one("h2 a[href]")
            if not a:
                continue
            href = a.get("href", "").strip("/")
            if not href or "/" not in href:
                continue
            if href in seen:
                continue
            lang_el = article.select_one('[itemprop="programmingLanguage"]')
            stars = 0
            for link in article.select("a.Link--muted"):
                text = link.get_text(strip=True).replace(",", "")
                if text.isdigit():
                    stars = int(text)
                    break
            period_stars = 0
            period_el = article.find(string=re.compile(r"stars this (week|month|day)"))
            if period_el:
                m = re.search(r"([\d,]+)", str(period_el))
                if m:
                    period_stars = int(m.group(1).replace(",", ""))
            desc_el = article.select_one("p")
            seen[href] = {
                "full_name": href,
                "description": desc_el.get_text(" ", strip=True) if desc_el else "",
                "language": lang_el.get_text(strip=True) if lang_el else "",
                "stars": stars,
                "period_stars": period_stars,
                "source": f"trending_{since}",
            }

    async def _fallback(self, since: str, *, limit: int) -> list[dict]:
        """Search API 兜底：近 30 天创建的高星（weekly 口径）/ 成熟高星池（monthly 口径）。"""
        try:
            if since == "weekly":
                raw = await self._github.search_new_repos(days=30, per_page=min(limit, 100))
            else:
                raw = await self._github.search_by_stars(min_stars=2000, per_page=min(limit, 100))
        except Exception as e:  # noqa: BLE001 兜底也挂就真的没了
            logger.error("Search API 兜底也失败: %s", e)
            return []
        return [
            {
                "full_name": r["full_name"],
                "description": r.get("description") or "",
                "language": r.get("language") or "",
                "stars": r.get("stargazers_count", 0),
                "period_stars": 0,
                "source": f"search_fallback_{since}",
            }
            for r in raw[:limit]
        ]


async def main_smoke() -> None:  # pragma: no cover 手工冒烟用
    logging.basicConfig(level=logging.INFO)
    gh = GitHubClient()
    try:
        for since in ("weekly", "monthly"):
            items = await TrendingFetcher(gh).fetch(since)
            print(f"{since}: {len(items)} repos, top3 = {[i['full_name'] for i in items[:3]]}")
    finally:
        await gh.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main_smoke())
