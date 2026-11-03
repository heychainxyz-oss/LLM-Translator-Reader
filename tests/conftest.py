"""pytest 公共配置：隔离数据库、Mock AI、测试客户端。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DATA = Path(tempfile.mkdtemp(prefix="german-reader-tests-"))
os.environ["READER_DATA_DIR"] = str(TEST_DATA)
os.environ["READER_DB_PATH"] = str(TEST_DATA / "reader_data.db")
os.environ["READER_UPLOAD_DIR"] = str(TEST_DATA / "uploads")
os.environ["READER_SKIP_BUNDLED_KEY"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)

GERMAN_TEXT = """Der Prozess
Franz Kafka

Kapitel 1 · Die Verhaftung

Jemand musste Josef K. verleumdet haben, denn ohne dass er etwas Böses getan hätte, wurde er eines Morgens verhaftet. Die Köchin der Frau Grubach brachte diesmal nicht das Frühstück. Die Wissen-
schaft von der Schuld war ihm fremd.

Die Männer in den schwarzen Röcken betrachteten ihn ruhig. Am Goethe-
Institut hatte er einst einen Vortrag über Kafka gehalten.

Kapitel 2 · Erste Untersuchung

Er ging langsam die Treppe hinunter und trat auf die Straße. Die Häuser standen dicht beieinander, und die Menschen gingen vorbei, ohne ihn zu beachten.

Im Gerichtsgebäude war es kühl und dunkel. Ein Beamter führte ihn durch lange Gänge, in denen es nach Papier und Staub roch.

Kapitel 3 · Der Dom

Wochen vergingen. Josef K. ging seiner Arbeit nach und las abends Zeitung. Manchmal vergaß er das Verfahren für Stunden, dann fiel es ihm wieder ein.
"""


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_dir():
    yield
    shutil.rmtree(TEST_DATA, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_database():
    import ai_service
    import database as db

    db.init_db()
    with db.connection() as conn:
        conn.execute("DELETE FROM chat_messages")
        conn.execute("DELETE FROM annotations")
        conn.execute("DELETE FROM chapters")
        conn.execute("DELETE FROM books")
        conn.execute("DELETE FROM settings")
    db.set_settings(db.DEFAULT_SETTINGS)
    ai_service._THINKING_UNSUPPORTED.clear()
    yield


@pytest.fixture()
def client(monkeypatch):
    """带 Mock AI 的 FastAPI 测试客户端，绝不发起真实网络请求。"""
    import ai_service
    from fastapi.testclient import TestClient

    import main

    calls = {"explain": 0, "guide": 0, "background": 0, "chat": 0}

    def fake_background(title, author, excerpt):
        calls["background"] += 1
        return {
            "book_summary": "这是一部关于荒诞司法程序的长篇小说。",
            "author_bio": "弗朗茨·卡夫卡，德语作家。",
            "cultural_context": "二十世纪初的布拉格与奥匈帝国背景。",
            "source": "mock",
            "search": {"available": False, "summaries": [], "error": "mock"},
        }

    def fake_guide(book_title, chapter_title, chapter_text, language="de"):
        calls["guide"] += 1
        return {
            "summary": f"《{chapter_title}》的情节概要与人物关系。",
            "questions": ["问题一：叙述视角是什么？", "问题二：德语细节有何作用？", "问题三：人物动机是什么？"],
            "degraded": False,
        }

    def fake_explain(*args, **kwargs):
        calls["explain"] += 1
        return f"### 📖 词义与用法\n**{args[0]}** 在此处构成被动态。\n\n- 语法：werden + Partizip II"

    def fake_explain_stream(*args, **kwargs):
        text = fake_explain(*args, **kwargs)
        yield text[:10]
        yield text[10:]

    def fake_chat(**kwargs):
        calls["chat"] += 1
        return "因为 ohnen dass 从句中使用第二虚拟式，表达非真实条件。"

    def fake_chat_stream(**kwargs):
        text = fake_chat(**kwargs)
        yield text[:8]
        yield text[8:]

    def fake_identify(*args, **kwargs):
        return {"title": None, "author": None, "confidence": "low", "reason": "mock"}

    monkeypatch.setattr(ai_service, "generate_book_background", fake_background)
    monkeypatch.setattr(ai_service, "generate_chapter_guide", fake_guide)
    monkeypatch.setattr(ai_service, "explain_selection", fake_explain)
    monkeypatch.setattr(ai_service, "explain_selection_stream", fake_explain_stream)
    monkeypatch.setattr(ai_service, "chat_followup", fake_chat)
    monkeypatch.setattr(ai_service, "chat_followup_stream", fake_chat_stream)
    monkeypatch.setattr(ai_service, "identify_book", fake_identify)

    with TestClient(main.app) as test_client:
        test_client.calls = calls  # type: ignore[attr-defined]
        yield test_client


def import_text(client, text: str = GERMAN_TEXT, name: str = "Der Prozess.txt") -> dict:
    response = client.post(
        "/api/books/import",
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()["book"]
