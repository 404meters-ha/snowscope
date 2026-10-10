"""S6 技术验证门 dry-run：示例需求「待办事项 Web 应用」走一遍 Agent 生成。

用法：项目根执行 .venv/Scripts/python.exe scripts/scaffold_dryrun.py
产物落 scaffolds/workspace/dryrun/，人工核验验收线后结论写 doc/scaffold/validation.md。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台防 GBK 乱码

from app.services import agent_sdk, scaffold_builder  # noqa: E402

WORKSPACE = "scaffolds/workspace/dryrun"

# 模拟 V2 链路产出（拆条 + 选型）的输入数据
RAW_TEXT = "待办事项 Web 应用：能添加、完成、删除待办，浏览器访问"
NEED_BRIEF = {
    "用途": "个人待办管理",
    "核心功能": "添加/完成/删除待办，浏览器访问",
    "规模": "单机单用户",
    "技术偏好": "Python 后端 + 轻量前端，零构建优先",
}
ITEMS = [
    {"no": 1, "name": "Web 后端框架", "desc": "提供 HTTP API 与静态页面托管",
     "keywords": ["fastapi", "python", "web"],
     "selected": "tiangolo/fastapi", "reason": "适配度 92：Python 技术栈命中，API 文档自动生成，生态成熟"},
    {"no": 2, "name": "前端页面", "desc": "待办列表交互界面",
     "keywords": ["vue", "frontend"],
     "selected": "vuejs/core", "reason": "适配度 85：渐进式框架，CDN 引入零构建即可用"},
    {"no": 3, "name": "数据存储", "desc": "待办条目持久化",
     "keywords": ["sqlite", "storage"],
     "selected": None, "reason": "自研：Python 标准库 sqlite3 足够，无需引组件"},
]
BASE_HINT = ("FastAPI 为主干（base），路由与静态托管都在它上面；前端页面由 FastAPI 托管，"
             "Vue 3 走 CDN 引入；SQLite 模块作为 service 层挂载。")


async def main() -> None:
    prompt = scaffold_builder.build_prompt(RAW_TEXT, NEED_BRIEF, ITEMS, BASE_HINT, WORKSPACE)
    result = await agent_sdk.run(
        prompt,
        progress=lambda m: None,
        log=print,
        timeout=3600,
        max_turns=scaffold_builder.BUILD_MAX_TURNS,
        max_tokens=scaffold_builder.BUILD_MAX_TOKENS,
        system_prompt=scaffold_builder.SCAFFOLD_SYSTEM,
    )
    print("\n" + "=" * 60)
    print(f"轮次 {result['num_turns']}｜成本 ${result['cost_usd']:.2f}｜"
          f"耗时 {result['duration_ms'] / 1000:.0f}s")
    print("=" * 60)
    print(result["result"][-1500:])


if __name__ == "__main__":
    asyncio.run(main())
