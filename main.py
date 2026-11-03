"""德语智读 · German AI Reader —— FastAPI 入口。

启动：``python main.py`` → http://127.0.0.1:8000
局域网：同一 Wi-Fi 的平板/手机访问 ``http://<你的局域网 IP>:8000``
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import ai_service
import database as db
import parser
import prompts

APP_NAME = "德语智读 · German AI Reader"
APP_VERSION = "1.2.2"
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HOST = os.environ.get("READER_HOST", "0.0.0.0")
PORT = int(os.environ.get("READER_PORT", os.environ.get("PORT", "8000")))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_SELECTED_CHARS = 500
MAX_CONTEXT_CHARS = 4000


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def _configure_logging() -> None:
    if logging.getLogger("reader").handlers:
        return
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    try:
        file_handler = RotatingFileHandler(
            db.DATA_DIR / "reader.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logging.getLogger("reader").addHandler(file_handler)
    except OSError:
        pass
    logging.getLogger("reader").addHandler(console)
    logging.getLogger("reader").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("reader.app")


# --------------------------------------------------------------------------- #
# 后台任务登记（仅内存，用于前端展示“生成中”状态）
# --------------------------------------------------------------------------- #
class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, key: str, kind: str, label: str = "") -> bool:
        with self._lock:
            current = self._jobs.get(key)
            if current and current.get("status") == "running":
                return False
            self._jobs[key] = {
                "key": key,
                "kind": kind,
                "label": label,
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "error": None,
                "result": None,
            }
            return True

    def finish(self, key: str, result: Any = None) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if not job:
                return
            job.update(
                status="done",
                finished_at=time.time(),
                result=result,
                error=None,
            )

    def fail(self, key: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if not job:
                return
            job.update(status="error", finished_at=time.time(), error=error)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(key)
            return dict(job) if job else None

    def is_running(self, key: str) -> bool:
        job = self.get(key)
        return bool(job and job.get("status") == "running")

    def clear_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [k for k in self._jobs if k.startswith(prefix)]:
                self._jobs.pop(key, None)

    def remove(self, key: str) -> None:
        with self._lock:
            self._jobs.pop(key, None)

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {
                "background": sum(
                    1
                    for job in self._jobs.values()
                    if job.get("kind") == "background" and job.get("status") == "running"
                ),
                "guides": sum(
                    1
                    for job in self._jobs.values()
                    if job.get("kind") == "guide" and job.get("status") == "running"
                ),
            }


jobs = JobRegistry()

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _named_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _job_key_book(book_id: int) -> str:
    return f"book:{book_id}:background"


def _job_key_guide(chapter_id: int) -> str:
    return f"guide:{chapter_id}"


# --------------------------------------------------------------------------- #
# 后台生成逻辑
# --------------------------------------------------------------------------- #
def _book_excerpt(book_id: int, limit: int = 2600) -> str:
    chapter = db.get_chapter(book_id, 0)
    if not chapter:
        return ""
    parts: list[str] = []
    size = 0
    for paragraph in chapter["paragraphs"]:
        parts.append(paragraph)
        size += len(paragraph)
        if size >= limit:
            break
    return "\n\n".join(parts)[:limit]


def _maybe_identify_book(book_id: int) -> None:
    """文件名式书名 + 无作者时，让 AI 识别更准确的书名/作者。"""
    if (db.get_setting("auto_metadata", "1") or "1") != "1":
        return
    book = db.get_book(book_id)
    if not book or book.get("author"):
        return
    filename_title = parser.title_from_filename(Path(book["file_path"]).stem)
    if book["title"] != filename_title:
        return
    excerpt = _book_excerpt(book_id, 1600)
    if len(excerpt) < 80:
        return
    try:
        identified = ai_service.identify_book(Path(book["file_path"]).name, excerpt)
    except ai_service.AIServiceError as exc:
        log.info("书籍识别跳过：%s", exc.message)
        return
    except Exception:
        log.warning("书籍识别异常", exc_info=True)
        return
    confidence = identified.get("confidence") or "low"
    title = identified.get("title")
    author = identified.get("author")
    updates: dict[str, Any] = {}
    if title and confidence != "low" and title.strip() and title.strip() != book["title"]:
        updates["title"] = title.strip()
    if author and not book.get("author"):
        updates["author"] = author.strip()
    if updates:
        db.update_book(book_id, **updates)
        log.info("已根据正文更新书籍元数据：%s", updates)


def _generate_book_background_core(book_id: int) -> dict[str, Any]:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    _maybe_identify_book(book_id)
    book = db.get_book(book_id) or book
    excerpt = _book_excerpt(book_id)
    if not excerpt:
        raise HTTPException(status_code=400, detail="这本书没有可用的正文，无法生成背景。")
    data = ai_service.generate_book_background(book["title"], book.get("author"), excerpt)
    if not data.get("book_summary"):
        raise ai_service.AIServiceError("AI 没有生成有效的全书概要，请稍后重试。")
    db.update_book(
        book_id,
        book_summary=data.get("book_summary") or "",
        author_bio=data.get("author_bio") or "",
        cultural_context=data.get("cultural_context") or "",
    )
    return data


def _run_book_background(book_id: int, *, raise_errors: bool = False) -> None:
    key = _job_key_book(book_id)
    if not jobs.start(key, "background", "全书背景"):
        return
    try:
        with _named_lock(key):
            data = _generate_book_background_core(book_id)
        jobs.finish(
            key,
            {
                "source": data.get("source"),
                "search_available": bool((data.get("search") or {}).get("available")),
            },
        )
    except ai_service.AIServiceError as exc:
        jobs.fail(key, exc.message)
        if raise_errors:
            raise
    except HTTPException as exc:
        jobs.fail(key, str(exc.detail))
        if raise_errors:
            raise
    except Exception as exc:  # pragma: no cover - 防御性
        log.exception("生成全书背景失败")
        jobs.fail(key, "生成全书背景失败，请稍后重试。")
        if raise_errors:
            raise ai_service.AIServiceError("生成全书背景失败，请稍后重试。") from exc


def _generate_guide_core(chapter_id: int, *, force: bool = False) -> dict[str, Any]:
    chapter = db.get_chapter_by_id(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在。")
    if chapter["is_guide_ready"] and not force:
        return {
            "summary": chapter.get("chapter_summary") or "",
            "questions": chapter.get("questions") or [],
            "cached": True,
        }
    book = db.get_book(chapter["book_id"])
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在。")
    text = "\n\n".join(chapter["paragraphs"])
    if not text.strip():
        raise HTTPException(status_code=400, detail="本章没有正文，无法生成导读。")
    guide = ai_service.generate_chapter_guide(book["title"], chapter["title"], text)
    db.update_chapter_guide(chapter_id, guide["summary"], guide["questions"])
    return {
        "summary": guide["summary"],
        "questions": guide["questions"],
        "degraded": guide.get("degraded", False),
        "cached": False,
    }


def _run_guide(chapter_id: int) -> None:
    key = _job_key_guide(chapter_id)
    if not jobs.start(key, "guide", "章节导读"):
        return
    try:
        with _named_lock(key):
            data = _generate_guide_core(chapter_id)
        jobs.finish(key, {"ready": True, "degraded": data.get("degraded", False)})
    except ai_service.AIServiceError as exc:
        jobs.fail(key, exc.message)
    except HTTPException as exc:
        jobs.fail(key, str(exc.detail))
    except Exception:  # pragma: no cover
        log.exception("生成章节导读失败")
        jobs.fail(key, "生成章节导读失败，请稍后重试。")


def _post_import(book_id: int) -> None:
    """导入完成后并行生成全书背景与前两章导读。"""
    log.info("导入后处理开始：book_id=%s", book_id)
    auto_guide = (db.get_setting("auto_guide", "1") or "1") == "1"

    def guides() -> None:
        try:
            chapters = db.list_chapters(book_id)[:2]
        except Exception:
            log.exception("读取章节失败 book_id=%s", book_id)
            return
        for chapter in chapters:
            if not chapter["is_guide_ready"]:
                _run_guide(chapter["id"])

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="import-ai") as pool:
        futures = [pool.submit(_run_book_background, book_id)]
        if auto_guide:
            futures.append(pool.submit(guides))
        for future in futures:
            try:
                future.result()
            except Exception:
                log.exception("导入后处理任务异常 book_id=%s", book_id)
    log.info("导入后处理结束：book_id=%s", book_id)


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    log.info("%s v%s 已启动", APP_NAME, APP_VERSION)
    _print_startup_banner()
    yield
    log.info("服务关闭")


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=(
        "本地优先的德语 AI 阅读器：PDF/TXT/EPUB 解析、章节导读、划词精讲与多轮追问。\n\n"
        "所有书籍、解析、笔记与 API Key 均保存在本机 `reader_data.db`，不上传服务器。"
    ),
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class AnnotateRequest(BaseModel):
    chapter_id: int = Field(gt=0)
    paragraph_index: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    selected_text: str = Field(min_length=1, max_length=MAX_SELECTED_CHARS)
    context_sentence: str = Field(default="", max_length=MAX_CONTEXT_CHARS)
    force_regenerate: bool = False


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ProgressRequest(BaseModel):
    chapter_index: int = Field(ge=0)


class BookUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    author: str | None = Field(default=None, max_length=120)


class SettingsUpdate(BaseModel):
    active_model: Literal["deepseek", "gemini"] | None = None
    deepseek_key: str | None = Field(default=None, max_length=300)
    gemini_key: str | None = Field(default=None, max_length=300)
    deepseek_model: str | None = Field(default=None, max_length=80)
    gemini_model: str | None = Field(default=None, max_length=80)
    selection_prompt: str | None = Field(default=None, max_length=20000)
    reset_prompt: bool = False
    auto_annotate: bool | None = None
    auto_metadata: bool | None = None
    auto_guide: bool | None = None
    thinking_enabled: bool | None = None
    thinking_effort: Literal["low", "medium", "high", "max"] | None = None
    theme: Literal["light", "dark"] | None = None
    font_size: int | None = Field(default=None, ge=14, le=32)
    reading_mode: Literal["scroll", "paginated"] | None = None
    line_width: Literal["narrow", "comfortable", "wide"] | None = None


class TestConnectionRequest(BaseModel):
    provider: Literal["deepseek", "gemini"] | None = None


# --------------------------------------------------------------------------- #
# 异常处理
# --------------------------------------------------------------------------- #
@app.exception_handler(ai_service.AIServiceError)
async def _ai_error_handler(_request, exc: ai_service.AIServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(parser.ParserError)
async def _parser_error_handler(_request, exc: parser.ParserError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": exc.message, "code": exc.code})


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_request, exc: RequestValidationError) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    location = " → ".join(str(part) for part in first.get("loc", []) if part != "body")
    message = first.get("msg", "参数不合法")
    detail = f"请求参数不合法：{location + '：' if location else ''}{message}"
    return JSONResponse(status_code=422, content={"detail": detail, "code": "validation_error"})


@app.exception_handler(sqlite3.Error)
async def _db_error_handler(_request, exc: sqlite3.Error) -> JSONResponse:
    log.exception("数据库错误")
    return JSONResponse(
        status_code=500,
        content={"detail": f"数据库操作失败：{exc}", "code": "db_error"},
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(_request, exc: Exception) -> JSONResponse:
    log.exception("未处理的异常")
    detail = "服务器内部错误，请查看 reader.log 获取详情。"
    if os.environ.get("READER_DEBUG") == "1":
        detail = f"{type(exc).__name__}: {exc}"
    return JSONResponse(status_code=500, content={"detail": detail, "code": "internal_error"})


# --------------------------------------------------------------------------- #
# 基础 / 系统
# --------------------------------------------------------------------------- #
def _lan_urls() -> list[str]:
    ips: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(str(info[4][0]))
    except OSError:
        pass
    result = []
    for ip in sorted(ips):
        if ip.startswith("127.") or ip.startswith("169.254.") or ip == "0.0.0.0":
            continue
        result.append(f"http://{ip}:{PORT}")
    return result


def _public_settings() -> dict[str, Any]:
    raw = db.get_settings()
    status = ai_service.provider_status()
    deepseek_key = (raw.get("deepseek_key") or "").strip() or os.environ.get(
        "DEEPSEEK_API_KEY", ""
    )
    gemini_key = (raw.get("gemini_key") or "").strip() or os.environ.get("GEMINI_API_KEY", "")
    return {
        "active_model": status["active_model"],
        "deepseek_model": raw.get("deepseek_model", "deepseek-chat"),
        "gemini_model": raw.get("gemini_model", "gemini-2.5-flash"),
        "has_deepseek_key": status["has_deepseek_key"],
        "has_gemini_key": status["has_gemini_key"],
        "deepseek_key_hint": _mask_key(deepseek_key),
        "gemini_key_hint": _mask_key(gemini_key),
        "selection_prompt": raw.get("selection_prompt") or prompts.DEFAULT_SELECTION_PROMPT,
        "default_selection_prompt": prompts.DEFAULT_SELECTION_PROMPT,
        "auto_annotate": raw.get("auto_annotate", "1") == "1",
        "auto_metadata": raw.get("auto_metadata", "1") == "1",
        "auto_guide": raw.get("auto_guide", "1") == "1",
        "thinking_enabled": raw.get("thinking_enabled", "1") == "1",
        "thinking_effort": raw.get("thinking_effort", "medium")
        if raw.get("thinking_effort") in {"low", "medium", "high", "max"}
        else "medium",
        "theme": raw.get("theme", "light"),
        "font_size": int(raw.get("font_size", "21") or 21),
        "reading_mode": raw.get("reading_mode", "scroll"),
        "line_width": raw.get("line_width", "comfortable"),
    }


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return "…" + key[-2:]
    return f"{key[:5]}…{key[-4:]}"


def _print_startup_banner() -> None:
    urls = _lan_urls()
    lines = [
        "",
        "=" * 68,
        f"  {APP_NAME}  v{APP_VERSION}",
        f"  本机访问 : http://127.0.0.1:{PORT}",
    ]
    if urls:
        lines.append(f"  手机访问 : {urls[0]}  (同一 Wi-Fi)")
    lines += [
        f"  数据目录 : {db.DATA_DIR}",
        f"  API 文档 : http://127.0.0.1:{PORT}/api/docs",
        "=" * 68,
        "",
    ]
    try:
        print("\n".join(lines))
    except UnicodeEncodeError:  # pragma: no cover - 极端控制台编码
        print("\n".join(line.encode("ascii", "ignore").decode() for line in lines))


@app.get("/api/health", tags=["系统"], summary="健康检查")
def health() -> dict[str, Any]:
    try:
        db_info = db.healthcheck()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"数据库不可用：{exc}") from exc
    return {
        "status": "ok",
        "version": APP_VERSION,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db": db_info,
        "stats": db.library_stats(),
        "ai": ai_service.provider_status(),
        "jobs": jobs.summary(),
    }


@app.get("/api/system/info", tags=["系统"], summary="运行环境与局域网地址")
def system_info() -> dict[str, Any]:
    urls = _lan_urls()
    try:
        pdf_engine = f"PyMuPDF {parser.fitz.__version__}" if getattr(parser, "fitz", None) else "PyMuPDF"
    except Exception:  # pragma: no cover - 防御性
        pdf_engine = "PyMuPDF"
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "host": HOST,
        "port": PORT,
        "lan_urls": urls,
        "primary_url": urls[0] if urls else f"http://127.0.0.1:{PORT}",
        "local_url": f"http://127.0.0.1:{PORT}",
        "data_dir": str(db.DATA_DIR),
        "database": str(db.DB_PATH),
        "uploads": str(db.UPLOAD_DIR),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "pdf_engine": pdf_engine,
        "stats": db.library_stats(),
        "ai": ai_service.provider_status(),
    }


@app.get("/api/system/qr", tags=["系统"], summary="生成局域网访问二维码（SVG）")
def system_qr(url: str = Query(default="", max_length=300)) -> Response:
    target = url.strip()
    if not target:
        urls = _lan_urls()
        target = urls[0] if urls else f"http://127.0.0.1:{PORT}"
    if not re.match(r"^https?://[A-Za-z0-9.\-:]+(?:/[^\s]*)?$", target):
        raise HTTPException(status_code=400, detail="二维码内容必须是合法的 http(s) 地址。")
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=503, detail="未安装 qrcode 依赖。")
    image = qrcode.make(
        target,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
@app.get("/api/settings", tags=["设置"], summary="读取设置（API Key 已脱敏）")
def get_settings() -> dict[str, Any]:
    return _public_settings()


@app.put("/api/settings", tags=["设置"], summary="保存设置")
def update_settings(payload: SettingsUpdate) -> dict[str, Any]:
    updates: dict[str, str] = {}
    data = payload.model_dump(exclude_unset=True)
    for key in ("active_model", "deepseek_model", "gemini_model", "theme", "reading_mode", "line_width"):
        value = data.get(key)
        if value is not None:
            updates[key] = str(value)
    for key in ("auto_annotate", "auto_metadata", "auto_guide", "thinking_enabled"):
        value = data.get(key)
        if value is not None:
            updates[key] = "1" if value else "0"
    if data.get("thinking_effort") is not None:
        updates["thinking_effort"] = data["thinking_effort"]
    if data.get("font_size") is not None:
        updates["font_size"] = str(int(data["font_size"]))
    if data.get("selection_prompt") is not None:
        prompt = data["selection_prompt"].strip()
        if len(prompt) < 20:
            raise HTTPException(status_code=400, detail="自定义 Prompt 太短，请至少写 20 个字符。")
        updates["selection_prompt"] = prompt
    if data.get("reset_prompt"):
        updates["selection_prompt"] = prompts.DEFAULT_SELECTION_PROMPT
    for key in ("deepseek_key", "gemini_key"):
        value = data.get(key)
        if value is None:
            continue
        value = value.strip()
        if "…" in value or "..." in value or set(value) == {"•"}:
            continue  # 前端把脱敏提示误传回来时忽略
        if value:
            updates[key] = value
        else:
            updates[key] = ""
    if updates:
        db.set_settings(updates)
        log.info("设置已更新：%s", ", ".join(sorted(updates)))
    return _public_settings()


@app.post("/api/settings/test", tags=["设置"], summary="测试 AI 连接")
def test_connection(payload: TestConnectionRequest | None = None) -> dict[str, Any]:
    provider = payload.provider if payload else None
    try:
        result = ai_service.test_connection(provider)
    except ai_service.AIServiceError as exc:
        return {
            "ok": False,
            "provider": provider or ai_service.provider_status()["active_model"],
            "error": exc.message,
            "code": exc.code,
        }
    return result


@app.post("/api/settings/reset-prompt", tags=["设置"], summary="恢复默认划词 Prompt")
def reset_prompt() -> dict[str, Any]:
    db.set_setting("selection_prompt", prompts.DEFAULT_SELECTION_PROMPT)
    return {"ok": True, "selection_prompt": prompts.DEFAULT_SELECTION_PROMPT}


# --------------------------------------------------------------------------- #
# 书籍
# --------------------------------------------------------------------------- #
@app.get("/api/books", tags=["书籍"], summary="书库列表")
def list_books() -> dict[str, Any]:
    books = db.list_books()
    for book in books:
        book["background_job"] = jobs.get(_job_key_book(book["id"]))
    return {"books": books, "stats": db.library_stats()}


@app.post("/api/books/import", tags=["书籍"], summary="导入 PDF / TXT / EPUB")
async def import_book(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF、TXT 或 EPUB 文件"),
    title: str | None = Form(default=None),
    author: str | None = Form(default=None),
) -> dict[str, Any]:
    original_name = Path(file.filename or "book").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in parser.SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持“{suffix or '无扩展名'}”文件，请上传 PDF、TXT 或 EPUB。",
        )
    db.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^\w\-. ]+", "_", Path(original_name).stem, flags=re.UNICODE)[:80].strip()
    stored_name = f"{safe_stem or 'book'}-{uuid.uuid4().hex[:8]}{suffix}"
    destination = db.UPLOAD_DIR / stored_name
    size = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件超过 200 MB 限制。")
                handle.write(chunk)
        await file.close()
        if size == 0:
            raise HTTPException(status_code=400, detail="上传的文件是空的。")

        try:
            parsed = await run_in_threadpool(parser.parse_file, destination, filename=original_name)
        except parser.ParserError:
            raise
        except Exception as exc:
            log.exception("解析失败：%s", original_name)
            raise parser.ParserError(f"解析文件失败：{exc}") from exc

        final_title = (title or "").strip() or parsed.title
        final_author = (author or "").strip() or parsed.author
        chapters = [
            {"title": chapter.title, "paragraphs": chapter.paragraphs, "word_count": chapter.word_count}
            for chapter in parsed.chapters
        ]
        book_id = await run_in_threadpool(
            db.create_book_with_chapters,
            title=final_title,
            author=final_author,
            file_path=str(destination),
            file_type=parsed.file_type,
            chapters=chapters,
        )
        background_tasks.add_task(_post_import, book_id)
        book = db.get_book(book_id)
        assert book is not None
        book["chapters"] = db.list_chapters(book_id)
        book["background_job"] = jobs.get(_job_key_book(book_id))
        log.info(
            "导入成功：%s（%s 章，%s 词，来源 %s）",
            final_title,
            len(chapters),
            book["word_count"],
            parsed.toc_source,
        )
        return {
            "book": book,
            "parse": {
                "toc_source": parsed.toc_source,
                "encoding": parsed.encoding,
                "metadata_source": parsed.metadata_source,
                "word_count": parsed.word_count,
                "chapter_count": len(chapters),
                "warnings": parsed.warnings,
                "original_filename": original_name,
                "size_bytes": size,
            },
        }
    except Exception:
        destination.unlink(missing_ok=True)
        raise


@app.get("/api/books/{book_id}", tags=["书籍"], summary="书籍详情")
def get_book(book_id: int) -> dict[str, Any]:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    chapters = db.list_chapters(book_id)
    for chapter in chapters:
        chapter["guide_job"] = jobs.get(_job_key_guide(chapter["id"]))
    book["chapters"] = chapters
    book["background_job"] = jobs.get(_job_key_book(book_id))
    return book


@app.patch("/api/books/{book_id}", tags=["书籍"], summary="修改书名/作者")
def update_book(book_id: int, payload: BookUpdateRequest) -> dict[str, Any]:
    if not db.get_book(book_id):
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    updates = {k: v.strip() for k, v in payload.model_dump(exclude_none=True).items() if v.strip()}
    if updates:
        db.update_book(book_id, **updates)
    book = db.get_book(book_id)
    assert book is not None
    return book


@app.delete("/api/books/{book_id}", tags=["书籍"], summary="删除书籍及其全部笔记")
def delete_book(book_id: int) -> dict[str, Any]:
    chapter_rows = db.list_chapters(book_id) if db.get_book(book_id) else []
    book = db.delete_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    file_path = Path(book["file_path"])
    try:
        if file_path.is_file() and file_path.resolve().is_relative_to(db.UPLOAD_DIR.resolve()):
            file_path.unlink(missing_ok=True)
    except OSError:
        log.warning("删除原文件失败：%s", file_path, exc_info=True)
    jobs.remove(_job_key_book(book_id))
    for chapter in chapter_rows:
        jobs.remove(_job_key_guide(chapter["id"]))
    log.info("已删除书籍：%s", book["title"])
    return {"ok": True, "deleted_id": book_id}


@app.patch("/api/books/{book_id}/progress", tags=["书籍"], summary="保存阅读进度")
def save_progress(book_id: int, payload: ProgressRequest) -> dict[str, Any]:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    if payload.chapter_index >= max(1, book["chapter_count"]):
        raise HTTPException(status_code=400, detail="章节序号超出范围。")
    db.set_progress(book_id, payload.chapter_index)
    return {"ok": True, "current_chapter": payload.chapter_index}


@app.get("/api/books/{book_id}/file", tags=["书籍"], summary="下载原始文件")
def download_book_file(book_id: int) -> FileResponse:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    path = Path(book["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="原始文件已丢失。")
    if not path.resolve().is_relative_to(db.UPLOAD_DIR.resolve()):
        raise HTTPException(status_code=403, detail="不允许访问该路径。")
    suffix = path.suffix
    filename = re.sub(r"[\\/:*?\"<>|]+", "_", book["title"])[:100] or "book"
    return FileResponse(path, filename=f"{filename}{suffix}")


@app.get("/api/books/{book_id}/search", tags=["书籍"], summary="全书搜索")
def search_book(
    book_id: int,
    q: str = Query(min_length=2, max_length=100, description="搜索关键词"),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    if not db.get_book(book_id):
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    results = db.search_book(book_id, q, limit=limit)
    return {"query": q, "count": len(results), "results": results}


@app.post("/api/books/{book_id}/background", tags=["书籍"], summary="重新生成全书背景")
def regenerate_background(book_id: int) -> dict[str, Any]:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    key = _job_key_book(book_id)
    if jobs.is_running(key):
        raise HTTPException(status_code=409, detail="全书背景正在生成中，请稍候。")
    _run_book_background(book_id, raise_errors=True)
    refreshed = db.get_book(book_id)
    assert refreshed is not None
    return {"book": refreshed, "job": jobs.get(key)}


# --------------------------------------------------------------------------- #
# 章节
# --------------------------------------------------------------------------- #
@app.get("/api/books/{book_id}/chapters", tags=["章节"], summary="章节目录")
def list_chapters(book_id: int) -> dict[str, Any]:
    if not db.get_book(book_id):
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    chapters = db.list_chapters(book_id)
    for chapter in chapters:
        chapter["guide_job"] = jobs.get(_job_key_guide(chapter["id"]))
    return {"chapters": chapters}


@app.get(
    "/api/books/{book_id}/chapters/{chapter_index}",
    tags=["章节"],
    summary="读取章节正文、导读、全部划词缓存",
)
def get_chapter(
    book_id: int,
    chapter_index: int,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    chapter = db.get_chapter(book_id, chapter_index)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在。")
    annotations = db.list_annotations_with_messages(chapter["id"])
    chapter["annotations"] = annotations
    chapter["book"] = {
        "id": book["id"],
        "title": book["title"],
        "author": book.get("author"),
        "chapter_count": book["chapter_count"],
        "word_count": book["word_count"],
    }
    chapter["total_chapters"] = book["chapter_count"]
    chapter["prev_chapter"] = chapter_index - 1 if chapter_index > 0 else None
    chapter["next_chapter"] = (
        chapter_index + 1 if chapter_index + 1 < book["chapter_count"] else None
    )
    chapter["guide_job"] = jobs.get(_job_key_guide(chapter["id"]))
    # 打开章节即保存进度。
    if book.get("current_chapter") != chapter_index:
        db.set_progress(book_id, chapter_index)

    auto_guide = (db.get_setting("auto_guide", "1") or "1") == "1"
    if auto_guide:
        if not chapter["is_guide_ready"] and not jobs.is_running(_job_key_guide(chapter["id"])):
            background_tasks.add_task(_run_guide, chapter["id"])
            chapter["guide_job"] = {
                "status": "running",
                "kind": "guide",
                "started_at": time.time(),
            }
        next_chapter = db.get_chapter(book_id, chapter_index + 1)
        if next_chapter and not next_chapter["is_guide_ready"]:
            if not jobs.is_running(_job_key_guide(next_chapter["id"])):
                background_tasks.add_task(_run_guide, next_chapter["id"])
    return chapter


@app.post(
    "/api/books/{book_id}/chapters/{chapter_index}/guide",
    tags=["章节"],
    summary="立即生成/重新生成本章导读",
)
def generate_guide(
    book_id: int,
    chapter_index: int,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="书籍不存在或已被删除。")
    chapter = db.get_chapter(book_id, chapter_index)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在。")
    key = _job_key_guide(chapter["id"])
    with _named_lock(key):
        fresh = db.get_chapter_by_id(chapter["id"])
        assert fresh is not None
        if fresh["is_guide_ready"] and not force:
            return {
                "summary": fresh.get("chapter_summary") or "",
                "questions": fresh.get("questions") or [],
                "cached": True,
                "chapter_id": chapter["id"],
                "is_guide_ready": True,
            }
        result = _generate_guide_core(chapter["id"], force=force)
    jobs.finish(key, {"ready": True})
    return {
        **result,
        "chapter_id": chapter["id"],
        "is_guide_ready": True,
        "job": jobs.get(key),
    }


@app.post(
    "/api/chapters/{chapter_id}/prepare_guide",
    tags=["章节"],
    summary="后台预热章节导读（设计稿中的懒加载接口）",
)
def prepare_guide(chapter_id: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
    chapter = db.get_chapter_by_id(chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在。")
    if chapter["is_guide_ready"]:
        return {"status": "ready", "chapter_id": chapter_id}
    key = _job_key_guide(chapter_id)
    if jobs.is_running(key):
        return {"status": "generating", "chapter_id": chapter_id, "job": jobs.get(key)}
    background_tasks.add_task(_run_guide, chapter_id)
    return {"status": "queued", "chapter_id": chapter_id}


# --------------------------------------------------------------------------- #
# 划词解析与追问
# --------------------------------------------------------------------------- #
def _context_window(text: str, start: int, end: int, limit: int = 1200) -> str:
    left = max(0, start - 500)
    right = min(len(text), end + 500)
    snippet = text[left:right]
    if len(snippet) <= limit:
        return snippet
    return snippet[:limit]


def _resolve_offsets(paragraph: str, start: int, end: int, selected: str) -> tuple[int, int, str]:
    """校验/校正前端选区，返回可稳定高亮的偏移量。"""
    selected = selected.strip()
    if not selected:
        raise HTTPException(status_code=400, detail="没有选中任何文字。")
    if end > start and end <= len(paragraph):
        raw = paragraph[start:end]
        if raw == selected:
            return start, end, selected
        if raw.strip() == selected:
            leading = len(raw) - len(raw.lstrip())
            return start + leading, start + leading + len(selected), selected
        if re.sub(r"\s+", " ", raw).strip() == re.sub(r"\s+", " ", selected):
            return start, end, raw.strip()
    position = paragraph.find(selected)
    if position < 0:
        # 兼容大小写/弯引号差异。
        lowered = paragraph.casefold().find(selected.casefold())
        if lowered < 0:
            raise HTTPException(
                status_code=400,
                detail="选区与原文不一致，请重新选择后再试。",
            )
        position = lowered
    return position, position + len(selected), selected


def _annotation_payload(annotation: dict[str, Any]) -> dict[str, Any]:
    annotation = dict(annotation)
    annotation["messages"] = db.list_chat_messages(annotation["id"])
    return annotation


def _prepare_annotation(
    payload: AnnotateRequest,
) -> tuple[dict[str, Any], str, int, int, str, str, dict[str, Any] | None]:
    """流式/非流式划词共用的校验与上下文准备。"""
    chapter = db.get_chapter_by_id(payload.chapter_id)
    if not chapter:
        raise HTTPException(status_code=404, detail="章节不存在。")
    paragraphs = chapter.get("paragraphs") or []
    if payload.paragraph_index >= len(paragraphs):
        raise HTTPException(status_code=400, detail="段落序号超出范围。")
    paragraph = paragraphs[payload.paragraph_index]
    start, end, selected = _resolve_offsets(
        paragraph, payload.start_offset, payload.end_offset, payload.selected_text
    )
    if len(selected) > MAX_SELECTED_CHARS:
        raise HTTPException(status_code=400, detail=f"选中内容过长（上限 {MAX_SELECTED_CHARS} 字）。")
    context = (payload.context_sentence or "").strip()
    if len(context) < 4:
        context = _context_window(paragraph, start, end)
    context = context[:MAX_CONTEXT_CHARS]
    book = db.get_book(chapter["book_id"])
    return chapter, paragraph, start, end, selected, context, book


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_response(generator: Any) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/annotate", tags=["划词"], summary="划词解析（命中缓存则零 Token 返回）")
def annotate(payload: AnnotateRequest) -> dict[str, Any]:
    chapter, paragraph, start, end, selected, context, book = _prepare_annotation(payload)

    key = f"annot:{chapter['id']}:{payload.paragraph_index}:{selected}"
    with _named_lock(key):
        existing = db.find_annotation(chapter["id"], payload.paragraph_index, selected)
        if existing and not payload.force_regenerate:
            result = _annotation_payload(existing)
            result["cached"] = True
            result["regenerated"] = False
            return {"annotation": result, "cached": True}
        explanation = ai_service.explain_selection(
            selected,
            context,
            book_title=(book or {}).get("title", ""),
            chapter_title=chapter["title"],
            prompt_template=db.get_setting("selection_prompt"),
        )
        if existing:
            db.update_annotation(
                existing["id"],
                ai_explanation=explanation,
                start_offset=start,
                end_offset=end,
                context_sentence=context,
            )
            annotation = db.get_annotation(existing["id"])
            assert annotation is not None
            cached, regenerated = False, True
        else:
            annotation = db.create_annotation(
                chapter_id=chapter["id"],
                paragraph_index=payload.paragraph_index,
                start_offset=start,
                end_offset=end,
                selected_text=selected,
                context_sentence=context,
                ai_explanation=explanation,
            )
            cached, regenerated = False, False
    result = _annotation_payload(annotation)
    result["cached"] = cached
    result["regenerated"] = regenerated
    return {"annotation": result, "cached": cached}


@app.post(
    "/api/annotate/stream",
    tags=["划词"],
    summary="流式划词解析（SSE）",
)
def annotate_stream(payload: AnnotateRequest) -> StreamingResponse:
    chapter, paragraph, start, end, selected, context, book = _prepare_annotation(payload)
    key = f"annot:{chapter['id']}:{payload.paragraph_index}:{selected}"

    def generate() -> Any:
        try:
            with _named_lock(key):
                existing = db.find_annotation(chapter["id"], payload.paragraph_index, selected)
                if existing and not payload.force_regenerate:
                    result = _annotation_payload(existing)
                    result["cached"] = True
                    result["regenerated"] = False
                    yield _sse("meta", {"cached": True, "annotation_id": existing["id"]})
                    yield _sse("delta", {"text": existing["ai_explanation"]})
                    yield _sse("done", {"annotation": result, "cached": True})
                    return

                yield _sse("meta", {"cached": False})
                chunks: list[str] = []
                for piece in ai_service.explain_selection_stream(
                    selected,
                    context,
                    book_title=(book or {}).get("title", ""),
                    chapter_title=chapter["title"],
                    prompt_template=db.get_setting("selection_prompt"),
                ):
                    if piece:
                        chunks.append(piece)
                        yield _sse("delta", {"text": piece})
                explanation = "".join(chunks).strip()
                if not explanation:
                    raise ai_service.AIServiceError(
                        "AI 没有生成有效的解析内容，请重试。", code="empty_content"
                    )
                if existing:
                    db.update_annotation(
                        existing["id"],
                        ai_explanation=explanation,
                        start_offset=start,
                        end_offset=end,
                        context_sentence=context,
                    )
                    annotation = db.get_annotation(existing["id"])
                    assert annotation is not None
                    regenerated = True
                else:
                    annotation = db.create_annotation(
                        chapter_id=chapter["id"],
                        paragraph_index=payload.paragraph_index,
                        start_offset=start,
                        end_offset=end,
                        selected_text=selected,
                        context_sentence=context,
                        ai_explanation=explanation,
                    )
                    regenerated = False
                result = _annotation_payload(annotation)
                yield _sse(
                    "done",
                    {"annotation": result, "cached": False, "regenerated": regenerated},
                )
        except ai_service.AIServiceError as exc:
            yield _sse("error", {"detail": exc.message, "code": exc.code})
        except HTTPException as exc:
            yield _sse("error", {"detail": str(exc.detail), "code": "http_error"})
        except Exception:
            log.exception("流式划词解析失败")
            yield _sse(
                "error",
                {"detail": "服务器内部错误，请稍后重试。", "code": "internal_error"},
            )

    return _sse_response(generate())


@app.get("/api/annotations/{annotation_id}", tags=["划词"], summary="读取标注与追问历史")
def get_annotation(annotation_id: int) -> dict[str, Any]:
    annotation = db.get_annotation(annotation_id)
    if not annotation:
        raise HTTPException(status_code=404, detail="标注不存在。")
    return _annotation_payload(annotation)


@app.delete("/api/annotations/{annotation_id}", tags=["划词"], summary="删除高亮与全部追问")
def delete_annotation(annotation_id: int) -> dict[str, Any]:
    if not db.delete_annotation(annotation_id):
        raise HTTPException(status_code=404, detail="标注不存在。")
    return {"ok": True, "deleted_id": annotation_id}


@app.get("/api/chapters/{chapter_id}/annotations", tags=["划词"], summary="章节全部标注")
def list_chapter_annotations(chapter_id: int) -> dict[str, Any]:
    if not db.get_chapter_by_id(chapter_id):
        raise HTTPException(status_code=404, detail="章节不存在。")
    return {"annotations": db.list_annotations_with_messages(chapter_id)}


@app.post("/api/annotations/{annotation_id}/chat", tags=["划词"], summary="就当前词句继续追问")
def annotation_chat(annotation_id: int, payload: ChatRequest) -> dict[str, Any]:
    annotation = db.get_annotation(annotation_id)
    if not annotation:
        raise HTTPException(status_code=404, detail="标注不存在。")
    question = payload.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="追问内容不能为空。")
    chapter = db.get_chapter_by_id(annotation["chapter_id"])
    book = db.get_book(chapter["book_id"]) if chapter else None
    history = db.list_chat_messages(annotation_id)[-20:]
    key = f"chat:{annotation_id}"
    with _named_lock(key):
        answer = ai_service.chat_followup(
            selected_text=annotation["selected_text"],
            context_sentence=annotation["context_sentence"],
            explanation=annotation["ai_explanation"],
            history=history,
            question=question,
            book_title=(book or {}).get("title", ""),
        )
        user_message = db.add_chat_message(annotation_id, "user", question)
        assistant_message = db.add_chat_message(annotation_id, "assistant", answer)
    return {
        "user": user_message,
        "assistant": assistant_message,
        "messages": db.list_chat_messages(annotation_id),
    }


@app.post(
    "/api/annotations/{annotation_id}/chat/stream",
    tags=["划词"],
    summary="流式追问（SSE）",
)
def annotation_chat_stream(annotation_id: int, payload: ChatRequest) -> StreamingResponse:
    annotation = db.get_annotation(annotation_id)
    if not annotation:
        raise HTTPException(status_code=404, detail="标注不存在。")
    question = payload.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="追问内容不能为空。")
    chapter = db.get_chapter_by_id(annotation["chapter_id"])
    book = db.get_book(chapter["book_id"]) if chapter else None
    key = f"chat:{annotation_id}"

    def generate() -> Any:
        try:
            with _named_lock(key):
                history = db.list_chat_messages(annotation_id)[-20:]
                yield _sse("meta", {"annotation_id": annotation_id})
                chunks: list[str] = []
                for piece in ai_service.chat_followup_stream(
                    selected_text=annotation["selected_text"],
                    context_sentence=annotation["context_sentence"],
                    explanation=annotation["ai_explanation"],
                    history=history,
                    question=question,
                    book_title=(book or {}).get("title", ""),
                ):
                    if piece:
                        chunks.append(piece)
                        yield _sse("delta", {"text": piece})
                answer = "".join(chunks).strip()
                if not answer:
                    raise ai_service.AIServiceError(
                        "AI 没有生成有效的回答，请重试。", code="empty_content"
                    )
                user_message = db.add_chat_message(annotation_id, "user", question)
                assistant_message = db.add_chat_message(annotation_id, "assistant", answer)
                yield _sse(
                    "done",
                    {
                        "user": user_message,
                        "assistant": assistant_message,
                        "messages": db.list_chat_messages(annotation_id),
                    },
                )
        except ai_service.AIServiceError as exc:
            yield _sse("error", {"detail": exc.message, "code": exc.code})
        except HTTPException as exc:
            yield _sse("error", {"detail": str(exc.detail), "code": "http_error"})
        except Exception:
            log.exception("流式追问失败 annotation_id=%s", annotation_id)
            yield _sse(
                "error",
                {"detail": "服务器内部错误，请稍后重试。", "code": "internal_error"},
            )

    return _sse_response(generate())


@app.delete("/api/annotations/{annotation_id}/chat", tags=["划词"], summary="清空追问历史")
def clear_annotation_chat(annotation_id: int) -> dict[str, Any]:
    if not db.get_annotation(annotation_id):
        raise HTTPException(status_code=404, detail="标注不存在。")
    count = db.clear_chat_messages(annotation_id)
    return {"ok": True, "cleared": count}


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=500, detail="前端文件缺失：static/index.html")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    path = STATIC_DIR / "favicon.svg"
    if path.is_file():
        return FileResponse(path, media_type="image/svg+xml")
    return Response(status_code=204)


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> Response:
    path = STATIC_DIR / "manifest.webmanifest"
    if path.is_file():
        return FileResponse(path, media_type="application/manifest+json")
    return Response(status_code=404)


def main() -> None:
    print(f"\n启动 {APP_NAME} …")
    if os.environ.get("READER_OPEN_BROWSER") == "1" and os.environ.get("READER_RELOAD") != "1":
        url = f"http://127.0.0.1:{PORT}"
        threading.Timer(2.2, lambda: webbrowser.open(url)).start()
    if os.environ.get("READER_RELOAD") == "1":
        uvicorn.run(
            "main:app",
            host=HOST,
            port=PORT,
            log_level="info",
            reload=True,
            access_log=False,
        )
    else:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
