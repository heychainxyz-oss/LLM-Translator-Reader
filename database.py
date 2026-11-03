"""SQLite 持久化层。

* 单文件数据库，默认位于项目根目录 ``reader_data.db``；
* 每个线程/请求独立连接，WAL 模式，显式外键级联；
* 所有 SQL 集中在本模块，路由层只调用语义化函数；
* 支持通过 ``READER_DATA_DIR`` / ``READER_DB_PATH`` / ``READER_UPLOAD_DIR`` 覆盖路径（便于测试）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from prompts import DEFAULT_SELECTION_PROMPT

log = logging.getLogger("reader.db")

BASE_DIR = Path(__file__).resolve().parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


DATA_DIR = _env_path("READER_DATA_DIR", BASE_DIR)
DB_PATH = _env_path("READER_DB_PATH", DATA_DIR / "reader_data.db")
UPLOAD_DIR = _env_path("READER_UPLOAD_DIR", DATA_DIR / "uploads")

#: 设置表默认值。API Key 默认为空，首次启动会尝试从环境变量/项目内 Key 文件导入。
DEFAULT_SETTINGS: dict[str, str] = {
    "active_model": "deepseek",
    "deepseek_key": "",
    "gemini_key": "",
    "deepseek_model": "deepseek-chat",
    "gemini_model": "gemini-2.5-flash",
    "selection_prompt": DEFAULT_SELECTION_PROMPT,
    "auto_annotate": "1",
    "auto_metadata": "1",
    "auto_guide": "1",
    "thinking_enabled": "1",
    "thinking_effort": "medium",
    "ai_timeout": "120",
    "theme": "light",
    "font_size": "21",
    "reading_mode": "scroll",
    "line_width": "comfortable",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT,
    file_path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    book_summary TEXT,
    author_bio TEXT,
    cultural_context TEXT,
    current_chapter INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    chapter_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    paragraphs_json TEXT NOT NULL,
    chapter_summary TEXT,
    guiding_questions TEXT,
    is_guide_ready INTEGER DEFAULT 0,
    word_count INTEGER DEFAULT 0,
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL,
    paragraph_index INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    selected_text TEXT NOT NULL,
    context_sentence TEXT NOT NULL,
    ai_explanation TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(annotation_id) REFERENCES annotations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chapters_book_index
    ON chapters(book_id, chapter_index);
CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id);
CREATE INDEX IF NOT EXISTS idx_annotations_lookup
    ON annotations(chapter_id, paragraph_index, selected_text);
CREATE INDEX IF NOT EXISTS idx_annotations_chapter ON annotations(chapter_id);
CREATE INDEX IF NOT EXISTS idx_chat_annotation ON chat_messages(annotation_id);
CREATE INDEX IF NOT EXISTS idx_books_created ON books(created_at);
"""

_BOOK_FIELDS = {
    "title",
    "author",
    "file_path",
    "file_type",
    "book_summary",
    "author_bio",
    "cultural_context",
    "current_chapter",
}


# --------------------------------------------------------------------------- #
# 连接管理
# --------------------------------------------------------------------------- #
@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """打开一个自动提交/回滚的连接。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建库建表、补齐字段、写入默认设置（幂等）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.executescript(SCHEMA)
        # 轻量迁移：为早期数据库补齐新增列。
        _ensure_column(conn, "chapters", "word_count", "INTEGER DEFAULT 0")
    _seed_defaults()
    log.info("SQLite 就绪：%s", DB_PATH)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _seed_defaults() -> None:
    # 注意：get_settings() 会合并代码默认值，此处需读取数据库中的真实键。
    with connection() as conn:
        existing = {
            row["key"] for row in conn.execute("SELECT key FROM settings").fetchall()
        }
    missing = {k: v for k, v in DEFAULT_SETTINGS.items() if k not in existing}
    if missing:
        set_settings(missing)
    # 首次启动自动读取项目内的 Key 文件 / 环境变量，省去手动配置。
    if not get_setting("deepseek_key"):
        seeded = _read_bundled_key()
        if seeded:
            set_setting("deepseek_key", seeded)
            log.info("已从本地 Key 文件/环境变量导入 DeepSeek Key")


def _read_bundled_key() -> str:
    if os.environ.get("READER_SKIP_BUNDLED_KEY") == "1":
        return ""
    candidates = [
        BASE_DIR / "DeepSeek API Key.txt",
        BASE_DIR / "deepseek_api_key.txt",
        BASE_DIR / "deepseek.key",
    ]
    for path in candidates:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8-sig").strip()
                if value.startswith("sk-") and len(value) > 20 and "\n" not in value:
                    return value
        except Exception:  # pragma: no cover - 读取失败不阻断启动
            log.warning("读取 Key 文件失败：%s", path, exc_info=True)
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return env if env.startswith("sk-") else ""


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def get_settings() -> dict[str, str]:
    with connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    result = dict(DEFAULT_SETTINGS)
    result.update({row["key"]: row["value"] for row in rows})
    return result


def set_setting(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def set_settings(values: Mapping[str, str]) -> None:
    if not values:
        return
    with connection() as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in values.items()],
        )


# --------------------------------------------------------------------------- #
# 书籍
# --------------------------------------------------------------------------- #
def create_book_with_chapters(
    *,
    title: str,
    author: str | None,
    file_path: str,
    file_type: str,
    chapters: Sequence[Mapping[str, Any]],
    current_chapter: int = 0,
) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO books(title, author, file_path, file_type, current_chapter) "
            "VALUES(?, ?, ?, ?, ?)",
            (title, author, file_path, file_type, current_chapter),
        )
        book_id = int(cur.lastrowid)
        rows = [
            (
                book_id,
                idx,
                str(ch.get("title") or f"Kapitel {idx + 1}"),
                json.dumps(ch.get("paragraphs") or [], ensure_ascii=False),
                int(ch.get("word_count") or 0),
            )
            for idx, ch in enumerate(chapters)
        ]
        conn.executemany(
            "INSERT INTO chapters(book_id, chapter_index, title, paragraphs_json, word_count) "
            "VALUES(?, ?, ?, ?, ?)",
            rows,
        )
    return book_id


def list_books() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   COUNT(c.id) AS chapter_count,
                   COALESCE(SUM(c.word_count), 0) AS word_count,
                   COALESCE(SUM(CASE WHEN c.is_guide_ready = 1 THEN 1 ELSE 0 END), 0)
                       AS guides_ready
            FROM books b
            LEFT JOIN chapters c ON c.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            """
        ).fetchall()
    return [_book_row(row) for row in rows]


def get_book(book_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            """
            SELECT b.*,
                   (SELECT COUNT(*) FROM chapters WHERE book_id = b.id) AS chapter_count,
                   (SELECT COALESCE(SUM(word_count), 0) FROM chapters WHERE book_id = b.id)
                       AS word_count
            FROM books b WHERE b.id = ?
            """,
            (book_id,),
        ).fetchone()
    return _book_row(row) if row else None


def update_book(book_id: int, **fields: Any) -> None:
    payload = {k: v for k, v in fields.items() if k in _BOOK_FIELDS}
    if not payload:
        return
    assignments = ", ".join(f"{key} = ?" for key in payload)
    with connection() as conn:
        conn.execute(
            f"UPDATE books SET {assignments} WHERE id = ?",  # noqa: S608 - 字段白名单
            (*payload.values(), book_id),
        )


def set_progress(book_id: int, chapter_index: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE books SET current_chapter = ? WHERE id = ?",
            (max(0, int(chapter_index)), book_id),
        )


def delete_book(book_id: int) -> dict[str, Any] | None:
    """删除书籍（级联章节/标注/对话），返回被删除记录以便清理文件。"""
    book = get_book(book_id)
    if not book:
        return None
    with connection() as conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return book


def library_stats() -> dict[str, int]:
    with connection() as conn:
        books = conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
        words = conn.execute("SELECT COALESCE(SUM(word_count), 0) AS n FROM chapters").fetchone()[
            "n"
        ]
        annotations = conn.execute("SELECT COUNT(*) AS n FROM annotations").fetchone()["n"]
        chats = conn.execute("SELECT COUNT(*) AS n FROM chat_messages").fetchone()["n"]
    return {
        "books": int(books),
        "words": int(words),
        "annotations": int(annotations),
        "chat_messages": int(chats),
    }


# --------------------------------------------------------------------------- #
# 章节
# --------------------------------------------------------------------------- #
def list_chapters(book_id: int, *, with_paragraphs: bool = False) -> list[dict[str, Any]]:
    columns = "*" if with_paragraphs else (
        "id, book_id, chapter_index, title, chapter_summary, guiding_questions, "
        "is_guide_ready, word_count"
    )
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM chapters WHERE book_id = ? ORDER BY chapter_index",
            (book_id,),
        ).fetchall()
    return [_chapter_row(row, decode_paragraphs=with_paragraphs) for row in rows]


def get_chapter(book_id: int, chapter_index: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM chapters WHERE book_id = ? AND chapter_index = ?",
            (book_id, int(chapter_index)),
        ).fetchone()
    return _chapter_row(row, decode_paragraphs=True) if row else None


def get_chapter_by_id(chapter_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    return _chapter_row(row, decode_paragraphs=True) if row else None


def update_chapter_guide(
    chapter_id: int, summary: str, questions: Sequence[str], *, ready: bool = True
) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE chapters SET chapter_summary = ?, guiding_questions = ?, "
            "is_guide_ready = ? WHERE id = ?",
            (
                summary,
                json.dumps(list(questions), ensure_ascii=False),
                int(bool(ready)),
                chapter_id,
            ),
        )


def reset_chapter_guide(chapter_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE chapters SET chapter_summary = NULL, guiding_questions = NULL, "
            "is_guide_ready = 0 WHERE id = ?",
            (chapter_id,),
        )


# --------------------------------------------------------------------------- #
# 标注（划词解释）
# --------------------------------------------------------------------------- #
def find_annotation(chapter_id: int, paragraph_index: int, selected_text: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM annotations WHERE chapter_id = ? AND paragraph_index = ? "
            "AND selected_text = ? ORDER BY id DESC LIMIT 1",
            (chapter_id, int(paragraph_index), selected_text),
        ).fetchone()
    return dict(row) if row else None


def get_annotation(annotation_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    return dict(row) if row else None


def create_annotation(
    *,
    chapter_id: int,
    paragraph_index: int,
    start_offset: int,
    end_offset: int,
    selected_text: str,
    context_sentence: str,
    ai_explanation: str,
) -> dict[str, Any]:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO annotations(chapter_id, paragraph_index, start_offset, end_offset, "
            "selected_text, context_sentence, ai_explanation) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                chapter_id,
                int(paragraph_index),
                int(start_offset),
                int(end_offset),
                selected_text,
                context_sentence,
                ai_explanation,
            ),
        )
        annotation_id = int(cur.lastrowid)
    result = get_annotation(annotation_id)
    assert result is not None
    return result


def update_annotation(
    annotation_id: int,
    *,
    ai_explanation: str,
    start_offset: int | None = None,
    end_offset: int | None = None,
    context_sentence: str | None = None,
) -> None:
    fields = ["ai_explanation = ?", "created_at = CURRENT_TIMESTAMP"]
    values: list[Any] = [ai_explanation]
    if start_offset is not None:
        fields.append("start_offset = ?")
        values.append(int(start_offset))
    if end_offset is not None:
        fields.append("end_offset = ?")
        values.append(int(end_offset))
    if context_sentence is not None:
        fields.append("context_sentence = ?")
        values.append(context_sentence)
    values.append(annotation_id)
    with connection() as conn:
        conn.execute(f"UPDATE annotations SET {', '.join(fields)} WHERE id = ?", values)


def delete_annotation(annotation_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    return cur.rowcount > 0


def list_annotations(chapter_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE chapter_id = ? ORDER BY paragraph_index, start_offset",
            (chapter_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_annotations_with_messages(chapter_id: int) -> list[dict[str, Any]]:
    annotations = list_annotations(chapter_id)
    if not annotations:
        return []
    messages = list_chat_messages_bulk([a["id"] for a in annotations])
    for ann in annotations:
        ann["messages"] = messages.get(ann["id"], [])
    return annotations


# --------------------------------------------------------------------------- #
# 追问消息
# --------------------------------------------------------------------------- #
def add_chat_message(annotation_id: int, role: str, content: str) -> dict[str, Any]:
    if role not in {"user", "assistant"}:
        raise ValueError("role must be user or assistant")
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages(annotation_id, role, content) VALUES(?, ?, ?)",
            (annotation_id, role, content),
        )
        message_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row)


def list_chat_messages(annotation_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE annotation_id = ? ORDER BY id",
            (annotation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_chat_messages_bulk(annotation_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = list(dict.fromkeys(int(i) for i in annotation_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM chat_messages WHERE annotation_id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
    result: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for row in rows:
        result.setdefault(row["annotation_id"], []).append(dict(row))
    return result


def clear_chat_messages(annotation_id: int) -> int:
    with connection() as conn:
        cur = conn.execute("DELETE FROM chat_messages WHERE annotation_id = ?", (annotation_id,))
    return cur.rowcount


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #
def search_book(book_id: int, query: str, limit: int = 40) -> list[dict[str, Any]]:
    """在全书段落中做大小写不敏感的子串搜索，返回可跳转的定位结果。"""
    query = query.strip()
    if len(query) < 2:
        return []
    needle = query.casefold()
    results: list[dict[str, Any]] = []
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, chapter_index, title, paragraphs_json FROM chapters "
            "WHERE book_id = ? ORDER BY chapter_index",
            (book_id,),
        ).fetchall()
    for row in rows:
        try:
            paragraphs = json.loads(row["paragraphs_json"])
        except (TypeError, ValueError):
            continue
        for p_idx, paragraph in enumerate(paragraphs):
            text = str(paragraph)
            idx = text.casefold().find(needle)
            if idx < 0:
                continue
            start = max(0, idx - 55)
            end = min(len(text), idx + len(query) + 75)
            snippet = text[start:end]
            results.append(
                {
                    "chapter_id": row["id"],
                    "chapter_index": row["chapter_index"],
                    "chapter_title": row["title"],
                    "paragraph_index": p_idx,
                    "offset": idx,
                    "snippet": ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else ""),
                }
            )
            if len(results) >= limit:
                return results
    return results


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _book_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["has_background"] = bool(data.get("book_summary"))
    return data


def _chapter_row(row: sqlite3.Row, *, decode_paragraphs: bool = False) -> dict[str, Any]:
    data = dict(row)
    if decode_paragraphs:
        try:
            paragraphs = json.loads(data.get("paragraphs_json") or "[]")
        except (TypeError, ValueError):
            paragraphs = []
        data["paragraphs"] = [str(p) for p in paragraphs] if isinstance(paragraphs, list) else []
        data["paragraph_count"] = len(data["paragraphs"])
        data["paragraphs_json"] = None
    questions_raw = data.get("guiding_questions")
    if isinstance(questions_raw, str):
        try:
            parsed = json.loads(questions_raw)
            data["questions"] = [str(q) for q in parsed] if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            data["questions"] = []
    else:
        data["questions"] = list(questions_raw or [])
    data["guiding_questions"] = data["questions"]
    data["is_guide_ready"] = bool(data.get("is_guide_ready"))
    return data


_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]+(?:[-'’][A-Za-zÄÖÜäöüß]+)*")


def count_words(text: str) -> int:
    """德语词数统计（保留连字符复合词为一个词）。"""
    return len(_WORD_RE.findall(text))


def healthcheck() -> dict[str, Any]:
    with connection() as conn:
        conn.execute("SELECT 1").fetchone()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    return {
        "db_path": str(DB_PATH),
        "tables": sorted(tables),
        "upload_dir": str(UPLOAD_DIR),
    }
