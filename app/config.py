"""全局配置：.env + 环境变量，全部可通过 .env 覆盖。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitHub
    github_token: str = ""
    github_proxy: str = ""  # 空则不启用 trending 网页爬取通道

    # LLM（OpenAI 兼容）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "glm-4.7"
    # 视觉模型（扫描版 PDF 逐页转录用）：空 = 回退主模型（不支持图片则转录失败）
    llm_vision_model: str = "glm-5.3-flash"

    # 搜索
    search_provider: str = ""  # bocha | tavily | zai | 空
    search_api_key: str = ""

    # 用户画像（贡献方向推荐与匹配度评分用）——建议在 .env 里改成你自己的背景
    user_profile: str = "全栈开发者，关注 AI 应用与开发者工具方向"
    # 技能清单（issue 规则预分关键词 + LLM 匹配提示），逗号分隔
    user_skills: str = "Python,JavaScript,FastAPI,Vue,LLM,AI Agent,RAG,后端,前端,开发者工具"

    # 规模
    candidate_pool: int = 100
    top_n_llm: int = 30
    sched_hour: int = 8

    # 数据库
    db_url: str = "sqlite:///./celestial.db"

    # 内置 Agent SDK（vendored cc-mini，in-process）
    skill_run_timeout: int = 3600  # 单次 agent/技能执行超时（秒）
    # PlatformAPI 工具访问的本服务地址（云端部署时改成对外域名）
    platform_api_base: str = "http://127.0.0.1:8100"

    # 子路径部署（如 https://域名/celestial-snow/）：main.py 的中间件剥掉请求路径里的该前缀，
    # 前端构建 base 也用它（vite.config.js 读同一份 .env）。空 = 根路径部署（本地默认）
    base_path: str = ""

    # 课程发布：本地磁盘落盘（Linux 服务器建议指向 nginx 托管目录；
    # base_url 留空时由本服务静态托管在 /published，见 main.py）
    local_publish_dir: str = "./published"  # 发布根目录：相对项目根或绝对路径
    local_publish_base_url: str = ""  # 对外访问前缀；空 = {platform_api_base}/published

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key)

    @property
    def search_configured(self) -> bool:
        return bool(self.search_provider and self.search_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
