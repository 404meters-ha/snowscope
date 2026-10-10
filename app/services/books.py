"""教材（电子书 PDF）解析与教程生成调度。

上传的 PDF 逐页抽文本（pypdf）；扫描页（无文本层）渲染成 PNG 送视觉大模型转录
（LLM_VISION_MODEL，flash 档便宜量大）。逐页文本落 uploads/books/{id}/text/pNNNN.txt
（agent 经 ReadFile 读，uploads 已进 READ_ROOTS），再由 LLM 归纳学习大纲（章 → 页范围）。

课程生成本体在 /tech-book 技能里；本模块负责「把书变成可读素材」与「章节系列课的逐章调度」。
"""
import asyncio
import io
import re
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader
from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Book
from . import agent_sdk, llm, skill_runner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOOKS_ROOT = PROJECT_ROOT / "uploads" / "books"

MIN_PAGE_CHARS = 60  # 单页抽出字符低于此值视为扫描页，转走视觉转录
MIN_BOOK_CHARS = 500  # 全书抽完仍低于此值 → failed（无文本层且视觉转录也没救回来）
VISION_CONCURRENCY = 4  # 视觉转录并发（对端限流友好）
SAMPLE_STEP = 8  # 大纲抽样：每 N 页取一页开头
OCR_MAX_TOKENS = 8000  # 单页转录上限（密集教材页约 2-3k 字）

OCR_SYSTEM = "你是精确的 OCR 转录器。只输出页面上实际存在的文字，不总结、不翻译、不改写、不补充。"
OCR_PROMPT = (
    "转录这张教材页面图片里的全部文字。要求：保持原文的标题层级、段落与列表结构；"
    "公式用 LaTeX；表格转成 Markdown 表格；图表题注保留、图形本身用一句【图】占位描述；"
    "页眉页脚页码忽略。直接输出转录文本，不要任何说明。"
)


def book_dir(book_id: int) -> Path:
    return BOOKS_ROOT / str(book_id)


def _render_page(pdf_path: Path, page_no: int) -> bytes:
    """pypdfium2 渲染单页为 PNG（scale≈2 → 约 144dpi，flash 档识读够用）。"""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_no - 1]
        bitmap = page.render(scale=2.0)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


def _scan_pdf(pdf_path: Path) -> tuple[int, list[str], list[dict]]:
    """pypdf 全书扫描：每页文本 + 顶层书签（页码 1 起）。损坏页返回空文本不中断。"""
    reader = PdfReader(str(pdf_path))
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 单页解析失败不该拖垮整本书
            texts.append("")
    toc: list[dict] = []
    try:
        for item in reader.outline:
            if isinstance(item, list):  # 嵌套子书签拍平太麻烦，只取顶层
                continue
            try:
                toc.append({"title": str(item.title).strip(), "page": reader.get_destination_page_number(item) + 1})
            except Exception:  # noqa: BLE001 指向命名目标的坏书签跳过
                continue
    except Exception:  # noqa: BLE001 无书签/书签损坏不影响主流程
        pass
    return len(texts), texts, [t for t in toc if t["title"]]


async def _ocr_pages(client: llm.LLMClient, pdf_path: Path, pending: list[int], progress, log) -> dict[int, str]:
    """并发视觉转录：返回 {页码: 文本}；单页失败记日志跳过（对应页保持空）。"""
    sem = asyncio.Semaphore(VISION_CONCURRENCY)
    done = {"n": 0}

    async def one(no: int) -> tuple[int, str] | None:
        async with sem:
            try:
                png = await asyncio.to_thread(_render_page, pdf_path, no)
                text = await client.chat_vision(OCR_SYSTEM, OCR_PROMPT, png, max_tokens=OCR_MAX_TOKENS)
            except Exception as e:  # noqa: BLE001 单页失败不拖垮整本
                log(f"✗ 第 {no} 页转录失败：{str(e)[:120]}")
                return None
            done["n"] += 1
            if done["n"] % 10 == 0 or done["n"] == len(pending):
                progress(f"视觉转录 {done['n']}/{len(pending)} 页")
            return no, text.strip()

    results = await asyncio.gather(*(one(n) for n in pending))
    return {r[0]: r[1] for r in results if r}


def _fallback_chapters(toc: list[dict], pages: int) -> list[dict]:
    """大纲 LLM 失败时的兜底：有书签按书签切，无书签按 50 页一块均分。"""
    if len(toc) >= 2:
        chapters = []
        for i, t in enumerate(toc):
            start = max(1, t["page"])
            end = toc[i + 1]["page"] - 1 if i + 1 < len(toc) else pages
            if end < start:
                end = start
            chapters.append({"no": i + 1, "title": t["title"] or f"第{i + 1}部分",
                             "start": start, "end": min(end, pages),
                             "summary": "", "why": ""})
        return chapters
    blocks = max(1, (pages + 49) // 50)
    return [{"no": i + 1, "title": f"第 {i * 50 + 1}-{min((i + 1) * 50, pages)} 页",
             "start": i * 50 + 1, "end": min((i + 1) * 50, pages),
             "summary": "", "why": ""} for i in range(blocks)]


def _normalize_chapters(raw: list, pages: int) -> list[dict]:
    """LLM 大纲清洗：页码夹进 [1, pages]、start<=end、补章号、按页码排序。"""
    chapters = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        try:
            start = max(1, int(c.get("start") or 1))
            end = min(pages, int(c.get("end") or pages))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        title = str(c.get("title") or "").strip() or f"第{i + 1}部分"
        chapters.append({"no": i + 1, "title": title[:120], "start": start, "end": end,
                         "summary": str(c.get("summary") or "")[:400],
                         "why": str(c.get("why") or "")[:200]})
    return sorted(chapters, key=lambda c: c["start"]) or _fallback_chapters([], pages)


async def extract_pipeline(book_id: int, task_id: str, progress, log) -> dict:
    """教材解析任务：抽文本（扫描页视觉转录）→ 落逐页文件 → LLM 学习大纲 → status=ready。"""
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None:
            raise RuntimeError(f"教材 {book_id} 不存在")
        title, filename = book.title, book.filename
        book.status, book.note = "extracting", ""
        session.commit()
    pdf_path = book_dir(book_id) / "book.pdf"
    if not pdf_path.is_file():
        await _mark_failed(book_id, "PDF 文件缺失（uploads/books/ 下没有 book.pdf）")
        raise RuntimeError("PDF 文件缺失")

    try:
        progress("扫描 PDF…")
        pages, texts, toc = await asyncio.to_thread(_scan_pdf, pdf_path)
        log(f"共 {pages} 页；文本层直抽 {sum(1 for t in texts if len(t) >= MIN_PAGE_CHARS)} 页；"
            f"PDF 书签 {len(toc)} 条")
        pending = [i for i, t in enumerate(texts, start=1) if len(t) < MIN_PAGE_CHARS]
        vision_failed = 0
        if pending:
            if not get_settings().llm_configured:
                await _mark_failed(book_id, "扫描页需要视觉模型转录，但 LLM 未配置")
                raise RuntimeError("LLM 未配置，无法转录扫描页")
            log(f"{len(pending)} 页疑似扫描页，送视觉模型转录（并发 {VISION_CONCURRENCY}）…")
            client = llm.LLMClient()
            try:
                transcribed = await _ocr_pages(client, pdf_path, pending, progress, log)
            finally:
                await client.close()
            for no in pending:
                if no in transcribed and transcribed[no]:
                    texts[no - 1] = transcribed[no]
                else:
                    vision_failed += 1
        text_dir = book_dir(book_id) / "text"
        text_dir.mkdir(parents=True, exist_ok=True)
        for no, txt in enumerate(texts, start=1):
            (text_dir / f"p{no:04d}.txt").write_text(txt, encoding="utf-8")
        chars = sum(len(t) for t in texts)
        if chars < MIN_BOOK_CHARS:
            note = (f"全书只抽出 {chars} 字符：既无文本层，视觉转录也没成功——"
                    f"请确认 PDF 未加密损坏，或先转成文本版再上传")
            await _mark_failed(book_id, note)
            raise RuntimeError(note)
        # 书名空则从第 1 页推断（封面/扉页），推断不出来保持原文件名
        if not title:
            cover = texts[0][:200] or (texts[1][:200] if pages > 1 else "")
            title = re.sub(r"\s+", " ", cover).strip(" .-·")[:80] or filename
        progress("归纳学习大纲…")
        sampled = [{"page": no, "head": texts[no - 1][:400]}
                   for no in range(1, pages + 1, max(1, SAMPLE_STEP)) if texts[no - 1]]
        try:
            client = llm.LLMClient()
            try:
                data = await llm.book_outline(client, title, pages, toc, sampled)
            finally:
                await client.close()
            chapters = _normalize_chapters(data.get("chapters", []), pages)
        except Exception as e:  # noqa: BLE001 大纲失败降级兜底，不让整本白抽
            log(f"大纲归纳失败（{str(e)[:120]}），退化为书签/均匀分块")
            chapters = _fallback_chapters(toc, pages)
        stats = {"chars": chars, "vision_pages": len(pending) - vision_failed,
                 "vision_failed": vision_failed}
        with SessionLocal() as session:
            book = session.get(Book, book_id)
            book.title, book.pages, book.stats = title, pages, stats
            book.outline = {"chapters": chapters}
            book.status, book.note = "ready", ""
            session.commit()
        progress(f"解析完成：{pages} 页 / {len(chapters)} 章")
        return {"book_id": book_id, "pages": pages, **stats, "chapters": len(chapters)}
    except Exception as e:  # noqa: BLE001 任何异常都落到 failed，任务层再抛
        await _mark_failed(book_id, str(e)[:500])
        raise


async def _mark_failed(book_id: int, note: str) -> None:
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is not None:
            book.status, book.note = "failed", note
            session.commit()


async def series_pipeline(book_id: int, chapter_nos: list[int], task_id: str, progress, log) -> dict:
    """章节系列课：对选中章逐章跑 /tech-book（单章模式），一章一门课，串行不问询。"""
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None or book.status != "ready":
            raise RuntimeError("教材不存在或尚未解析完成（等解析任务成功后再发起系列课）")
        chapters = {c.get("no"): c for c in (book.outline or {}).get("chapters", [])}
    todo = [(no, chapters[no]) for no in chapter_nos if no in chapters]
    for no in [n for n in chapter_nos if n not in chapters]:
        log(f"章号 {no} 不在大纲里，跳过")
    if not todo:
        raise RuntimeError("没有可生成的章节（章号都不在大纲里）")
    s = get_settings()
    ok: list[int] = []
    failed: list[int] = []
    for i, (no, ch) in enumerate(todo, start=1):
        progress(f"章节课 {i}/{len(todo)}：第{no}章 {ch.get('title', '')}")
        log(f"—— 第 {i}/{len(todo)} 门：第{no}章 {ch.get('title', '')} ——")
        try:
            prompt = skill_runner.resolve_skill("tech-book", f"{book_id} 第{no}章")
            res = await agent_sdk.run(
                prompt, progress, log=log, timeout=s.skill_run_timeout,
                task_id=task_id, interactive=False, max_tokens=16384,
            )
            ok.append(no)
            log(f"✓ 第{no}章课程完成（{res.get('num_turns')} 轮 / ${res.get('cost_usd', 0):.4f}）")
        except Exception as e:  # noqa: BLE001 单章失败继续后面的章
            failed.append(no)
            log(f"✗ 第{no}章课程失败：{str(e)[:160]}")
    progress(f"系列课完成：成功 {len(ok)} 门、失败 {len(failed)} 门")
    return {"book_id": book_id, "ok": ok, "failed": failed}
