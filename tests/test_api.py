"""FastAPI 集成测试：导入、章节、划词缓存、追问、设置、搜索、删除。"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz
import pytest

import ai_service
import database as db
from conftest import GERMAN_TEXT, import_text


def first_paragraph(client, book_id: int, index: int = 0) -> tuple[dict, str]:
    response = client.get(f"/api/books/{book_id}/chapters/{index}")
    assert response.status_code == 200, response.text
    chapter = response.json()
    return chapter, chapter["paragraphs"][0]


def parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            try:
                data = json.loads("\n".join(data_lines))
            except ValueError:
                data = "\n".join(data_lines)
            events.append({"event": event, "data": data})
    return events


class TestHealthAndImport:
    def test_health(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"]
        assert set(data["db"]["tables"]) >= {
            "books",
            "chapters",
            "annotations",
            "chat_messages",
            "settings",
        }

    def test_import_txt_generates_background_and_guides(self, client):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        book = import_text(client)
        assert book["title"] == "Der Prozess"
        assert book["author"] == "Franz Kafka"
        assert book["chapter_count"] == 3
        assert book["word_count"] > 50
        detail = client.get(f"/api/books/{book['id']}").json()
        assert detail["book_summary"] == "这是一部关于荒诞司法程序的长篇小说。"
        assert detail["author_bio"]
        assert detail["cultural_context"]
        assert detail["chapters"][0]["is_guide_ready"] is True
        assert detail["chapters"][1]["is_guide_ready"] is True
        assert detail["chapters"][2]["is_guide_ready"] is False

    def test_import_parse_metadata(self, client):
        response = client.post(
            "/api/books/import",
            files={"file": ("Der Prozess.txt", GERMAN_TEXT.encode("utf-8"), "text/plain")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["parse"]["toc_source"] == "headings"
        assert payload["parse"]["encoding"] == "utf-8"
        assert payload["parse"]["original_filename"] == "Der Prozess.txt"
        assert payload["parse"]["chapter_count"] == 3

    def test_import_pdf(self, client, tmp_path):
        doc = fitz.open()
        for index in range(1, 4):
            page = doc.new_page()
            page.insert_textbox(
                fitz.Rect(60, 60, 540, 760),
                f"Kapitel {index}\n\nDies ist der Text von Kapitel {index}. "
                "Er enthält deutsche Umlaute äöüß und genügend Wörter für die Prüfung. "
                "Noch ein Satz, damit die Seite gefüllt ist.",
                fontsize=11,
                fontname="helv",
            )
        path = tmp_path / "test.pdf"
        doc.save(path)
        doc.close()
        with path.open("rb") as handle:
            response = client.post(
                "/api/books/import", files={"file": ("Kapitelbuch.pdf", handle, "application/pdf")}
            )
        assert response.status_code == 200, response.text
        book = response.json()["book"]
        assert book["file_type"] == "pdf"
        assert book["chapter_count"] == 3

    def test_import_rejects_unsupported_and_empty(self, client):
        response = client.post(
            "/api/books/import", files={"file": ("evil.exe", b"test", "application/octet-stream")}
        )
        assert response.status_code == 400
        assert "不支持" in response.json()["detail"]
        response = client.post(
            "/api/books/import", files={"file": ("empty.txt", b"", "text/plain")}
        )
        assert response.status_code == 400

    def test_list_books_and_stats(self, client):
        import_text(client)
        data = client.get("/api/books").json()
        assert len(data["books"]) == 1
        assert data["stats"]["books"] == 1
        assert data["stats"]["words"] == data["books"][0]["word_count"]

    def test_download_and_delete_file(self, client):
        book = import_text(client)
        response = client.get(f"/api/books/{book['id']}/file")
        assert response.status_code == 200
        assert "attachment" in response.headers.get("content-disposition", "")
        deleted = client.delete(f"/api/books/{book['id']}")
        assert deleted.status_code == 200
        assert client.get(f"/api/books/{book['id']}").status_code == 404
        assert client.get(f"/api/books/{book['id']}/file").status_code == 404


class TestChapters:
    def test_chapter_payload_and_progress(self, client):
        book = import_text(client)
        chapter, first = first_paragraph(client, book["id"])
        assert chapter["chapter_index"] == 0
        assert chapter["paragraphs"]
        assert first.startswith("Jemand musste")
        assert chapter["total_chapters"] == 3
        assert chapter["next_chapter"] == 1
        assert chapter["prev_chapter"] is None
        assert chapter["annotations"] == []
        # 打开章节时自动保存进度
        assert client.get(f"/api/books/{book['id']}").json()["current_chapter"] == 0

    def test_prepare_guide_lazy(self, client):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        book = import_text(client)
        chapters = client.get(f"/api/books/{book['id']}/chapters").json()["chapters"]
        target = chapters[2]
        assert target["is_guide_ready"] is False
        response = client.post(f"/api/chapters/{target['id']}/prepare_guide")
        assert response.status_code == 200
        assert response.json()["status"] in {"queued", "generating", "ready"}
        refreshed = client.get(f"/api/books/{book['id']}/chapters/2").json()
        assert refreshed["is_guide_ready"] is True
        assert len(refreshed["questions"]) == 3

    def test_generate_guide_force(self, client):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        book = import_text(client)
        response = client.post(f"/api/books/{book['id']}/chapters/0/guide?force=true")
        assert response.status_code == 200
        assert response.json()["is_guide_ready"] is True
        assert len(response.json()["questions"]) == 3

    def test_progress_validation(self, client):
        book = import_text(client)
        assert client.patch(
            f"/api/books/{book['id']}/progress", json={"chapter_index": 2}
        ).status_code == 200
        assert client.get(f"/api/books/{book['id']}").json()["current_chapter"] == 2
        response = client.patch(
            f"/api/books/{book['id']}/progress", json={"chapter_index": 99}
        )
        assert response.status_code == 400
        assert "超出" in response.json()["detail"]


class TestAnnotations:
    def test_annotate_cache_regenerate_and_chat(self, client):
        book = import_text(client)
        chapter, paragraph = first_paragraph(client, book["id"])
        selected = paragraph[:8]
        body = {
            "chapter_id": chapter["id"],
            "paragraph_index": 0,
            "start_offset": 0,
            "end_offset": len(selected),
            "selected_text": selected,
            "context_sentence": paragraph,
            "force_regenerate": False,
        }
        first = client.post("/api/annotate", json=body)
        assert first.status_code == 200, first.text
        annotation = first.json()["annotation"]
        assert first.json()["cached"] is False
        assert annotation["selected_text"] == selected
        assert annotation["messages"] == []
        assert annotation["ai_explanation"]

        cached = client.post("/api/annotate", json=body)
        assert cached.status_code == 200
        assert cached.json()["cached"] is True
        assert cached.json()["annotation"]["id"] == annotation["id"]

        regenerated = client.post("/api/annotate", json={**body, "force_regenerate": True})
        assert regenerated.status_code == 200
        assert regenerated.json()["cached"] is False
        assert regenerated.json()["annotation"]["id"] == annotation["id"]

        message = client.post(
            f"/api/annotations/{annotation['id']}/chat", json={"message": "为什么用 hätte？"}
        )
        assert message.status_code == 200
        payload = message.json()
        assert payload["user"]["role"] == "user"
        assert payload["assistant"]["role"] == "assistant"
        assert len(payload["messages"]) == 2

        detailed = client.get(f"/api/annotations/{annotation['id']}")
        assert detailed.status_code == 200
        assert len(detailed.json()["messages"]) == 2

        cleared = client.delete(f"/api/annotations/{annotation['id']}/chat")
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] == 2
        assert client.get(f"/api/annotations/{annotation['id']}").json()["messages"] == []

        deleted = client.delete(f"/api/annotations/{annotation['id']}")
        assert deleted.status_code == 200
        chapter_after = client.get(f"/api/books/{book['id']}/chapters/0").json()
        assert chapter_after["annotations"] == []

    def test_annotate_stream_and_cache(self, client):
        book = import_text(client)
        chapter, paragraph = first_paragraph(client, book["id"])
        selected = paragraph[:8]
        body = {
            "chapter_id": chapter["id"],
            "paragraph_index": 0,
            "start_offset": 0,
            "end_offset": len(selected),
            "selected_text": selected,
            "context_sentence": paragraph,
        }
        response = client.post("/api/annotate/stream", json=body)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        kinds = [event["event"] for event in events]
        assert "meta" in kinds and "delta" in kinds and "done" in kinds
        done = next(event["data"] for event in events if event["event"] == "done")
        assert done["cached"] is False
        assert done["annotation"]["selected_text"] == selected
        deltas = "".join(
            event["data"]["text"] for event in events if event["event"] == "delta"
        )
        assert deltas == done["annotation"]["ai_explanation"]

        cached = client.post("/api/annotate/stream", json=body)
        cached_events = parse_sse(cached.text)
        cached_done = next(event["data"] for event in cached_events if event["event"] == "done")
        assert cached_done["cached"] is True
        meta = next(event["data"] for event in cached_events if event["event"] == "meta")
        assert meta["cached"] is True

    def test_chat_stream(self, client):
        book = import_text(client)
        chapter, paragraph = first_paragraph(client, book["id"])
        selected = paragraph[:8]
        body = {
            "chapter_id": chapter["id"],
            "paragraph_index": 0,
            "start_offset": 0,
            "end_offset": len(selected),
            "selected_text": selected,
            "context_sentence": paragraph,
        }
        annotation = client.post("/api/annotate", json=body).json()["annotation"]
        response = client.post(
            f"/api/annotations/{annotation['id']}/chat/stream",
            json={"message": "为什么这里用 hätte？"},
        )
        assert response.status_code == 200, response.text
        events = parse_sse(response.text)
        kinds = [event["event"] for event in events]
        assert "delta" in kinds and "done" in kinds
        done = next(event["data"] for event in events if event["event"] == "done")
        assert len(done["messages"]) == 2
        assert done["messages"][0]["content"] == "为什么这里用 hätte？"
        deltas = "".join(
            event["data"]["text"] for event in events if event["event"] == "delta"
        )
        assert deltas == done["assistant"]["content"]

    def test_annotate_stream_error_event(self, client, monkeypatch):
        book = import_text(client)
        chapter, paragraph = first_paragraph(client, book["id"])
        selected = paragraph[:8]

        def broken_stream(*args, **kwargs):
            yield "已经生成的一部分"
            raise ai_service.AIServiceError("上游模型暂时不可用。", code="upstream_error")

        monkeypatch.setattr(ai_service, "explain_selection_stream", broken_stream)
        response = client.post(
            "/api/annotate/stream",
            json={
                "chapter_id": chapter["id"],
                "paragraph_index": 0,
                "start_offset": 0,
                "end_offset": len(selected),
                "selected_text": selected,
                "context_sentence": paragraph,
            },
        )
        assert response.status_code == 200
        events = parse_sse(response.text)
        error = next(event["data"] for event in events if event["event"] == "error")
        assert error["code"] == "upstream_error"
        assert "上游模型" in error["detail"]
        assert not any(event["event"] == "done" for event in events)

    def test_annotate_validation(self, client):
        book = import_text(client)
        chapter, paragraph = first_paragraph(client, book["id"])
        invalid = {
            "chapter_id": chapter["id"],
            "paragraph_index": 0,
            "start_offset": 0,
            "end_offset": 4,
            "selected_text": "XXXX",
            "context_sentence": paragraph,
        }
        response = client.post("/api/annotate", json=invalid)
        assert response.status_code == 400
        assert "选区" in response.json()["detail"]

        bad_paragraph = {**invalid, "selected_text": "Jemand", "paragraph_index": 999}
        assert client.post("/api/annotate", json=bad_paragraph).status_code == 400

        too_long = {**invalid, "selected_text": "x" * 501}
        assert client.post("/api/annotate", json=too_long).status_code == 422

        assert client.post("/api/annotate", json={**invalid, "chapter_id": 999999}).status_code == 404

    def test_annotation_nonexistent(self, client):
        assert client.get("/api/annotations/99999").status_code == 404
        assert client.delete("/api/annotations/99999").status_code == 404
        assert client.post("/api/annotations/99999/chat", json={"message": "hi"}).status_code == 404


class TestSettings:
    def test_settings_are_masked(self, client):
        settings = client.get("/api/settings").json()
        assert "deepseek_key" not in settings
        assert settings["has_deepseek_key"] is False
        assert settings["selection_prompt"]
        assert "{selected_text}" in settings["selection_prompt"]
        assert settings["thinking_enabled"] is True
        assert settings["thinking_effort"] == "medium"

    def test_update_thinking_settings(self, client):
        response = client.put(
            "/api/settings",
            json={"thinking_enabled": False, "thinking_effort": "low"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["thinking_enabled"] is False
        assert data["thinking_effort"] == "low"

        again = client.put(
            "/api/settings",
            json={"thinking_enabled": True, "thinking_effort": "max"},
        ).json()
        assert again["thinking_enabled"] is True
        assert again["thinking_effort"] == "max"

        invalid = client.put("/api/settings", json={"thinking_effort": "ultra"})
        assert invalid.status_code == 422
        assert "请求参数不合法" in invalid.json()["detail"]

    def test_guide_requires_available_model(self, client):
        book = import_text(client)
        chapter, _ = first_paragraph(client, book["id"])
        response = client.post(
            f"/api/books/{book['id']}/chapters/0/guide?force=true"
        )
        assert response.status_code == 400
        assert "未配置可用模型" in response.json()["detail"]
        fresh = client.get(f"/api/books/{book['id']}/chapters/0").json()
        assert fresh["is_guide_ready"] is False
        assert not fresh.get("guide_job") or fresh["guide_job"].get("status") != "running"
        prepare = client.post(f"/api/chapters/{chapter['id']}/prepare_guide")
        assert prepare.status_code == 200
        assert prepare.json()["status"] == "unavailable"

    def test_openai_settings_and_prompt_overrides(self, client):
        response = client.put(
            "/api/settings",
            json={
                "active_model": "openai",
                "openai_key": "sk-local-test",
                "openai_base_url": "http://localhost:11434/v1",
                "openai_model": "local-model",
                "prompts": {
                    "prompt_chapter_system": "CUSTOM CHAPTER SYSTEM",
                    "prompt_background_user": "T={title}|R={reference}",
                },
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["active_model"] == "openai"
        assert data["has_openai_key"] is True
        assert data["openai_base_url"] == "http://localhost:11434/v1"
        assert data["prompts"]["prompt_chapter_system"] == "CUSTOM CHAPTER SYSTEM"
        assert data["prompts"]["prompt_background_user"] == "T={title}|R={reference}"
        assert data["prompt_meta"]

        unknown = client.put(
            "/api/settings", json={"prompts": {"not_a_prompt": "value"}}
        )
        assert unknown.status_code == 400
        assert "未知的 Prompt" in unknown.json()["detail"]

    def test_update_settings_and_prompt(self, client):
        response = client.put(
            "/api/settings",
            json={
                "active_model": "gemini",
                "gemini_key": "AIzaTESTKEY1234567890",
                "deepseek_model": "deepseek-flash",
                "gemini_model": "gemini-3.8-flash",
                "theme": "dark",
                "font_size": 24,
                "reading_mode": "paginated",
                "auto_annotate": False,
                "selection_prompt": "请用简体中文解释 {selected_text}，并结合上下文 {context_sentence}。",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["active_model"] == "gemini"
        assert data["has_gemini_key"] is True
        assert "AIzaTESTKEY1234567890" not in json.dumps(data)
        assert data["deepseek_key_hint"] == ""
        assert data["gemini_key_hint"].startswith("AIzaT")
        assert data["theme"] == "dark"
        assert data["font_size"] == 24
        assert data["auto_annotate"] is False
        assert "请用简体中文解释" in data["selection_prompt"]

        raw = client.get("/api/settings").json()
        assert "AIzaTESTKEY1234567890" not in json.dumps(raw)

        cleared = client.put("/api/settings", json={"gemini_key": ""}).json()
        assert cleared["has_gemini_key"] is False

    def test_prompt_too_short(self, client):
        response = client.put("/api/settings", json={"selection_prompt": "short"})
        assert response.status_code == 400
        assert "Prompt" in response.json()["detail"]

    def test_reset_prompt(self, client):
        client.put(
            "/api/settings",
            json={"selection_prompt": "这是一个足够长的自定义提示词模板，用于测试重置功能是否有效。"},
        )
        response = client.post("/api/settings/reset-prompt")
        assert response.status_code == 200
        assert "{selected_text}" in response.json()["selection_prompt"]

    def test_connection_test_endpoint(self, client, monkeypatch):
        import ai_service

        monkeypatch.setattr(
            ai_service,
            "test_connection",
            lambda provider=None: {
                "ok": True,
                "provider": provider or "deepseek",
                "model": "mock-model",
                "latency_ms": 42,
                "reply": "Verbindung OK",
            },
        )
        response = client.post("/api/settings/test", json={"provider": "deepseek"})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["latency_ms"] == 42


class TestSearchAndErrors:
    def test_search(self, client):
        book = import_text(client)
        response = client.get(f"/api/books/{book['id']}/search", params={"q": "verhaftet"})
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1
        assert data["results"][0]["chapter_index"] == 0
        assert "verhaftet" in data["results"][0]["snippet"]
        assert client.get(f"/api/books/{book['id']}/search", params={"q": "x"}).status_code == 422

    def test_not_found(self, client):
        assert client.get("/api/books/9999").status_code == 404
        assert client.get("/api/books/9999/chapters/0").status_code == 404
        assert client.delete("/api/books/9999").status_code == 404
        assert client.get("/api/books/9999/search", params={"q": "test"}).status_code == 404

    def test_validation_error_is_chinese(self, client):
        response = client.post("/api/annotate", json={"chapter_id": 0})
        assert response.status_code == 422
        assert "请求参数不合法" in response.json()["detail"]

    def test_background_regenerate(self, client):
        book = import_text(client)
        response = client.post(f"/api/books/{book['id']}/background")
        assert response.status_code == 200, response.text
        assert response.json()["book"]["book_summary"]

    def test_system_info_and_qr(self, client):
        info = client.get("/api/system/info")
        assert info.status_code == 200
        data = info.json()
        assert data["version"]
        assert data["primary_url"].startswith("http")
        assert "PyMuPDF" in data["pdf_engine"]
        qr = client.get("/api/system/qr")
        assert qr.status_code == 200
        assert qr.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in qr.content
