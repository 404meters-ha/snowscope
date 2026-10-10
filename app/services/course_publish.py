"""把 /tech 生成的课程目录发布到服务器本地磁盘（LOCAL_PUBLISH_DIR，默认 ./published）。

本地目录 courses/{id}/ 是按 slug 命名的（01-overview.html），直接落盘在发布目录里没法看。这里做三件事：

1. **分文件夹**：整门课进 `{course_id}-{repo}-issue{n}-{课程标题}/`，同名课程互不干扰。
2. **重命名成可理解的**：课件文件用注册课程时的中文课标题命名（01-overview.html → 01-项目导览.html），
   index.html → 00-课程目录.html；assets/ 与 reference/ 保持原路径（web 约定，改名只会徒增坏链风险）。
3. **改写链接**：页面之间是用相对链接互指的（index.html → 01-overview.html），改名后必须同步改写
   href/src，否则发布目录里的导航全断。

顺带在静态副本里注入 `window.CELESTIAL_PUBLISHED = true`：quiz.js 据此说明「静态副本不计入平台进度」——
发布目录的页面调不到平台 API，否则会显示成「平台未启动」的误导提示。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import store

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 改写页面内的相对链接（href/src），只动指向顶层课件文件的那部分
_ATTR_RE = re.compile(r'(?P<attr>\bhref|\bsrc)(?P<eq>\s*=\s*)(?P<q>["\'])(?P<url>[^"\']*)(?P=q)')
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
# URL 路径里需要转义的字符：留在文件名里会让链接解析出错（'#' 是片段分隔符，实测会把链接截断成 403）
_URL_UNSAFE = re.compile(r"[%\[\]{}^`]+")
_HEAD_RE = re.compile(r"</head>", re.I)
_INDEX_NAME = "00-课程目录.html"
# 课标题常写成「第一章 · 项目导览：…」；文件名已有 01/02 序号，章前缀冗余且把名字撑得很长
_CHAPTER_RE = re.compile(r"^第\s*[一二三四五六七八九十百\d]+\s*章\s*[·\-—:：|]*\s*")

PUBLISHED_FLAG = "<script>window.CELESTIAL_PUBLISHED = true;</script>"


def safe_name(text: str, fallback: str = "untitled", max_len: int = 80) -> str:
    """把标题变成可安全用于文件名/key 的名字。

    三类字符要处理：文件系统非法字符（/ : * ? 等）、URL 路径需转义字符（% [ ] 等）、
    以及 '#'——它是 URL 片段分隔符，留在文件名里链接会被浏览器截断（实测链接直接断掉）。
    '#' 直接删掉而非换成 '-'，因为课标题里的它几乎都写作「issue #4686」，删掉才读得顺。
    """
    name = (text or "").replace("#", "")
    name = _URL_UNSAFE.sub("-", name)
    name = _ILLEGAL.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    name = name[:max_len].strip(" .-")
    return name or fallback


def course_dir_for(course_id: int) -> Path:
    """课程 HTML 的本地根目录（与 main.py 托管 /courses 的目录一致）。"""
    return PROJECT_ROOT / "courses" / str(course_id)


def key_root_for(course) -> str:
    """发布目录下的课程子目录（作为 key 前缀）：{folder}/。"""
    return f"{folder_name(course)}/"


def folder_name(course) -> str:
    """课程在发布目录里的文件夹名（按来源分支）：

    issue：{id}-{repo}-issue{n}-{标题}；repo：{id}-{repo}-{标题}；
    book：{id}-教材-{标题}（标题里通常已带书名·章节）；import：{id}-{标题}。
    """
    source = getattr(course, "source_type", None) or "issue"
    issue_no = course.issue.number if course.issue else None
    parts = [str(course.id)]
    if course.repo:  # issue / repo 课都有仓库段
        parts.append(safe_name(course.repo.full_name.split("/")[-1] or "repo", "repo", 40))
        if source == "issue" and issue_no is not None:
            parts.append(f"issue{issue_no}")
    elif source == "book":
        parts.append("教材")
    parts.append(safe_name(course.title, "课程", 60))
    return safe_name("-".join(parts), f"course-{course.id}")


def lesson_renames(course) -> dict[str, str]:
    """课件文件改名映射：原文件名 → 中文课标题文件名。

    序号沿用原文件名开头的数字（01-overview.html → 01-项目导览.html）；index.html 固定为 00-课程目录.html。
    课标题重复时补位序号，保证映射值唯一（否则会互相覆盖）。
    """
    renames: dict[str, str] = {"index.html": _INDEX_NAME}
    used: set[str] = {_INDEX_NAME}
    for idx, lesson in enumerate(course.lessons or [], start=1):
        src_file = str(lesson.get("file") or "").strip()
        if not src_file:
            continue
        src_name = Path(src_file).name
        m = re.match(r"^(\d+)", src_name)
        seq = m.group(1) if m else f"{idx:02d}"
        raw_title = str(lesson.get("title") or Path(src_name).stem)
        title = safe_name(_CHAPTER_RE.sub("", raw_title), Path(src_name).stem, 48)
        candidate = f"{seq}-{title}.html"
        n = 2
        while candidate in used:
            candidate = f"{seq}-{title}-{n}.html"
            n += 1
        used.add(candidate)
        renames[src_name] = candidate
    return renames


def rewrite_links(html: str, renames: dict[str, str]) -> str:
    """改写页面内的相对链接：只认顶层课件文件，外链/锚点/子目录链接原样保留。"""

    def repl(m: re.Match) -> str:
        url = m.group("url")
        if not url or url.startswith(("#", "/", "http://", "https://", "mailto:", "data:")):
            return m.group(0)
        path, sep, frag = url.partition("#")
        if not path:
            return m.group(0)
        name = Path(path).name
        target = renames.get(name)
        if target is None:
            return m.group(0)
        # 链接可能写成 ./01-overview.html 这种形式：只替换文件名部分，保留前缀
        new_path = path[: len(path) - len(name)] + target
        return f'{m.group("attr")}{m.group("eq")}{m.group("q")}{new_path}{sep}{frag}{m.group("q")}'

    return _ATTR_RE.sub(repl, html)


def _inject_flag(html: str) -> str:
    """给静态副本打标记（只影响上传副本，本地课程文件保持原样）。"""
    if "CELESTIAL_PUBLISHED" in html:
        return html
    if _HEAD_RE.search(html):
        return _HEAD_RE.sub(f"{PUBLISHED_FLAG}\n</head>", html, count=1)
    return PUBLISHED_FLAG + "\n" + html


def _transform(rel_dst: str, data: bytes, renames: dict[str, str]) -> bytes:
    if not rel_dst.lower().endswith((".html", ".htm")):
        return data
    html = _inject_flag(rewrite_links(data.decode("utf-8"), renames))
    return html.encode("utf-8")


def publish_course(course, course_dir: Path | None = None, prune: bool = False) -> dict:
    """把课程目录整体落盘到发布目录，返回发布清单。

    prune=True 时删除该课程文件夹下本次未写入的旧文件（重生成课程后清理残留）。
    """
    course_dir = Path(course_dir) if course_dir else course_dir_for(course.id)
    if not course_dir.is_dir():
        raise FileNotFoundError(f"课程目录不存在：{course_dir}")

    renames = lesson_renames(course)
    key_root = key_root_for(course)
    files: list[dict] = []

    for src in sorted(course_dir.rglob("*")):
        if not src.is_file():
            continue
        rel_src = src.relative_to(course_dir).as_posix()
        if any(part.startswith(".") for part in src.relative_to(course_dir).parts):
            continue
        # 只有顶层课件文件改名；子目录（assets/ reference/ learning-records/）按原路径写入
        rel_dst = renames[rel_src] if "/" not in rel_src and rel_src in renames else rel_src
        try:
            data = _transform(rel_dst, src.read_bytes(), renames)
        except UnicodeDecodeError as exc:
            # 带上文件名，否则只看到一段 codec 报错不知道是哪一课
            raise ValueError(f"{rel_src} 不是 UTF-8 编码，无法改写链接：{exc}") from exc
        key = key_root + rel_dst
        url = store.put_bytes(key, data)
        files.append({
            "source": rel_src,
            "key": key,
            "name": rel_dst,
            "url": url,
            "bytes": len(data),
            "renamed": rel_src != rel_dst,
        })

    pruned: list[str] = []
    if prune:
        current = {f["key"] for f in files}
        for key in store.list_prefix(key_root):
            if key not in current:
                store.delete_key(key)
                pruned.append(key)

    entry = next((f for f in files if f["name"] == _INDEX_NAME), None)
    entry = entry or next((f for f in files if f["name"].endswith(".html")), None)
    return {
        "folder": key_root.rstrip("/").split("/")[-1],
        "key_root": key_root,
        "storage": "local-disk",
        "dir": str(store.root()),
        "entry_url": entry["url"] if entry else "",
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "renamed": {k: v for k, v in renames.items() if k != v},
        "pruned": pruned,
    }
