"""课程发布存储：本地磁盘落盘（Linux 服务器建议指向 nginx 托管目录）。

发布根目录由 .env 的 LOCAL_PUBLISH_DIR 配置（默认 ./published，相对项目根解析）。
对外访问地址默认 {PLATFORM_API_BASE}/published ——
main.py 会把该目录挂成本服务的静态站点，无需任何配置即可访问；
nginx 前置时改 LOCAL_PUBLISH_BASE_URL 指向 nginx 的对外地址即可。

key 直接映射为 {发布根}/{key} 的文件路径，如
"3-{issue号}-{课程标题}/01-项目导览.html"。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from ..config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class StoreError(RuntimeError):
    """磁盘写入/删除失败（权限不足、磁盘满、路径不可达等）。"""


def root() -> Path:
    """发布根目录（相对路径按项目根解析；不存在则创建）。"""
    configured = Path(get_settings().local_publish_dir)
    path = configured if configured.is_absolute() else PROJECT_ROOT / configured
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path_for(key: str) -> Path:
    """key → 本地文件路径。key 由平台 safe_name 生成，这里再挡一层路径穿越。"""
    parts = [p for p in key.split("/") if p not in ("", ".", "..")]
    return root().joinpath(*parts)


def is_enabled() -> bool:
    """磁盘存储始终可用（目录有默认值），保留该函数供上层语义判断。"""
    return bool(get_settings().local_publish_dir)


def public_url(key: str) -> str:
    """发布文件的对外访问地址；中文文件名按 UTF-8 百分号编码，斜杠保留做层级。"""
    s = get_settings()
    base = s.local_publish_base_url or f"{s.platform_api_base.rstrip('/')}/published"
    return f"{base.rstrip('/')}/{quote(key, safe='/')}"


def put_bytes(key: str, data: bytes) -> str:
    """写一个文件到发布目录，返回对外 URL。目录逐级自动创建，同 key 覆盖。"""
    path = _path_for(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        raise StoreError(f"写入 {path} 失败：{exc}") from exc
    return public_url(key)


def delete_key(key: str) -> None:
    """删除单个文件（重发课程时清掉上一版残留）；不存在则忽略。"""
    try:
        _path_for(key).unlink(missing_ok=True)
    except OSError as exc:
        raise StoreError(f"删除 {key} 失败：{exc}") from exc


def list_prefix(prefix: str = "", limit: int = 1000) -> list[str]:
    """列出某前缀下的所有 key（相对发布根的 posix 路径，不含目录本身）。"""
    base = root()
    start = _path_for(prefix) if prefix else base
    if not start.exists():
        return []
    keys = [p.relative_to(base).as_posix() for p in start.rglob("*") if p.is_file()]
    return sorted(keys)[:limit]
