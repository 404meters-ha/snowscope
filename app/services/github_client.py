"""GitHub REST API 客户端（httpx 异步）。所有需要 PAT 的调用集中在此。"""
import base64
import time

import httpx

from ..config import get_settings


class GitHubRateLimitError(RuntimeError):
    pass


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self) -> None:
        settings = get_settings()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._client = httpx.AsyncClient(
            base_url=self.BASE, headers=headers, timeout=30, follow_redirects=True
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        resp = await self._client.get(path, params=params)
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = int(resp.headers.get("x-ratelimit-reset", "0"))
            wait_min = max(1, int(reset - time.time()) // 60)
            raise GitHubRateLimitError(f"GitHub API 限流，约 {wait_min} 分钟后重置（请配置 GITHUB_TOKEN）")
        resp.raise_for_status()
        return resp

    # ---------- 采集 ----------

    async def search_new_repos(self, *, days: int = 30, per_page: int = 100) -> list[dict]:
        """近 N 天创建、按 star 降序——GitHub Trending 的主要构成。"""
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        resp = await self._get(
            "/search/repositories",
            params={"q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": per_page},
        )
        return resp.json().get("items", [])

    async def search_by_stars(self, *, min_stars: int = 2000, per_page: int = 100) -> list[dict]:
        """成熟高星仓库池（用于快照增量 accumulate 后的月榜补充）。"""
        resp = await self._get(
            "/search/repositories",
            params={"q": f"stars:>{min_stars}", "sort": "stars", "order": "desc", "per_page": per_page},
        )
        return resp.json().get("items", [])

    async def search_repos(self, query: str, *, per_page: int = 30) -> list[dict]:
        """通用关键词搜索（定向行业分析用）：query 为完整搜索表达式，按 star 降序。"""
        resp = await self._get(
            "/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": min(per_page, 100)},
        )
        return resp.json().get("items", [])

    async def get_repo(self, full_name: str) -> dict:
        return (await self._get(f"/repos/{full_name}")).json()

    async def get_contributors_count(self, full_name: str) -> int:
        resp = await self._client.get(
            f"/repos/{full_name}/contributors", params={"per_page": 100, "anon": "true"}
        )
        resp.raise_for_status()
        items = resp.json()
        # Link header 最后一页页码 × 100 估算更多贡献者
        link = resp.headers.get("link", "")
        if 'rel="next"' in link:
            import re

            m = re.search(r'page=(\d+)>; rel="last"', link)
            if m:
                return (int(m.group(1)) - 1) * 100 + len(items)
        return len(items)

    # ---------- Issue / 贡献分析 ----------

    async def get_open_issues(self, full_name: str, *, limit: int = 100) -> list[dict]:
        """open issues（剔除 PR），按最近更新排序。"""
        resp = await self._get(
            f"/repos/{full_name}/issues",
            params={"state": "open", "sort": "updated", "direction": "desc", "per_page": min(limit, 100)},
        )
        return [i for i in resp.json() if "pull_request" not in i][:limit]

    async def get_issue(self, full_name: str, number: int) -> dict:
        return (await self._get(f"/repos/{full_name}/issues/{number}")).json()

    async def get_issue_comments(self, full_name: str, number: int, *, limit: int = 20) -> list[dict]:
        resp = await self._get(
            f"/repos/{full_name}/issues/{number}/comments", params={"per_page": min(limit, 100)}
        )
        return resp.json()[:limit]

    async def get_closed_issues(self, full_name: str, *, limit: int = 100) -> list[dict]:
        """最近更新的 closed issues（剔除 PR）——复查库内仍标 open 的记录是否已被关闭。

        关闭动作会更新 updated_at，自上次摄入以来关掉的 issue 必在最近这页；
        单轮关闭数超 100 的极端情况留待下轮刷新收敛。
        """
        resp = await self._get(
            f"/repos/{full_name}/issues",
            params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": min(limit, 100)},
        )
        return [i for i in resp.json() if "pull_request" not in i][:limit]

    async def search_merged_fix_prs(self, full_name: str, number: int) -> int | None:
        """搜已合并、正文提到该 issue 的 PR → 修复 PR 号（无则 None）。

        不走 issue timeline：实测带 token 请求 timeline 会隐去 cross-referenced 事件
        （匿名请求才有，原因未知），search 一次调用直接带 pull_request.merged_at，鉴权下稳定。
        """
        resp = await self._get(
            "/search/issues",
            params={"q": f"repo:{full_name} is:pr is:merged {number} in:body", "per_page": 10},
        )
        items = [i for i in resp.json().get("items", []) if (i.get("pull_request") or {}).get("merged_at")]
        return items[0]["number"] if items else None

    # ---------- 文档 ----------

    async def get_readme(self, full_name: str) -> str:
        data = (await self._get(f"/repos/{full_name}/readme")).json()
        return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")

    async def get_contributing(self, full_name: str) -> str:
        for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
                     ".github/CONTRIBUTING.rst", "contributing.md"):
            resp = await self._client.get(f"/repos/{full_name}/contents/{path}")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("encoding") == "base64":
                    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")[:8000]
        return ""
