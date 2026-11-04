"""AI 服务单元测试：JSON 容错、Prompt 占位符、模型请求、降级与错误映射。"""

from __future__ import annotations

import json

import pytest

import ai_service
import database as db
import prompts


class TestJsonParsing:
    def test_plain_object(self):
        assert ai_service._extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_object_with_prose(self):
        raw = '好的，结果如下：\n```json\n{"summary": "概要", "questions": ["a", "b"]}\n```'
        assert ai_service._extract_json(raw)["summary"] == "概要"

    def test_trailing_comma(self):
        assert ai_service._extract_json('{"a": 1, "b": [1, 2,],}')["b"] == [1, 2]

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ai_service._extract_json("完全没有 JSON")


class TestSelectionPrompt:
    def test_placeholders(self):
        prompt = ai_service._build_selection_prompt(
            "解释 {selected_text}，上下文：{context_sentence}", "verhaftet", "Er wurde verhaftet."
        )
        assert "verhaftet" in prompt
        assert "Er wurde verhaftet." in prompt

    def test_missing_placeholders_are_appended(self):
        prompt = ai_service._build_selection_prompt("请解释这个德语词。", "verhaftet", "Er wurde verhaftet.")
        assert "verhaftet" in prompt
        assert "Er wurde verhaftet." in prompt

    def test_empty_template_uses_default(self):
        prompt = ai_service._build_selection_prompt("", "verhaftet", "Er wurde verhaftet.")
        assert "词义与用法" in prompt


class TestGuideFallback:
    def test_non_json_response_still_returns_three_questions(self, monkeypatch):
        monkeypatch.setattr(
            ai_service,
            "chat",
            lambda *args, **kwargs: "这一章讲述了约瑟夫·K 被逮捕后的困惑。问题一？问题二？问题三？",
        )
        guide = ai_service.generate_chapter_guide("Der Prozess", "Kapitel 1", "Text")
        assert guide["summary"]
        assert len(guide["questions"]) == 3
        assert guide["degraded"] is True

    def test_json_response(self, monkeypatch):
        monkeypatch.setattr(
            ai_service,
            "chat",
            lambda *args, **kwargs: json.dumps(
                {"summary": "概要", "questions": ["一", "二", "三"]}, ensure_ascii=False
            ),
        )
        guide = ai_service.generate_chapter_guide("Buch", "Kapitel", "Text")
        assert guide["summary"] == "概要"
        assert guide["questions"] == ["一", "二", "三"]
        assert guide["degraded"] is False


class TestBookBackground:
    def test_search_augmented_generation(self, monkeypatch):
        monkeypatch.setattr(
            ai_service,
            "web_search",
            lambda *args, **kwargs: {
                "available": True,
                "summaries": ["摘要一", "摘要二"],
                "results": 2,
                "error": None,
            },
        )
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return json.dumps(
                {"book_summary": "概要", "author_bio": "生平", "cultural_context": "背景"},
                ensure_ascii=False,
            )

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        result = ai_service.generate_book_background("Der Prozess", "Franz Kafka", "Jemand musste...")
        assert result["book_summary"] == "概要"
        assert result["author_bio"] == "生平"
        assert result["cultural_context"] == "背景"
        assert result["source"] == "search+model"
        assert "摘要一" in captured["messages"][1]["content"]

    def test_search_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            ai_service,
            "web_search",
            lambda *args, **kwargs: {"available": False, "summaries": [], "error": "404", "results": 0},
        )
        monkeypatch.setattr(
            ai_service,
            "chat",
            lambda *args, **kwargs: '{"book_summary": "概要", "author_bio": "", "cultural_context": ""}',
        )
        result = ai_service.generate_book_background("Buch", None, "Text")
        assert result["source"] == "model_knowledge"
        assert result["book_summary"] == "概要"

    def test_invalid_json_falls_back_to_text(self, monkeypatch):
        monkeypatch.setattr(
            ai_service, "web_search", lambda *args, **kwargs: {"available": False, "summaries": []}
        )
        monkeypatch.setattr(ai_service, "chat", lambda *args, **kwargs: "这是一段没有 JSON 的背景文本。")
        result = ai_service.generate_book_background("Buch", "Autor", "Text")
        assert "背景文本" in result["book_summary"]


class TestWebSearch:
    def test_missing_key_returns_unavailable(self):
        result = ai_service.web_search("Kafka Hintergrund")
        assert result["available"] is False
        assert "Key" in result["error"]

    def test_search_failure_degrades(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")

        def boom(*args, **kwargs):
            raise ai_service.AIServiceError("404 Not Found", code="not_found")

        monkeypatch.setattr(ai_service, "_post_json", boom)
        result = ai_service.web_search("Kafka")
        assert result["available"] is False
        assert "404" in result["error"]

    def test_filters_low_scores(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        monkeypatch.setattr(
            ai_service,
            "_post_json",
            lambda *args, **kwargs: {
                "results": [
                    {"summary": "高分摘要", "rerank_score": 2.0},
                    {"summary": "低分摘要", "rerank_score": 0.2},
                    {"summary": "无分数摘要"},
                ]
            },
        )
        result = ai_service.web_search("Kafka")
        assert result["available"] is True
        assert result["summaries"] == ["高分摘要"]


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "API Key 无效"),
            (402, "余额不足"),
            (429, "限流"),
            (500, "暂时不可用"),
        ],
    )
    def test_http_error_mapping(self, status, expected):
        with pytest.raises(ai_service.AIServiceError) as info:
            ai_service._raise_http_error(status, '{"error": {"message": "detail"}}')
        assert expected in info.value.message

    def test_missing_key_chat(self):
        with pytest.raises(ai_service.AIServiceError) as info:
            ai_service.chat([{"role": "user", "content": "Hallo"}])
        assert info.value.code == "missing_key"
        assert "API Key" in info.value.message


class TestProviderRequests:
    def test_deepseek_request_shape(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("active_model", "deepseek")
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs["payload"]
            captured["headers"] = kwargs["headers"]
            return {"choices": [{"message": {"content": "Hallo zurück"}}]}

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        reply = ai_service.chat([{"role": "user", "content": "Hallo"}], json_mode=True)
        assert reply == "Hallo zurück"
        assert captured["url"].endswith("/chat/completions")
        assert captured["payload"]["model"] == "deepseek-flash"
        assert captured["payload"]["response_format"] == {"type": "json_object"}
        assert captured["headers"]["Authorization"].startswith("Bearer sk-")

    def test_openai_request_shape(self, monkeypatch):
        db.set_setting("openai_key", "sk-test-openai")
        db.set_setting("openai_base_url", "http://localhost:1234/v1/")
        db.set_setting("openai_model", "local-model")
        db.set_setting("active_model", "openai")
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs["payload"]
            captured["headers"] = kwargs["headers"]
            return {"choices": [{"message": {"content": "Hallo"}}]}

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        assert ai_service.chat([{"role": "user", "content": "Hallo"}]) == "Hallo"
        assert captured["url"] == "http://localhost:1234/v1/chat/completions"
        assert captured["payload"]["model"] == "local-model"
        assert captured["payload"]["stream"] is False
        assert "thinking" not in captured["payload"]
        assert captured["payload"]["reasoning_effort"] == "medium"
        assert captured["headers"]["Authorization"] == "Bearer sk-test-openai"

    def test_openai_key_is_optional_but_model_is_required(self):
        db.set_setting("openai_key", "")
        db.set_setting("openai_model", "local-model")
        db.set_setting("openai_base_url", "http://localhost:11434/v1")
        db.set_setting("active_model", "openai")
        config = ai_service._provider_config()
        assert config["key"] == ""
        assert config["model"] == "local-model"
        db.set_setting("openai_model", "")
        with pytest.raises(ai_service.AIServiceError) as info:
            ai_service._provider_config()
        assert info.value.code == "missing_model"

    def test_openai_thinking_effort_and_fallback(self, monkeypatch):
        db.set_setting("openai_key", "sk-test-openai")
        db.set_setting("openai_base_url", "http://localhost:1234/v1")
        db.set_setting("openai_model", "local-model")
        db.set_setting("active_model", "openai")
        db.set_setting("thinking_enabled", "0")
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs["payload"])
            return {"choices": [{"message": {"content": "ok"}}]}

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        assert ai_service.chat([{"role": "user", "content": "Hi"}]) == "ok"
        assert captured["reasoning_effort"] == "none"

        ai_service._THINKING_UNSUPPORTED.clear()
        db.set_setting("thinking_enabled", "1")
        calls = []

        def fake_post_with_rejection(url, **kwargs):
            calls.append(kwargs["payload"].copy())
            if len(calls) == 1:
                raise ai_service.AIServiceError(
                    "Unsupported parameter: reasoning_effort", status_code=400
                )
            return {"choices": [{"message": {"content": "final"}}]}

        monkeypatch.setattr(ai_service, "_post_json", fake_post_with_rejection)
        assert ai_service.chat([{"role": "user", "content": "Hi"}]) == "final"
        assert calls[0]["reasoning_effort"] == "medium"
        assert "reasoning_effort" not in calls[1]

    def test_gemini_request_shape(self, monkeypatch):
        db.set_setting("gemini_key", "AIza-test-key")
        db.set_setting("active_model", "gemini")
        captured = {}
        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs["payload"]
            captured["headers"] = kwargs["headers"]
            return {
                "candidates": [{"content": {"parts": [{"text": "Antwort"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "totalTokenCount": 5},
            }

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        reply = ai_service.chat(
            [
                {"role": "system", "content": "System"},
                {"role": "user", "content": "Hallo"},
            ],
            json_mode=True,
        )
        assert reply == "Antwort"
        assert ":generateContent" in captured["url"]
        assert captured["headers"]["x-goog-api-key"] == "AIza-test-key"
        assert captured["payload"]["systemInstruction"]["parts"][0]["text"] == "System"
        assert captured["payload"]["generationConfig"]["responseMimeType"] == "application/json"

    def test_explain_selection_uses_custom_prompt(self, monkeypatch):
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return "### 解析\n内容"

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        result = ai_service.explain_selection(
            "verhaftet",
            "Er wurde verhaftet.",
            book_title="Der Prozess",
            chapter_title="Kapitel 1",
            prompt_template="自定义模板：{selected_text} / {context_sentence}",
        )
        assert result.startswith("### 解析")
        assert "自定义模板：verhaftet / Er wurde verhaftet." in captured["messages"][1]["content"]

    def test_followup_includes_history(self, monkeypatch):
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return "回答"

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        ai_service.chat_followup(
            selected_text="verhaftet",
            context_sentence="Er wurde verhaftet.",
            explanation="解析",
            history=[
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
            question="第二问",
            book_title="Der Prozess",
        )
        contents = [message["content"] for message in captured["messages"]]
        assert "第一问" in contents
        assert "第一答" in contents
        assert contents[-1] == "第二问"
        context_prompt = ai_service.prompts.render_prompt(
            ai_service.prompts.FOLLOWUP_CONTEXT_TEMPLATE,
            {
                "selected_text": "verhaftet",
                "context_sentence": "Er wurde verhaftet.",
                "explanation": "解析",
            },
        )
        assert sum(1 for content in contents if context_prompt in content) == 1
        assert all(message["role"] != "assistant" or message["content"] != "已了解原句、上下文与首次解析。请提问。" for message in captured["messages"])
        assert captured["messages"][0]["role"] == "system"


class TestStatus:
    def test_provider_status_missing_keys(self):
        status = ai_service.provider_status()
        assert status["has_deepseek_key"] is False
        assert status["has_gemini_key"] is False
        assert status["active_model"] == "deepseek"
        assert status["thinking_enabled"] is True
        assert status["thinking_effort"] == "medium"

    def test_prompts_have_placeholders(self):
        assert "{selected_text}" in prompts.DEFAULT_SELECTION_PROMPT
        assert "{context_sentence}" in prompts.DEFAULT_SELECTION_PROMPT


class TestThinkingMode:
    def _deepseek_response(self, content: str = "最终回答", reasoning: str = "") -> dict:
        message = {"content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return {"choices": [{"message": message, "finish_reason": "stop"}]}

    def test_enabled_payload(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "1")
        db.set_setting("thinking_effort", "high")
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs["payload"])
            return self._deepseek_response("最终回答")

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        assert ai_service.chat([{"role": "user", "content": "Hallo"}], max_tokens=1200) == "最终回答"
        assert captured["thinking"] == {"type": "enabled"}
        assert captured["reasoning_effort"] == "high"
        assert captured["max_tokens"] >= 8192
        assert "temperature" not in captured

    def test_disabled_payload(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "0")
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs["payload"])
            return self._deepseek_response("最终回答")

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        assert ai_service.chat([{"role": "user", "content": "Hallo"}], max_tokens=1200) == "最终回答"
        assert captured["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in captured
        assert captured["max_tokens"] == 1200
        assert captured["temperature"] == 0.5

    def test_reasoning_only_retries_without_thinking(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "1")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["payload"])
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {"content": "", "reasoning_content": "只有思考过程"},
                            "finish_reason": "length",
                        }
                    ]
                }
            return self._deepseek_response("这是正式回答")

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        result = ai_service.chat([{"role": "user", "content": "Hallo"}])
        assert result == "这是正式回答"
        assert "只有思考过程" not in result
        assert len(calls) == 2
        assert calls[0]["thinking"] == {"type": "enabled"}
        assert calls[1]["thinking"] == {"type": "disabled"}

    def test_reasoning_only_raises_clear_error(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "1")

        def fake_post(url, **kwargs):
            return {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "只有思考过程"},
                        "finish_reason": "length",
                    }
                ]
            }

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        with pytest.raises(ai_service.AIServiceError) as info:
            ai_service.chat([{"role": "user", "content": "Hallo"}])
        assert info.value.code == "reasoning_only"
        assert "思考" in info.value.message

    def test_unsupported_thinking_parameter_falls_back(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "1")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["payload"])
            if len(calls) == 1:
                raise ai_service.AIServiceError(
                    "HTTP 400，服务返回：Unsupported parameter: thinking", status_code=400
                )
            return self._deepseek_response("最终回答")

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        assert ai_service.chat([{"role": "user", "content": "Hallo"}]) == "最终回答"
        assert "thinking" not in calls[1]
        assert "reasoning_effort" not in calls[1]

        # 能力缓存：下一次直接省略 thinking，不再多一次失败请求。
        assert ai_service.chat([{"role": "user", "content": "Noch einmal"}]) == "最终回答"
        assert len(calls) == 3
        assert "thinking" not in calls[2]

    def test_gemini_thoughts_are_filtered(self, monkeypatch):
        db.set_setting("gemini_key", "AIza-test-key")
        db.set_setting("active_model", "gemini")
        db.set_setting("thinking_enabled", "1")
        db.set_setting("thinking_effort", "medium")
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs["payload"])
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "这是模型思考，不应展示", "thought": True},
                                {"text": "这是正式回答"},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"thoughtsTokenCount": 20, "totalTokenCount": 60},
            }

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        result = ai_service.chat([{"role": "user", "content": "Hallo"}])
        assert result == "这是正式回答"
        assert "思考" not in result
        config = captured["generationConfig"]["thinkingConfig"]
        assert config["thinkingLevel"] == "medium"
        assert config["includeThoughts"] is False
        assert captured["generationConfig"]["maxOutputTokens"] > 4096

    def test_gemini_thought_only_retries_disabled(self, monkeypatch):
        db.set_setting("gemini_key", "AIza-test-key")
        db.set_setting("active_model", "gemini")
        db.set_setting("thinking_enabled", "1")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["payload"])
            if len(calls) == 1:
                return {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "只有思考", "thought": True}]},
                            "finishReason": "MAX_TOKENS",
                        }
                    ]
                }
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "正式回答"}]}, "finishReason": "STOP"}
                ]
            }

        monkeypatch.setattr(ai_service, "_post_json", fake_post)
        result = ai_service.chat([{"role": "user", "content": "Hallo"}])
        assert result == "正式回答"
        assert len(calls) == 2
        assert calls[1]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"

    def test_gemini_config_levels(self):
        base = {"provider": "gemini", "thinking_enabled": True, "thinking_effort": "high"}
        config, budget = ai_service._gemini_thinking_config(
            {**base, "model": "gemini-2.5-flash"}
        )
        assert config and config["thinkingBudget"] == 12288 and budget == 12288
        config, _ = ai_service._gemini_thinking_config(
            {**base, "model": "gemini-3.8-flash"}
        )
        assert config and config["thinkingLevel"] == "high"
        config, budget = ai_service._gemini_thinking_config(
            {"provider": "gemini", "model": "gemini-2.5-pro", "thinking_enabled": False}
        )
        assert config and config["thinkingBudget"] == 128

    def test_settings_api_thinking_fields(self):
        # 用数据库层快速覆盖默认值，API 层测试在 test_api.py。
        db.set_setting("thinking_enabled", "0")
        db.set_setting("thinking_effort", "max")
        status = ai_service.provider_status()
        assert status["thinking_enabled"] is False
        assert status["thinking_effort"] == "max"


class FakeStreamResponse:
    def __init__(self, chunks):
        self.status_code = 200
        self._chunks = chunks
        self.closed = False

    def iter_lines(self):
        for chunk in self._chunks:
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True


class TestStreaming:
    def test_deepseek_stream_ignores_reasoning_and_parses_deltas(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "1")
        captured = {}

        def fake_open(url, **kwargs):
            captured.update(kwargs["payload"])
            return FakeStreamResponse(
                [
                    b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
                    b'data: {"choices":[{"delta":{"content":"\\u6b63\\u5f0f"}}]}',
                    b'data: {"choices":[{"delta":{"content":"\\u56de\\u7b54"}}]}',
                    b"data: [DONE]",
                ]
            )

        monkeypatch.setattr(ai_service, "_open_stream", fake_open)
        assert "".join(ai_service.stream_chat([{"role": "user", "content": "Hallo"}])) == "正式回答"
        assert captured["stream"] is True
        assert captured["thinking"] == {"type": "enabled"}
        assert captured["max_tokens"] >= 8192
        assert "temperature" not in captured

    def test_deepseek_stream_retries_when_only_reasoning(self, monkeypatch):
        db.set_setting("deepseek_key", "sk-test-key-123456")
        db.set_setting("thinking_enabled", "1")
        payloads = []

        def fake_open(url, **kwargs):
            payloads.append(kwargs["payload"])
            if len(payloads) == 1:
                return FakeStreamResponse(
                    [
                        b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
                        b"data: [DONE]",
                    ]
                )
            return FakeStreamResponse(
                [b'data: {"choices":[{"delta":{"content":"final answer"}}]}']
            )

        monkeypatch.setattr(ai_service, "_open_stream", fake_open)
        result = "".join(ai_service.stream_chat([{"role": "user", "content": "Hallo"}]))
        assert result == "final answer"
        assert payloads[0]["thinking"] == {"type": "enabled"}
        assert payloads[1]["thinking"] == {"type": "disabled"}
        assert payloads[1]["max_tokens"] >= 4096

    def test_gemini_stream_filters_thought_parts(self, monkeypatch):
        db.set_setting("gemini_key", "AIza-test-key")
        db.set_setting("active_model", "gemini")
        db.set_setting("gemini_model", "gemini-2.5-flash")
        db.set_setting("thinking_enabled", "1")
        captured = {}

        def fake_open(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs["payload"])
            return FakeStreamResponse(
                [
                    b'data: {"candidates":[{"content":{"parts":[{"text":"internal thought","thought":true}]}}]}',
                    b'data: {"candidates":[{"content":{"parts":[{"text":"\\u6b63\\u5f0f\\u56de\\u7b54"}]},"finishReason":"STOP"}]}',
                ]
            )

        monkeypatch.setattr(ai_service, "_open_stream", fake_open)
        assert "".join(ai_service.stream_chat([{"role": "user", "content": "Hallo"}])) == "正式回答"
        assert captured["url"].endswith(":streamGenerateContent?alt=sse")
        assert captured["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 4096

    def test_openai_stream(self, monkeypatch):
        db.set_setting("openai_key", "sk-test-openai")
        db.set_setting("openai_base_url", "http://localhost:1234/v1")
        db.set_setting("openai_model", "local-model")
        db.set_setting("active_model", "openai")
        captured = {}

        def fake_open(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs["payload"])
            return FakeStreamResponse(
                [
                    b'data: {"choices":[{"delta":{"content":"Hallo"}}]}',
                    b'data: {"choices":[{"delta":{"content":" Welt"}}]}',
                    b"data: [DONE]",
                ]
            )

        monkeypatch.setattr(ai_service, "_open_stream", fake_open)
        assert "".join(ai_service.stream_chat([{"role": "user", "content": "Hallo"}])) == "Hallo Welt"
        assert captured["url"] == "http://localhost:1234/v1/chat/completions"
        assert captured["model"] == "local-model"
        assert captured["stream"] is True
        assert "thinking" not in captured
        assert captured["reasoning_effort"] == "medium"


class TestPromptOverrides:
    def test_chapter_prompts_are_overridable(self, monkeypatch):
        db.set_setting("prompt_chapter_system", "CUSTOM CHAPTER SYSTEM")
        db.set_setting(
            "prompt_chapter_user",
            "B={book_title}|C={chapter_title}|T={chapter_text}|L={language}",
        )
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return '{"summary":"s","questions":["q1","q2","q3"]}'

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        result = ai_service.generate_chapter_guide("Buch", "Kapitel", "Text", language="de")
        assert result["questions"] == ["q1", "q2", "q3"]
        assert captured["messages"][0]["content"] == "CUSTOM CHAPTER SYSTEM"
        assert captured["messages"][1]["content"] == "B=Buch|C=Kapitel|T=Text|L=de"

    def test_chapter_prompt_can_request_fewer_questions(self, monkeypatch):
        db.set_setting("prompt_chapter_user", "只问 1 个问题：{chapter_text}")

        def fake_chat(messages, **kwargs):
            return '{"summary":"s","questions":["only one"]}'

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        result = ai_service.generate_chapter_guide("Buch", "Kapitel", "Text")
        assert result["questions"] == ["only one"]

    def test_background_and_followup_prompts_are_overridable(self, monkeypatch):
        db.set_setting("prompt_background_system", "CUSTOM BACKGROUND SYSTEM")
        db.set_setting(
            "prompt_background_user",
            "T={title}|A={author}|E={excerpt}|R={reference}",
        )
        db.set_setting("prompt_followup_system", "CUSTOM FOLLOWUP SYSTEM")
        db.set_setting(
            "prompt_followup_context",
            "S={selected_text}|C={context_sentence}|E={explanation}",
        )
        captured = []

        def fake_chat(messages, **kwargs):
            captured.append(messages)
            if len(captured) == 1:
                return '{"book_summary":"s","author_bio":"a","cultural_context":"c"}'
            return "followup answer"

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        monkeypatch.setattr(
            ai_service, "web_search", lambda query: {"available": False, "summaries": []}
        )
        background = ai_service.generate_book_background("Buch", "Autor", "Anfang")
        assert background["book_summary"] == "s"
        assert captured[0][0]["content"] == "CUSTOM BACKGROUND SYSTEM"
        assert captured[0][1]["content"] == "T=Buch|A=Autor|E=Anfang|R="

        answer = ai_service.chat_followup(
            selected_text="Wort",
            context_sentence="Satz",
            explanation="Erklärung",
            history=[],
            question="Warum?",
            book_title="Buch",
        )
        assert answer == "followup answer"
        assert captured[1][0]["role"] == "system"
        assert "CUSTOM FOLLOWUP SYSTEM 当前阅读：《Buch》。" in captured[1][0]["content"]
        assert "S=Wort|C=Satz|E=Erklärung" in captured[1][0]["content"]
        assert captured[1][-1]["content"] == "Warum?"
        assert len(captured[1]) == 2

    def test_identify_prompts_are_overridable(self, monkeypatch):
        db.set_setting("prompt_identify_system", "CUSTOM IDENTIFY SYSTEM")
        db.set_setting("prompt_identify_user", "F={filename}|E={excerpt}")
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return '{"title":"Buch","author":"Autor","confidence":"high","reason":"ok"}'

        monkeypatch.setattr(ai_service, "chat", fake_chat)
        result = ai_service.identify_book("file.txt", "Anfang")
        assert result["title"] == "Buch"
        assert captured["messages"][0]["content"] == "CUSTOM IDENTIFY SYSTEM"
        assert captured["messages"][1]["content"] == "F=file.txt|E=Anfang"
