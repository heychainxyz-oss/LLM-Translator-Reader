"""数据库层验收测试：五表、级联、设置、缓存查询、并发。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import database as db


def create_book(title: str = "Der Prozess", author: str = "Franz Kafka") -> int:
    return db.create_book_with_chapters(
        title=title,
        author=author,
        file_path=f"/tmp/{title}.txt",
        file_type="txt",
        chapters=[
            {"title": "Kapitel 1", "paragraphs": ["Jemand musste Josef K. verleumdet haben.", "Er wurde verhaftet."], "word_count": 12},
            {"title": "Kapitel 2", "paragraphs": ["Die Köchin kam nicht."], "word_count": 5},
        ],
    )


class TestSchema:
    def test_five_core_tables(self):
        info = db.healthcheck()
        assert set(info["tables"]) >= {"books", "chapters", "annotations", "chat_messages", "settings"}
        assert info["db_path"]

    def test_init_idempotent(self):
        book_id = create_book()
        db.init_db()
        db.init_db()
        assert db.get_book(book_id) is not None
        assert db.get_book(book_id)["chapter_count"] == 2

    def test_legacy_model_names_migrate(self):
        db.set_setting("deepseek_model", "deepseek-chat")
        db.set_setting("gemini_model", "gemini-2.5-flash")
        db.init_db()
        assert db.get_setting("deepseek_model") == "deepseek-flash"
        assert db.get_setting("gemini_model") == "gemini-3.8-flash"

    def test_current_model_defaults(self):
        assert db.DEFAULT_SETTINGS["deepseek_model"] == "deepseek-flash"
        assert db.DEFAULT_SETTINGS["gemini_model"] == "gemini-3.8-flash"


class TestBooksAndChapters:
    def test_crud_and_progress(self):
        book_id = create_book()
        book = db.get_book(book_id)
        assert book["title"] == "Der Prozess"
        assert book["chapter_count"] == 2
        assert book["word_count"] == 17
        db.update_book(book_id, title="Der Process", current_chapter=1)
        assert db.get_book(book_id)["title"] == "Der Process"
        assert db.get_book(book_id)["current_chapter"] == 1
        db.set_progress(book_id, 0)
        assert db.get_book(book_id)["current_chapter"] == 0

    def test_chapter_payload_and_guide(self):
        book_id = create_book()
        chapter = db.get_chapter(book_id, 0)
        assert chapter["paragraphs"] == [
            "Jemand musste Josef K. verleumdet haben.",
            "Er wurde verhaftet.",
        ]
        assert chapter["paragraph_count"] == 2
        assert not chapter["is_guide_ready"]
        db.update_chapter_guide(chapter["id"], "本章概要", ["问题1", "问题2", "问题3"])
        refreshed = db.get_chapter(book_id, 0)
        assert refreshed["is_guide_ready"] is True
        assert refreshed["chapter_summary"] == "本章概要"
        assert refreshed["questions"] == ["问题1", "问题2", "问题3"]
        db.reset_chapter_guide(chapter["id"])
        assert db.get_chapter(book_id, 0)["is_guide_ready"] is False

    def test_list_books_aggregates(self):
        create_book("Buch A")
        create_book("Buch B")
        books = db.list_books()
        assert len(books) == 2
        assert all(book["chapter_count"] == 2 for book in books)
        assert all(book["word_count"] == 17 for book in books)


class TestAnnotations:
    def setup_book(self):
        book_id = create_book()
        chapter = db.get_chapter(book_id, 0)
        return book_id, chapter

    def test_cache_lookup_and_messages(self):
        _book_id, chapter = self.setup_book()
        annotation = db.create_annotation(
            chapter_id=chapter["id"],
            paragraph_index=0,
            start_offset=0,
            end_offset=6,
            selected_text="Jemand",
            context_sentence="Jemand musste Josef K. verleumdet haben.",
            ai_explanation="### 解析\nJemand 是不定代词。",
        )
        found = db.find_annotation(chapter["id"], 0, "Jemand")
        assert found is not None and found["id"] == annotation["id"]
        assert db.find_annotation(chapter["id"], 0, "Niemand") is None
        db.add_chat_message(annotation["id"], "user", "为什么用 Jemand？")
        db.add_chat_message(annotation["id"], "assistant", "因为主语不确定。")
        messages = db.list_chat_messages(annotation["id"])
        assert [m["role"] for m in messages] == ["user", "assistant"]
        loaded = db.list_annotations_with_messages(chapter["id"])
        assert len(loaded) == 1
        assert len(loaded[0]["messages"]) == 2
        assert db.clear_chat_messages(annotation["id"]) == 2
        assert db.list_chat_messages(annotation["id"]) == []

    def test_update_annotation(self):
        _book_id, chapter = self.setup_book()
        annotation = db.create_annotation(
            chapter_id=chapter["id"],
            paragraph_index=1,
            start_offset=3,
            end_offset=12,
            selected_text="verhaftet",
            context_sentence="Er wurde verhaftet.",
            ai_explanation="alt",
        )
        db.update_annotation(annotation["id"], ai_explanation="neu", context_sentence="neu context")
        refreshed = db.get_annotation(annotation["id"])
        assert refreshed["ai_explanation"] == "neu"
        assert refreshed["context_sentence"] == "neu context"

    def test_cascade_delete_book(self):
        book_id, chapter = self.setup_book()
        annotation = db.create_annotation(
            chapter_id=chapter["id"],
            paragraph_index=0,
            start_offset=0,
            end_offset=4,
            selected_text="Jem",
            context_sentence="Jemand",
            ai_explanation="x",
        )
        db.add_chat_message(annotation["id"], "user", "frage")
        db.add_chat_message(annotation["id"], "assistant", "antwort")
        deleted = db.delete_book(book_id)
        assert deleted and deleted["id"] == book_id
        with db.connection() as conn:
            for table in ("chapters", "annotations", "chat_messages"):
                assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0

    def test_delete_annotation(self):
        _book_id, chapter = self.setup_book()
        annotation = db.create_annotation(
            chapter_id=chapter["id"],
            paragraph_index=0,
            start_offset=0,
            end_offset=4,
            selected_text="Jem",
            context_sentence="Jemand",
            ai_explanation="x",
        )
        assert db.delete_annotation(annotation["id"]) is True
        assert db.delete_annotation(annotation["id"]) is False


class TestSettingsAndSearch:
    def test_settings(self):
        assert db.get_setting("active_model") == "deepseek"
        db.set_setting("active_model", "gemini")
        db.set_settings({"font_size": "24", "theme": "dark"})
        assert db.get_setting("active_model") == "gemini"
        settings = db.get_settings()
        assert settings["font_size"] == "24"
        assert settings["theme"] == "dark"

    def test_search_book(self):
        book_id = create_book()
        results = db.search_book(book_id, "verhaftet")
        assert len(results) == 1
        assert results[0]["chapter_index"] == 0
        assert results[0]["paragraph_index"] == 1
        assert "verhaftet" in results[0]["snippet"]
        assert db.search_book(book_id, "Kafka") == []

    def test_library_stats(self):
        create_book()
        stats = db.library_stats()
        assert stats["books"] == 1
        assert stats["words"] == 17


class TestConcurrency:
    def test_parallel_imports(self):
        def worker(index: int) -> int:
            return create_book(f"Buch {index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(worker, range(8)))
        assert len(set(ids)) == 8
        assert len(db.list_books()) == 8
