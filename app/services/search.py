"""搜索 provider：企业落地场景验证（bocha | tavily | zai），可配置，未配置则降级为只用 README 声明。"""
import httpx

from ..config import get_settings


class SearchClient:
    def __init__(self) -> None:
        s = get_settings()
        self._provider = s.search_provider.lower()
        self._api_key = s.search_api_key

    @property
    def configured(self) -> bool:
        return bool(self._provider and self._api_key)

    async def search(self, query: str, *, count: int = 5) -> list[dict]:
        """返回 [{title, snippet, url}]；未配置或失败返回空（保守模式不阻塞）。"""
        if not self.configured:
            return []
        try:
            if self._provider == "bocha":
                return await self._bocha(query, count)
            if self._provider == "tavily":
                return await self._tavily(query, count)
            if self._provider == "zai":
                return await self._zai(query, count)
        except Exception:  # noqa: BLE001 搜索是增强项，任何失败都不阻塞流水线
            return []
        return []

    async def _bocha(self, query: str, count: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.bochaai.com/v1/web-search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "count": count, "summary": True},
            )
            resp.raise_for_status()
            pages = resp.json().get("data", {}).get("webPages", {}).get("value", [])
            return [
                {"title": p.get("name", ""), "snippet": p.get("summary") or p.get("snippet", ""), "url": p.get("url", "")}
                for p in pages[:count]
            ]

    async def _tavily(self, query: str, count: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": self._api_key, "query": query, "max_results": count},
            )
            resp.raise_for_status()
            return [
                {"title": r.get("title", ""), "snippet": r.get("content", "")[:400], "url": r.get("url", "")}
                for r in resp.json().get("results", [])[:count]
            ]

    async def _zai(self, query: str, count: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://open.bigmodel.cn/api/paas/v4/web_search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"search_engine": "search_std", "search_query": query[:100]},
            )
            resp.raise_for_status()
            data = resp.json().get("search_result", [])
            return [
                {"title": d.get("title", ""), "snippet": d.get("content", "")[:400], "url": d.get("link", "")}
                for d in data[:count]
            ]
