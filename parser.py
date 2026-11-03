"""德语文档解析：PDF / EPUB / TXT。

功能：
* 编码探测，完整保留德语变音符 ä ö ü ß；
* PDF 按文本块提取，自动剔除重复页眉页脚；
* 德语跨行连字符（Silbentrennung）修复；
* 智能分章：PDF 书签 → EPUB TOC → 德语标题正则 → 每 ~3000 词自动分节；
* 输出清洗后的自然段列表，供数据库与前端稳定计算字符偏移。
"""

from __future__ import annotations

import logging
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # PyMuPDF ≥ 1.24 推荐 pymupdf；老版本仅有 fitz
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from bs4 import BeautifulSoup
from ebooklib import epub

log = logging.getLogger("reader.parser")

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".epub", ".md", ".text"}
TARGET_SECTION_WORDS = 3000
MIN_CHAPTER_WORDS = 25


class ParserError(Exception):
    """解析失败时抛出的可读错误。"""

    def __init__(self, message: str, *, code: str = "parse_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class ParsedChapter:
    title: str
    paragraphs: list[str]
    source: str = ""

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def word_count(self) -> int:
        return count_words(self.text)


@dataclass
class ParsedBook:
    title: str
    author: str | None
    file_type: str
    chapters: list[ParsedChapter]
    language: str = "de"
    encoding: str | None = None
    toc_source: str = "auto"
    metadata_source: str = "filename"
    filename: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(ch.word_count for ch in self.chapters)

    @property
    def excerpt(self) -> str:
        parts: list[str] = []
        total = 0
        for chapter in self.chapters:
            for paragraph in chapter.paragraphs:
                parts.append(paragraph)
                total += len(paragraph)
                if total >= 1500:
                    return "\n\n".join(parts)[:1500]
        return "\n\n".join(parts)[:1500]


# --------------------------------------------------------------------------- #
# 文本清洗与断词
# --------------------------------------------------------------------------- #
_SOFT_HYPHEN_RE = re.compile(r"\u00ad")
_SPACE_RE = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
    "\u201e": "„",
    "\u201c": "“",
    "\u201d": "”",
    "\u2018": "‘",
    "\u2019": "’",
}
_LIGATURE_TABLE = str.maketrans(_LIGATURES)


def normalize_text(text: str) -> str:
    """统一换行、清除软连字符/零宽字符、修复常见连字。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u2028", "\n").replace("\u2029", "\n")
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = text.translate(_LIGATURE_TABLE)
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


def repair_hyphenation(text: str) -> str:
    """修复德语跨行断词（Silbentrennung）。

    * ``Wissen-\\nschaft`` → ``Wissenschaft``
    * ``Goethe-\\nInstitut`` → ``Goethe-Institut``（专名保留连字符）
    * ``WISSEN-\\nSCHAFT`` → ``WISSENSCHAFT``（全大写标题）
    * 软连字符 `\\u00ad` 位于行尾时直接拼接两半。
    """
    # 软连字符 + 换行：两部分应无缝拼接（先于移除处理）。
    text = re.sub(r"[A-Za-zÄÖÜäöüß][ \t]*\u00ad[ \t]*\n[ \t]*(?=[A-Za-zÄÖÜäöüß])", "", text)
    text = _SOFT_HYPHEN_RE.sub("", text)

    # 1) 全大写复合词（标题中常见）：去掉连字符与换行。
    text = re.sub(
        r"\b([A-ZÄÖÜ]{2,})-[ \t]*\n[ \t]*([A-ZÄÖÜ]{2,})\b",
        r"\1\2",
        text,
    )
    # 2) 小写 + 小写：无缝拼接。
    text = re.sub(
        r"([A-Za-zÄÖÜäöüß])-[ \t]*\n[ \t]*([a-zäöüß])",
        r"\1\2",
        text,
    )
    # 3) 后半部分大写（专名/复合专名）：去换行但保留连字符。
    text = re.sub(
        r"([A-Za-zÄÖÜäöüß])-[ \t]*\n[ \t]*([A-ZÄÖÜ])",
        r"\1-\2",
        text,
    )
    # 4) 多余情况：字母-换行-字母（兜底，保留连字符）。
    text = re.sub(
        r"([A-Za-zÄÖÜäöüß])-[ \t]*\n[ \t]*([A-Za-zÄÖÜäöüß])",
        r"\1-\2",
        text,
    )
    return text


def decode_text(data: bytes) -> tuple[str, str]:
    """按 BOM → UTF-8 → CP1252/Latin-1 的顺序解码，返回 (文本, 编码名)。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # 德语文本更可能使用 CP1252（Latin-1 的可打印超集）。
    for encoding in ("cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_mojibake(text):
            continue
        return text, encoding
    return data.decode("latin-1", errors="replace"), "latin-1"


def _looks_like_mojibake(text: str) -> bool:
    control = sum(1 for ch in text if ord(ch) < 9 or 13 < ord(ch) < 32)
    return control > max(8, len(text) // 200)


_PAGE_NUMBER_RE = re.compile(
    r"^(?:[-–—]\s*)?(?:Seite|S\.|page)?\s*\d{1,4}\s*(?:\s*[-–—])?$", re.IGNORECASE
)
_SENTENCE_END_RE = re.compile(r"[.!?…:;»“”\"'’)\]]$")


def split_paragraphs(text: str) -> list[str]:
    """把章节正文拆成自然段并清洗。"""
    text = normalize_text(text)
    if not text:
        return []
    blocks = re.split(r"\n[ \t]*\n+", text)
    nonempty = [b for b in blocks if b.strip()]
    if len(nonempty) <= 1 and text.count("\n") >= 3:
        blocks = _reflow_hard_wrapped_lines(text)
    paragraphs: list[str] = []
    for block in blocks:
        paragraph = _clean_paragraph(block)
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _reflow_hard_wrapped_lines(text: str) -> list[str]:
    """无空行 TXT：根据句末标点 + 首字母大写判断段落边界。"""
    lines = [line.strip() for line in text.split("\n")]
    paragraphs: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            joined = " ".join(buffer).strip()
            if joined:
                paragraphs.append(joined)
            buffer.clear()

    for line in lines:
        if not line:
            flush()
            continue
        if buffer:
            previous = buffer[-1]
            starts_new = bool(_SENTENCE_END_RE.search(previous)) and line[:1].isupper()
            if starts_new and len(" ".join(buffer)) > 60:
                flush()
        buffer.append(line)
    flush()
    return paragraphs


def _clean_paragraph(block: str) -> str:
    block = repair_hyphenation(block)
    lines = []
    for line in block.split("\n"):
        line = line.strip()
        if not line:
            continue
        if _PAGE_NUMBER_RE.match(line):
            continue
        lines.append(line)
    paragraph = " ".join(lines)
    paragraph = _SPACE_RE.sub(" ", paragraph).strip()
    paragraph = re.sub(r"\s+([,.;:!?])", r"\1", paragraph)
    if len(paragraph) < 2:
        return ""
    return paragraph


# --------------------------------------------------------------------------- #
# 分章
# --------------------------------------------------------------------------- #
_ORDINALS = (
    "Erstes|Zweites|Drittes|Viertes|Fünftes|Sechstes|Siebentes|Achtes|Neuntes|Zehntes|"
    "Elftes|Zwölftes|Dreizehntes|Vierzehntes|Fünfzehntes|Sechzehntes|Siebzehntes|"
    "Achtzehntes|Neunzehntes|Zwanzigstes|Einundzwanzigstes|Zweiundzwanzigstes|"
    "Dreiundzwanzigstes|Vierundzwanzigstes|Fünfundzwanzigstes"
)
_HEADING_PATTERNS = [
    re.compile(
        r"^(?:KAPITEL|Kapitel|Kap\.|KAP\.|TEIL|Teil|BUCH|Buch|ABSCHNITT|Abschnitt|"
        r"PROLOG|Prolog|EPILOG|Epilog|NACHWORT|Nachwort|VORWORT|Vorwort)\b[^\n]{0,90}$"
    ),
    re.compile(rf"^(?:{_ORDINALS})\s+(?:Kapitel|Teil|Buch|Abschnitt)\b[^\n]{{0,70}}$"),
    re.compile(r"^[IVXLCDM]{1,8}\.\s+[A-ZÄÖÜ][^\n]{0,90}$"),
    re.compile(r"^\d{1,3}\.\s+[A-ZÄÖÜ][^\n]{0,90}$"),
]
_ALLCAPS_HEADING_RE = re.compile(r"^[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9 ,.!?\-–—:;']{3,70}$")


def _is_heading_line(line: str) -> bool:
    line = line.strip()
    if not (2 <= len(line) <= 110):
        return False
    if line.endswith((",", ";", "…", "-", "–", "—")):
        return False
    for pattern in _HEADING_PATTERNS:
        if pattern.match(line):
            return True
    if (
        _ALLCAPS_HEADING_RE.match(line)
        and re.search(r"[A-ZÄÖÜ]{2,}", line)
        and len(line.split()) >= 2
    ):
        return True
    return False


def _find_headings(text: str) -> list[tuple[int, int, str]]:
    """返回 (行首偏移, 行尾偏移, 标题文本)。"""
    headings: list[tuple[int, int, str]] = []
    offset = 0
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        start = offset + (len(line) - len(line.lstrip()))
        end = offset + len(line.rstrip())
        offset += len(line) + 1
        if not stripped or not _is_heading_line(stripped):
            continue
        previous_blank = index == 0 or not lines[index - 1].strip()
        next_blank = index + 1 >= len(lines) or not lines[index + 1].strip()
        # 编号式标题允许紧邻正文；全大写标题要求前后空行，避免误伤强调句。
        numbered = bool(
            re.match(r"^(?:\d{1,3}|[IVXLCDM]{1,8})\.", stripped)
            or re.match(r"^(?:KAPITEL|Kapitel|Kap\.|KAP\.|TEIL|Teil|BUCH|Buch)\b", stripped)
            or re.match(rf"^(?:{_ORDINALS})\s+", stripped)
        )
        if not numbered and not (previous_blank or next_blank):
            continue
        headings.append((start, end, stripped))
    return headings


def split_by_headings(text: str) -> list[ParsedChapter]:
    headings = _find_headings(text)
    if not headings:
        return []
    chapters: list[ParsedChapter] = []
    first_start = headings[0][0]
    preface = text[:first_start].strip()
    if count_words(preface) >= MIN_CHAPTER_WORDS:
        chapters.append(
            ParsedChapter(title="Vorwort", paragraphs=split_paragraphs(preface), source="headings")
        )
    for index, (start, end, title) in enumerate(headings):
        next_start = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        body = text[end:next_start].strip()
        if not body and index + 1 < len(headings):
            # 某些书目录重复列出标题；内容为空时把标题并入下一章。
            continue
        title = _clean_chapter_title(title, index, len(headings))
        chapters.append(
            ParsedChapter(title=title, paragraphs=split_paragraphs(body), source="headings")
        )
    chapters = [c for c in chapters if c.paragraphs]
    if len(chapters) == 1 and count_words(chapters[0].text) < MIN_CHAPTER_WORDS:
        return []
    return chapters


def chunk_into_sections(
    text: str, *, target_words: int = TARGET_SECTION_WORDS
) -> list[ParsedChapter]:
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return []
    total_words = sum(count_words(p) for p in paragraphs)
    if total_words <= target_words * 1.35:
        return [ParsedChapter(title="Volltext", paragraphs=paragraphs, source="single")]
    sections: list[ParsedChapter] = []
    buffer: list[str] = []
    words = 0
    for paragraph in paragraphs:
        buffer.append(paragraph)
        words += count_words(paragraph)
        if words >= target_words:
            sections.append(
                ParsedChapter(
                    title=f"Abschnitt {len(sections) + 1}",
                    paragraphs=buffer,
                    source="auto-chunks",
                )
            )
            buffer = []
            words = 0
    if buffer:
        if sections and words < MIN_CHAPTER_WORDS:
            sections[-1].paragraphs.extend(buffer)
        else:
            sections.append(
                ParsedChapter(
                    title=f"Abschnitt {len(sections) + 1}",
                    paragraphs=buffer,
                    source="auto-chunks",
                )
            )
    return sections


def _clean_chapter_title(raw: str, index: int, total: int) -> str:
    title = _SPACE_RE.sub(" ", raw).strip(" .:-\u2013\u2014")
    if not title:
        return f"Kapitel {index + 1}"
    if len(title) > 120:
        title = title[:117].rstrip() + "…"
    return title


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def parse_pdf(path: Path) -> tuple[str, str | None, list[ParsedChapter], list[str], str]:
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # pragma: no cover - 损坏文件
        raise ParserError(f"无法打开 PDF：{exc}") from exc
    try:
        page_count = doc.page_count
        if page_count == 0:
            raise ParserError("PDF 没有任何页面。")
        pages = _extract_pdf_pages(doc)
        raw_chars = sum(len(p) for p in pages)
        if raw_chars < 100 or raw_chars / max(1, page_count) < 15:
            raise ParserError(
                "这个 PDF 似乎没有可提取的文本层（可能是扫描件）。"
                "请先用 OCR 工具（如 ocrmypdf）处理后再导入。",
                code="scanned_pdf",
            )
        title, author = _pdf_metadata(doc, path)
        toc = _clean_toc(doc)
        if len(toc) >= 2:
            chapters = _chapters_from_pdf_toc(pages, toc)
            source = "pdf-toc"
        else:
            chapters = _chapters_from_pdf_text(pages)
            source = "headings" if chapters and chapters[0].source == "headings" else "auto-chunks"
        if not chapters:
            raise ParserError("PDF 文本已读取，但未能切分出任何章节。")
        return title, author, chapters, [], source
    finally:
        doc.close()


def _extract_pdf_pages(doc: "fitz.Document") -> list[str]:
    """逐页提取文本块，剔除重复页眉页脚后重排为段落。"""
    pages_blocks: list[list[tuple[float, float, str]]] = []
    header_counts: dict[str, int] = {}
    footer_counts: dict[str, int] = {}
    page_heights: list[float] = []
    for page in doc:
        height = float(page.rect.height or 1)
        page_heights.append(height)
        try:
            blocks = page.get_text("blocks")
        except Exception:
            blocks = []
        page_blocks: list[tuple[float, float, str]] = []
        for block in blocks:
            if len(block) < 5:
                continue
            if len(block) >= 7 and int(block[6]) != 0:  # 图片块
                continue
            text = str(block[4]).strip()
            if not text:
                continue
            y0, y1 = float(block[1]), float(block[3])
            if y1 < height * 0.09 and len(text) <= 120:
                key = _header_key(text)
                header_counts[key] = header_counts.get(key, 0) + 1
            elif y0 > height * 0.91 and len(text) <= 120:
                key = _header_key(text)
                footer_counts[key] = footer_counts.get(key, 0) + 1
            page_blocks.append((float(block[0]), y0, text))
        if not page_blocks:
            fallback = page.get_text("text").strip()
            if fallback:
                page_blocks = [(0.0, 0.0, fallback)]
        page_blocks.sort(key=lambda item: (round(item[1], 1), round(item[0], 1)))
        pages_blocks.append(page_blocks)

    threshold = max(4, int(len(pages_blocks) * 0.45))
    repeated_headers: set[str] = set()
    repeated_footers: set[str] = set()
    # 少于 5 页的文档不做页眉页脚剔除，避免误删章节标题。
    if len(pages_blocks) >= 5:
        repeated_headers = {
            key
            for key, count in header_counts.items()
            if count >= threshold and not _looks_like_chapter_key(key)
        }
        repeated_footers = {
            key for key, count in footer_counts.items() if count >= threshold
        }

    page_texts: list[str] = []
    for page_blocks, height in zip(pages_blocks, page_heights):
        kept: list[str] = []
        for _, y0, text in page_blocks:
            key = _header_key(text)
            if y0 < height * 0.09 and key in repeated_headers:
                continue
            if y0 > height * 0.91 and key in repeated_footers:
                continue
            if _PAGE_NUMBER_RE.match(text.strip()):
                continue
            kept.append(text)
        page_text = normalize_text("\n\n".join(kept))
        page_texts.append(repair_hyphenation(page_text))
    return page_texts


def _header_key(text: str) -> str:
    return re.sub(r"\d+", "#", _SPACE_RE.sub(" ", text.strip().lower()))[:90]


def _looks_like_chapter_key(key: str) -> bool:
    return bool(
        re.match(
            r"^(?:kapitel|chapter|kap\.|teil|buch|abschnitt|prolog|epilog|vorwort|nachwort)\b",
            key,
        )
        or re.match(r"^(?:[ivxlcdm]+|#)\.", key)
    )


def _join_pdf_pages(pages: Sequence[str]) -> str:
    """拼接 PDF 页文本，并修复跨页断词（不改变原始页数，保证书签页码映射）。"""
    if not pages:
        return ""
    result = pages[0]
    for page in pages[1:]:
        if not page:
            continue
        if re.search(r"[A-Za-zÄÖÜäöüß]-$", result) and re.match(r"^[a-zäöüß]", page):
            result = result[:-1] + page
        elif re.search(r"[A-Za-zÄÖÜäöüß]-$", result) and re.match(r"^[A-ZÄÖÜ]", page):
            result = result + page
        else:
            result = result + "\n\n" + page
    return result


def _pdf_metadata(doc: "fitz.Document", path: Path) -> tuple[str, str | None]:
    meta = doc.metadata or {}
    title = _clean_metadata(meta.get("title"))
    author = _clean_metadata(meta.get("author"))
    if not title or _looks_like_garbage_title(title):
        title = title_from_filename(path.stem)
    if author and _looks_like_garbage_author(author):
        author = None
    return title, author


def _clean_toc(doc: "fitz.Document") -> list[tuple[int, str, int]]:
    try:
        raw = doc.get_toc(simple=True) or []
    except Exception:
        return []
    entries: list[tuple[int, str, int]] = []
    for item in raw:
        if len(item) < 3:
            continue
        level, title, page = int(item[0]), str(item[1]).strip(), int(item[2])
        if title and page >= 1:
            entries.append((level, title, page))
    if not entries:
        return []
    # 通常一级即章节；若一级只有书名一条，而二级有多章，则退而使用二级。
    counts = Counter(level for level, _, _ in entries)
    if counts.get(1, 0) >= 2:
        chosen_level = 1
    else:
        multi_levels = [level for level, count in sorted(counts.items()) if count >= 2]
        chosen_level = multi_levels[0] if multi_levels else min(counts)
    return [(level, title, page) for level, title, page in entries if level == chosen_level]


def _chapters_from_pdf_toc(
    pages: list[str], toc: list[tuple[int, str, int]]
) -> list[ParsedChapter]:
    total_pages = len(pages)
    entries = [(title, max(0, page - 1)) for _, title, page in toc]
    entries = [(title, min(page, total_pages - 1)) for title, page in entries]
    entries.sort(key=lambda item: item[1])
    # 多个书签指向同一页时只保留第一个，避免重复正文。
    deduped: list[tuple[str, int]] = []
    for title, page in entries:
        if deduped and page <= deduped[-1][1]:
            continue
        deduped.append((title, page))
    entries = deduped
    chapters: list[ParsedChapter] = []
    first_page = entries[0][1] if entries else 0
    preface_text = _join_pdf_pages(pages[:first_page]).strip()
    if count_words(preface_text) >= MIN_CHAPTER_WORDS:
        chapters.append(
            ParsedChapter(
                title="Vorwort", paragraphs=split_paragraphs(preface_text), source="pdf-toc"
            )
        )
    for index, (title, start) in enumerate(entries):
        next_start = entries[index + 1][1] if index + 1 < len(entries) else total_pages
        if next_start <= start:
            next_start = min(total_pages, start + 1)
        body = _join_pdf_pages(pages[start:next_start]).strip()
        paragraphs = split_paragraphs(body)
        if not paragraphs:
            continue
        cleaned = _clean_chapter_title(title, len(chapters), len(entries))
        chapters.append(ParsedChapter(title=cleaned, paragraphs=paragraphs, source="pdf-toc"))
    return chapters


def _chapters_from_pdf_text(pages: list[str]) -> list[ParsedChapter]:
    full_text = _join_pdf_pages(pages)
    chapters = split_by_headings(full_text)
    if len(chapters) >= 2:
        return chapters
    if len(chapters) == 1 and count_words(chapters[0].text) >= MIN_CHAPTER_WORDS:
        return chapters
    chunks = chunk_into_sections(full_text)
    return chunks


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #
def parse_epub(path: Path) -> tuple[str, str | None, list[ParsedChapter], list[str], str]:
    warnings.filterwarnings("ignore", module="ebooklib")
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
    except Exception as exc:
        raise ParserError(f"无法读取 EPUB：{exc}") from exc
    title = _dc_value(book, "title") or title_from_filename(path.stem)
    author = _dc_value(book, "creator") or _dc_value(book, "author")
    author = author.strip() if author and author.strip() else None
    toc_map = _epub_toc_titles(book)
    chapters: list[ParsedChapter] = []
    spine_items = _epub_spine_items(book)
    if not spine_items:
        spine_items = [item for item in book.get_items_of_type(9)]  # ITEM_DOCUMENT == 9
    for position, item in enumerate(spine_items):
        try:
            content = item.get_content()
        except Exception:
            continue
        paragraphs, headings = _epub_document_paragraphs(content)
        if not paragraphs and not headings:
            continue
        name = _epub_item_key(item)
        doc_title = toc_map.get(name) or toc_map.get(Path(name).name)
        split_chapters = _split_epub_document(paragraphs, headings, doc_title, position)
        for chapter in split_chapters:
            if chapter.paragraphs:
                chapters.append(chapter)
    if not chapters:
        raise ParserError("EPUB 中没有找到可读的正文内容。")
    chapters = _merge_tiny_chapters(chapters)
    return title, author, chapters, [], "epub-toc"


def _dc_value(book: "epub.EpubBook", name: str) -> str | None:
    try:
        values = book.get_metadata("DC", name)
    except Exception:
        return None
    for value, _attrs in values or []:
        text = _clean_metadata(str(value))
        if text:
            return text
    return None


def _epub_toc_titles(book: "epub.EpubBook") -> dict[str, str]:
    result: dict[str, str] = {}

    def walk(items: Iterable[Any]) -> None:
        for item in items:
            if isinstance(item, tuple):
                section, children = item
                title = getattr(section, "title", None)
                href = getattr(section, "href", None)
                if title and href:
                    result[_normalize_href(href)] = str(title).strip()
                walk(children or [])
            else:
                title = getattr(item, "title", None)
                href = getattr(item, "href", None)
                if title and href:
                    result[_normalize_href(href)] = str(title).strip()

    try:
        walk(book.toc or [])
    except Exception:
        log.debug("读取 EPUB 目录失败", exc_info=True)
    return result


def _normalize_href(href: str) -> str:
    href = str(href).split("#", 1)[0].strip()
    return Path(href.replace("\\", "/")).name


def _epub_item_key(item: Any) -> str:
    try:
        return Path(str(item.get_name()).replace("\\", "/")).name
    except Exception:
        return ""


def _epub_spine_items(book: "epub.EpubBook") -> list[Any]:
    items: list[Any] = []
    try:
        for entry in book.spine:
            item_id = entry[0] if isinstance(entry, tuple) else entry
            item = book.get_item_with_id(item_id)
            if item is not None and getattr(item, "media_type", "") in {
                "application/xhtml+xml",
                "text/html",
            }:
                items.append(item)
    except Exception:
        log.debug("读取 EPUB spine 失败", exc_info=True)
    return items


_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li", "pre")


def _epub_document_paragraphs(content: bytes) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """返回 [(段落文本, 位置)] 与 [(标题文本, 位置)]。位置用于按标题切章。"""
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "nav", "svg"]):
        tag.decompose()
    body = soup.body or soup
    paragraphs: list[tuple[str, int]] = []
    headings: list[tuple[str, int]] = []
    order = 0
    for tag in body.find_all(_BLOCK_TAGS):
        if tag.find_parent(["p", "blockquote", "li", "pre"]):
            continue
        name = tag.name.lower()
        text = tag.get_text(" ", strip=True)
        text = _SPACE_RE.sub(" ", text).strip()
        if not text:
            continue
        order += 1
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            headings.append((text, order))
        else:
            paragraphs.append((text, order))
    return paragraphs, headings


def _split_epub_document(
    paragraphs: list[tuple[str, int]],
    headings: list[tuple[str, int]],
    doc_title: str | None,
    position: int,
) -> list[ParsedChapter]:
    usable_headings = [(text, order) for text, order in headings if len(text) <= 150]
    if len(usable_headings) >= 2:
        chapters: list[ParsedChapter] = []
        first_order = usable_headings[0][1]
        front = [p for p, order in paragraphs if order < first_order]
        if count_words(" ".join(front)) >= MIN_CHAPTER_WORDS:
            chapters.append(
                ParsedChapter(
                    title=doc_title or f"Kapitel {position + 1}",
                    paragraphs=front,
                    source="epub-toc",
                )
            )
        for index, (title, order) in enumerate(usable_headings):
            next_order = (
                usable_headings[index + 1][1] if index + 1 < len(usable_headings) else 10**9
            )
            body = [p for p, o in paragraphs if order < o < next_order]
            if body:
                chapters.append(
                    ParsedChapter(
                        title=_clean_chapter_title(title, index, len(usable_headings)),
                        paragraphs=body,
                        source="epub-toc",
                    )
                )
        if chapters:
            return chapters
    title = doc_title
    if not title and usable_headings:
        title = usable_headings[0][0]
    if not title:
        title = f"Kapitel {position + 1}"
    body = [p for p, _ in paragraphs]
    return [
        ParsedChapter(
            title=_clean_chapter_title(title, position, position + 1),
            paragraphs=body,
            source="epub-toc",
        )
    ]


def _merge_tiny_chapters(chapters: list[ParsedChapter]) -> list[ParsedChapter]:
    if len(chapters) <= 1:
        return chapters
    merged: list[ParsedChapter] = []
    for chapter in chapters:
        if (
            merged
            and count_words(chapter.text) < 12
            and count_words(merged[-1].text) < TARGET_SECTION_WORDS
        ):
            merged[-1].paragraphs.extend(chapter.paragraphs)
        else:
            merged.append(chapter)
    return merged


# --------------------------------------------------------------------------- #
# TXT
# --------------------------------------------------------------------------- #
def parse_txt(path: Path) -> tuple[str, str | None, list[ParsedChapter], list[str], str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ParserError(f"无法读取文本文件：{exc}") from exc
    if not data.strip():
        raise ParserError("文本文件是空的。")
    text, encoding = decode_text(data)
    text = normalize_text(text)
    text = repair_hyphenation(text)
    title, author, metadata_source = _guess_txt_metadata(text, path.stem)
    chapters = split_by_headings(text)
    source = "headings"
    if len(chapters) < 2:
        chapters = chunk_into_sections(text)
        source = chapters[0].source if chapters else "auto-chunks"
    if not chapters:
        raise ParserError("文本文件中没有可读内容。")
    return title, author, chapters, [], source


def _guess_txt_metadata(text: str, stem: str) -> tuple[str, str | None, str]:
    """从文件名与开头几行推断书名/作者。"""
    title = title_from_filename(stem)
    author: str | None = None
    source = "filename"
    lines = [line.strip() for line in text.split("\n") if line.strip()][:6]
    if lines:
        first = lines[0]
        if 2 <= len(first) <= 100 and not _SENTENCE_END_RE.search(first) and not first.startswith("Kapitel"):
            title = first
            source = "content"
            if len(lines) > 1:
                second = lines[1]
                match = re.match(
                    r"^(?:von|Von|Autor(?:in)?|Verfasser(?:in)?)\s*[:：]?\s*(.{2,80})$", second
                )
                if match:
                    author = match.group(1).strip()
                    source = "content"
                elif (
                    2 <= len(second) <= 60
                    and len(second.split()) <= 6
                    and not _SENTENCE_END_RE.search(second)
                ):
                    author = second
                    source = "content"
    return title, author, source


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
def title_from_filename(stem: str) -> str:
    title = re.sub(r"[_]+", " ", stem)
    title = re.sub(r"\.(?:pdf|txt|epub|md|text)$", "", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\s*[\(\[](?:z-lib\.?org|libgen|german edition|deutsch|scan|ocr)[\)\]]\s*",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*-\s*(?:Z-Library|LibGen|Anna.s Archive).*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s{2,}", " ", title).strip(" .-_")
    if not title:
        return "Unbenanntes Buch"
    return title[:180]


def _clean_metadata(value: Any) -> str | None:
    if value is None:
        return None
    text = _SPACE_RE.sub(" ", str(value)).strip()
    if not text or text.lower() in {"untitled", "unknown", "none", "null"}:
        return None
    return text[:200]


def _looks_like_garbage_title(title: str) -> bool:
    if len(title) > 200:
        return True
    lowered = title.lower()
    if any(word in lowered for word in ("microsoft word", "untitled", "document1", "pdf created")):
        return True
    return bool(re.search(r"\.(?:pdf|docx?|indd)$", lowered))


def _looks_like_garbage_author(author: str) -> bool:
    lowered = author.lower()
    return "administrator" in lowered or "microsoft" in lowered or "user" == lowered


def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def parse_file(path: str | Path, *, filename: str | None = None) -> ParsedBook:
    """解析文档并返回结构化结果。"""
    path = Path(path)
    if not path.is_file():
        raise ParserError("文件不存在。", code="not_found")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ParserError(
            f"不支持的文件类型“{suffix or '无扩展名'}”，请上传 PDF、TXT 或 EPUB。",
            code="unsupported_type",
        )
    if suffix == ".pdf":
        title, author, chapters, warnings_list, source = parse_pdf(path)
        file_type = "pdf"
        encoding = None
    elif suffix == ".epub":
        title, author, chapters, warnings_list, source = parse_epub(path)
        file_type = "epub"
        encoding = None
    else:
        title, author, chapters, warnings_list, source = parse_txt(path)
        file_type = "txt"
        encoding = _detect_encoding_label(path)
    chapters = [ch for ch in chapters if ch.paragraphs]
    if not chapters:
        raise ParserError("未能从文档中提取到正文段落。")
    # 过滤空段落，防止前端渲染空 <p>。
    for chapter in chapters:
        chapter.paragraphs = [p for p in chapter.paragraphs if p.strip()]
    filename_title = title_from_filename(path.stem)
    metadata_source = (
        "metadata"
        if file_type in {"pdf", "epub"} and (author or title != filename_title)
        else "filename"
    )
    if file_type == "txt" and (author or title != filename_title):
        metadata_source = "content"
    return ParsedBook(
        title=title,
        author=author,
        file_type=file_type,
        chapters=chapters,
        encoding=encoding,
        toc_source=source,
        metadata_source=metadata_source,
        filename=filename or path.name,
        warnings=warnings_list,
    )


def _detect_encoding_label(path: Path) -> str | None:
    try:
        _text, encoding = decode_text(path.read_bytes())
        return encoding
    except Exception:
        return None


_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]+(?:[-'’][A-Za-zÄÖÜäöüß]+)*")


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def split_text_into_paragraphs(text: str) -> list[str]:
    """兼容旧接口。"""
    return split_paragraphs(text)
