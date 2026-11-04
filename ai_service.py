"""AI 服务层：DeepSeek / Gemini 双模型、联网背景检索、导读与划词讲解。

对外函数全部为同步调用（在 FastAPI 线程池中执行），任何失败都抛出
:class:`AIServiceError`，由路由层转换为统一的中文错误响应。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterator, Mapping, Sequence

import requests

import database as db
import prompts

log = logging.getLogger("reader.ai")

DEFAULT_TIMEOUT_SECONDS = 120
THINKING_EFFORTS = ("low", "medium", "high", "max")

#: 某些模型/网关不支持 thinking 参数；记录后后续请求直接跳过，避免每次多一次失败重试。
_THINKING_UNSUPPORTED: set[str] = set()


def _thinking_key(config: Mapping[str, Any]) -> str:
    return f"{config.get('provider')}:{config.get('model')}"


def _thinking_enabled(config: Mapping[str, Any]) -> bool:
    value = config.get("thinking_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no", ""}
    return bool(value)


def _thinking_effort(config: Mapping[str, Any]) -> str:
    effort = str(config.get("thinking_effort") or "medium").strip().lower()
    return effort if effort in THINKING_EFFORTS else "medium"


def _is_thinking_parameter_error(exc: AIServiceError) -> bool:
    """判断 400 是否由 thinking / reasoning_effort 不被支持引起。"""
    if exc.status_code not in (400, 422):
        return False
    message = (exc.message or "").lower()
    markers = (
        "thinking",
        "reasoning_effort",
        "reasoning effort",
        "unknown parameter",
        "unknown name",
        "unknown field",
        "unsupported parameter",
        "not supported",
        "unsupported",
        "extra inputs",
        "unexpected keyword",
        "invalid parameter",
        "cannot find field",
    )
    return any(marker in message for marker in markers)


class AIServiceError(Exception):
    """带用户可读中文信息的 AI 调用异常。"""

    def __init__(self, message: str, *, status_code: int = 502, code: str = "ai_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# --------------------------------------------------------------------------- #
# 配置与基础请求
# --------------------------------------------------------------------------- #
def _clean_key(value: str | None) -> str:
    if not value:
        return ""
    key = value.strip().strip('"').strip("'")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def _provider_config(provider: str | None = None) -> dict[str, Any]:
    provider = (provider or db.get_setting("active_model", "deepseek") or "deepseek").lower()
    if provider not in {"deepseek", "gemini", "openai"}:
        provider = "deepseek"
    if provider == "deepseek":
        key = _clean_key(db.get_setting("deepseek_key")) or _clean_key(
            os.environ.get("DEEPSEEK_API_KEY")
        )
        model = (db.get_setting("deepseek_model", "deepseek-flash") or "deepseek-flash").strip()
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        if not key:
            raise AIServiceError(
                "尚未配置 DeepSeek API Key。请点击右上角 ⚙️ 设置，填入 Key 后重试。",
                status_code=400,
                code="missing_key",
            )
    elif provider == "gemini":
        key = _clean_key(db.get_setting("gemini_key")) or _clean_key(
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
        model = (db.get_setting("gemini_model", "gemini-3.8-flash") or "gemini-3.8-flash").strip()
        base = os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        if not key:
            raise AIServiceError(
                "尚未配置 Gemini API Key。请点击右上角 ⚙️ 设置，填入 Key 后重试。",
                status_code=400,
                code="missing_key",
            )
    else:
        # OpenAI 兼容服务：Key 可为空（本地网关常见），但模型名必须填写。
        key = _clean_key(db.get_setting("openai_key")) or _clean_key(
            os.environ.get("OPENAI_API_KEY")
        )
        model = (
            db.get_setting("openai_model")
            or os.environ.get("OPENAI_MODEL")
            or ""
        ).strip()
        base = (
            db.get_setting("openai_base_url")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip().rstrip("/")
        if not model:
            raise AIServiceError(
                "尚未填写 OpenAI 兼容模型名。请在设置中选择 OpenAI 兼容服务并填写模型名。",
                status_code=400,
                code="missing_model",
            )
        if not base:
            raise AIServiceError(
                "尚未填写 OpenAI 兼容 Base URL。", status_code=400, code="missing_base_url"
            )
    thinking_effort = (db.get_setting("thinking_effort", "medium") or "medium").strip().lower()
    if thinking_effort not in THINKING_EFFORTS:
        thinking_effort = "medium"
    thinking_enabled = (db.get_setting("thinking_enabled", "1") or "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    return {
        "provider": provider,
        "key": key,
        "model": model,
        "base": base,
        "thinking_enabled": thinking_enabled,
        "thinking_effort": thinking_effort,
    }


def _prompt_value(name: str) -> str:
    """读取用户覆盖的 Prompt；空值或缺失时使用代码默认。"""
    value = db.get_setting(name)
    if value and value.strip():
        return value
    return prompts.PROMPT_DEFAULTS.get(name, "")


def _render_prompt(name: str, values: Mapping[str, Any]) -> str:
    return prompts.render_prompt(_prompt_value(name), dict(values))


def _timeout() -> tuple[int, int]:
    try:
        read_timeout = int(db.get_setting("ai_timeout", str(DEFAULT_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        read_timeout = DEFAULT_TIMEOUT_SECONDS
    return (10, max(20, min(read_timeout, 600)))


def _api_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or "").strip()
        if isinstance(error, str):
            return error.strip()
        message = payload.get("message")
        if message:
            return str(message)
    return ""


def _raise_http_error(status: int, body: str) -> None:
    detail = ""
    try:
        detail = _api_error_message(json.loads(body))
    except Exception:
        detail = body.strip()[:300]
    suffix = f"（服务返回：{detail}）" if detail else ""
    if status in (401, 403):
        raise AIServiceError(
            f"API Key 无效或没有访问权限，请在设置中检查 Key。{suffix}",
            status_code=401,
            code="unauthorized",
        )
    if status == 402:
        raise AIServiceError(
            f"账户余额不足或额度已用尽，请前往服务商充值。{suffix}",
            status_code=402,
            code="insufficient_balance",
        )
    if status == 429:
        raise AIServiceError(
            f"请求过于频繁，已触发限流，请稍后重试。{suffix}",
            status_code=429,
            code="rate_limited",
        )
    if status == 404:
        raise AIServiceError(
            f"接口地址不存在（模型名或服务地址可能有误）。{suffix}",
            status_code=502,
            code="not_found",
        )
    if status >= 500:
        raise AIServiceError(
            f"AI 服务暂时不可用（HTTP {status}），请稍后再试。{suffix}",
            status_code=502,
            code="upstream_error",
        )
    raise AIServiceError(
        f"AI 请求被拒绝（HTTP {status}）。{suffix}",
        status_code=400,
        code="bad_request",
    )


def _post_json(
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: tuple[int, int] | None = None,
    retries: int = 2,
) -> dict[str, Any]:
    """POST JSON，带有限重试与友好错误映射。"""
    timeout = timeout or _timeout()
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            with requests.Session() as session:
                response = session.post(
                    url, headers={**headers, "Content-Type": "application/json"}, json=payload,
                    timeout=timeout,
                )
            elapsed = time.monotonic() - started
            log.info("POST %s -> %s in %.2fs", _mask_url(url), response.status_code, elapsed)
            if response.status_code >= 400:
                body = response.text
                if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.2 * (attempt + 1))
                    last_error = AIServiceError(
                        f"AI 服务暂时不可用（HTTP {response.status_code}）。"
                    )
                    continue
                _raise_http_error(response.status_code, body)
            try:
                data = response.json()
            except ValueError as exc:
                raise AIServiceError(
                    "AI 服务返回了无法解析的响应，请稍后重试。", code="bad_response"
                ) from exc
            if not isinstance(data, dict):
                raise AIServiceError("AI 服务返回格式异常。", code="bad_response")
            return data
        except requests.Timeout as exc:
            last_error = AIServiceError(
                "AI 请求超时。网络较慢或文本过长，请稍后重试。",
                status_code=504,
                code="timeout",
            )
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise last_error from exc
        except requests.RequestException as exc:
            last_error = AIServiceError(
                "无法连接 AI 服务，请检查网络或代理设置。", status_code=502, code="network"
            )
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise last_error from exc
    raise last_error or AIServiceError("AI 请求失败。", code="unknown")


def _mask_url(url: str) -> str:
    return re.sub(r"([?&]key=)[^&]+", r"\1***", url)


def _extract_json(text: str) -> Any:
    """从模型输出中提取 JSON，容忍代码块、前后说明和尾随逗号。"""
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    candidates = [cleaned]
    object_start, object_end = cleaned.find("{"), cleaned.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(cleaned[object_start : object_end + 1])
    array_start, array_end = cleaned.find("["), cleaned.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(cleaned[array_start : array_end + 1])
    for candidate in candidates:
        for fixer in (lambda s: s, lambda s: re.sub(r",\s*([}\]])", r"\1", s)):
            try:
                return json.loads(fixer(candidate))
            except (TypeError, ValueError):
                continue
    raise ValueError("no json object found")


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def _parse_deepseek_choice(data: Mapping[str, Any]) -> tuple[str, str, str | None, Mapping[str, Any]]:
    choices = data.get("choices") or []
    if not choices:
        raise AIServiceError(
            f"DeepSeek 没有返回结果：{_api_error_message(data) or '未知原因'}",
            code="empty_choice",
        )
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()
    return content, reasoning, choice.get("finish_reason"), data.get("usage") or {}


def _deepseek_chat(
    messages: Sequence[Mapping[str, str]],
    *,
    config: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    model = str(config["model"])
    thinking_enabled = _thinking_enabled(config)
    effort = _thinking_effort(config)
    cache_key = _thinking_key(config)
    url = f"{config['base']}/chat/completions"

    def build_payload(thinking_mode: str | None, output_limit: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "max_tokens": max(1, int(output_limit)),
        }
        if thinking_mode:
            payload["thinking"] = {"type": thinking_mode}
            if thinking_mode == "enabled":
                payload["reasoning_effort"] = effort
        # 思考模式不支持 temperature；关闭思考或使用旧 reasoner 时按需携带。
        if thinking_mode != "enabled" and "reasoner" not in model.lower():
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _post_json(
            url,
            headers={"Authorization": f"Bearer {config['key']}", "Accept": "application/json"},
            payload=payload,
        )

    thinking_mode: str | None = None
    if cache_key not in _THINKING_UNSUPPORTED:
        thinking_mode = "enabled" if thinking_enabled else "disabled"
    output_limit = max(int(max_tokens), 8192) if thinking_mode == "enabled" else int(max_tokens)
    log.info(
        "DeepSeek request model=%s thinking=%s effort=%s max_tokens=%s",
        model,
        thinking_mode or "unsupported/omitted",
        effort,
        output_limit,
    )
    try:
        data = request(build_payload(thinking_mode, output_limit))
    except AIServiceError as exc:
        if thinking_mode and _is_thinking_parameter_error(exc):
            _THINKING_UNSUPPORTED.add(cache_key)
            log.warning("当前 DeepSeek 模型不支持 thinking 参数，降级为普通请求：%s", model)
            thinking_mode = None
            data = request(build_payload(None, int(max_tokens)))
        else:
            raise

    content, reasoning, finish, usage = _parse_deepseek_choice(data)

    # 关键修复：content 才是最终回答；reasoning_content 只是思维链，绝不能作为回答返回。
    # 若思考占满输出额度导致 content 为空，则自动关闭思考重试一次，确保用户看到正式输出。
    if not content and thinking_enabled and thinking_mode is not None:
        log.warning(
            "DeepSeek 只返回了思考过程（finish=%s, reasoning_chars=%s），自动关闭思考重试一次",
            finish,
            len(reasoning),
        )
        retry_mode = None if cache_key in _THINKING_UNSUPPORTED else "disabled"
        retry_limit = max(int(max_tokens), 4096)
        try:
            data = request(build_payload(retry_mode, retry_limit))
        except AIServiceError as exc:
            if retry_mode and _is_thinking_parameter_error(exc):
                _THINKING_UNSUPPORTED.add(cache_key)
                data = request(build_payload(None, retry_limit))
            else:
                raise
        content, reasoning, finish, usage = _parse_deepseek_choice(data)

    if not content:
        if reasoning:
            raise AIServiceError(
                "模型只返回了思考过程，没有生成最终回答。请重试，"
                "或在设置中关闭「模型思考」/降低思考强度。",
                status_code=502,
                code="reasoning_only",
            )
        raise AIServiceError(
            f"DeepSeek 返回了空内容（finish_reason={finish}），请稍后重试。",
            code="empty_content",
        )
    if finish in {"length", "max_tokens"}:
        log.warning("DeepSeek 输出达到长度上限，回答可能被截断（finish=%s）", finish)
    if usage:
        details = usage.get("completion_tokens_details") or {}
        log.info(
            "DeepSeek tokens: prompt=%s completion=%s reasoning=%s total=%s",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            details.get("reasoning_tokens"),
            usage.get("total_tokens"),
        )
    return content


def _openai_content(message: Mapping[str, Any]) -> tuple[str, str]:
    raw_content = message.get("content")
    if isinstance(raw_content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in raw_content
            if isinstance(part, Mapping) and part.get("type") in {None, "text", "output_text"}
        ).strip()
    else:
        content = str(raw_content or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()
    return content, reasoning


def _openai_reasoning_effort(config: Mapping[str, Any]) -> str:
    if not _thinking_enabled(config):
        return "none"
    effort = _thinking_effort(config)
    # OpenAI 官方支持 none/minimal/low/medium/high；max 映射到 high 以兼容多数网关。
    return {"low": "low", "medium": "medium", "high": "high", "max": "high"}.get(effort, "medium")


def _openai_chat(
    messages: Sequence[Mapping[str, str]],
    *,
    config: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    """OpenAI Chat Completions 兼容实现；不发送 DeepSeek/Gemini 专属思考字段。"""
    model = str(config["model"])
    url = f"{config['base']}/chat/completions"
    headers = {"Accept": "application/json"}
    if config.get("key"):
        headers["Authorization"] = f"Bearer {config['key']}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "stream": False,
        "max_tokens": max(1, int(max_tokens)),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    cache_key = _thinking_key(config)
    if cache_key not in _THINKING_UNSUPPORTED:
        payload["reasoning_effort"] = _openai_reasoning_effort(config)

    for attempt in range(3):
        try:
            data = _post_json(url, headers=headers, payload=payload)
            break
        except AIServiceError as exc:
            if exc.status_code != 400:
                raise
            message = (exc.message or "").lower()
            changed = False
            if "reasoning" in message and "reasoning_effort" in payload:
                payload.pop("reasoning_effort", None)
                _THINKING_UNSUPPORTED.add(cache_key)
                changed = True
            if "response_format" in message and "response_format" in payload:
                payload.pop("response_format", None)
                changed = True
            if "temperature" in message and "temperature" in payload:
                payload.pop("temperature", None)
                changed = True
            if "max_tokens" in message and "max_tokens" in payload:
                payload.pop("max_tokens", None)
                payload["max_completion_tokens"] = max(1, int(max_tokens))
                changed = True
            if not changed or attempt >= 2:
                raise
            log.warning("OpenAI 兼容接口拒绝可选参数，降级重试：%s", exc.message[:160])
    else:  # pragma: no cover - 循环必然 break 或 raise
        raise AIServiceError("OpenAI 兼容接口请求失败。", code="openai_error")

    choices = data.get("choices") or []
    if not choices:
        raise AIServiceError(
            f"OpenAI 兼容接口没有返回结果：{_api_error_message(data) or '未知原因'}",
            code="empty_choice",
        )
    choice = choices[0] or {}
    content, reasoning = _openai_content(choice.get("message") or {})
    if not content:
        if reasoning:
            raise AIServiceError(
                "模型只返回了思考过程，没有生成最终回答。请重试。",
                status_code=502,
                code="reasoning_only",
            )
        raise AIServiceError(
            f"OpenAI 兼容接口返回了空内容（finish_reason={choice.get('finish_reason')}）。",
            code="empty_content",
        )
    usage = data.get("usage") or {}
    if usage:
        log.info(
            "OpenAI-compatible tokens: prompt=%s completion=%s total=%s",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
    return content


def _gemini_thinking_config(
    config: Mapping[str, Any], *, enabled_override: bool | None = None
) -> tuple[dict[str, Any] | None, int]:
    """构造 Gemini 思考配置。2.5 用 thinkingBudget，3/4 用 thinkingLevel。"""
    if _thinking_key(config) in _THINKING_UNSUPPORTED:
        return None, 0
    model = str(config["model"]).lower()
    enabled = _thinking_enabled(config) if enabled_override is None else enabled_override
    effort = _thinking_effort(config)
    if "gemini-4" in model or "gemini-3" in model:
        if not enabled:
            # Gemini 3/4 不支持完全关闭；low 是各型号都支持的最低档，比 minimal 兼容性更好。
            level = "low"
        else:
            level = {"low": "low", "medium": "medium", "high": "high", "max": "high"}[effort]
        return {"thinkingLevel": level, "includeThoughts": False}, 0
    if not enabled:
        # Gemini 2.5 Pro 不能完全关闭思考，128 是其最小值。
        budget = 128 if "2.5-pro" in model else 0
    else:
        budget = {"low": 1024, "medium": 4096, "high": 12288, "max": 24576}[effort]
    return {"thinkingBudget": budget, "includeThoughts": False}, budget


def _gemini_output_limit(base: int, thinking: Mapping[str, Any] | None, budget: int) -> int:
    if not thinking:
        return max(1, int(base))
    if "thinkingBudget" in thinking:
        if budget <= 0:
            return max(1, int(base), 1024)
        return min(32768, max(int(base), 2048) + max(0, budget) + 1024)
    return max(int(base), 8192)


def _parse_gemini_response(
    data: Mapping[str, Any],
) -> tuple[str, str, str | None, Mapping[str, Any]]:
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise AIServiceError(
            f"Gemini 拒绝了该请求（{feedback.get('blockReason')}），请调整文本。",
            status_code=400,
            code="blocked",
        )
    candidates = data.get("candidates") or []
    if not candidates:
        raise AIServiceError(
            f"Gemini 没有返回结果：{_api_error_message(data) or '未知原因'}",
            code="empty_choice",
        )
    candidate = candidates[0] or {}
    parts = ((candidate.get("content") or {}).get("parts")) or []
    final_parts: list[str] = []
    thought_parts: list[str] = []
    for part in parts:
        text = str(part.get("text") or "").strip()
        if not text:
            continue
        if part.get("thought"):
            thought_parts.append(text)
        else:
            final_parts.append(text)
    return (
        "\n".join(final_parts).strip(),
        "\n".join(thought_parts).strip(),
        candidate.get("finishReason"),
        data.get("usageMetadata") or {},
    )


def _gemini_chat(
    messages: Sequence[Mapping[str, str]],
    *,
    config: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        text = str(message.get("content", ""))
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hallo"}]}]
    model = str(config["model"])
    url = f"{config['base']}/models/{model}:generateContent"
    cache_key = _thinking_key(config)

    def build_payload(
        thinking: Mapping[str, Any] | None, output_limit: int
    ) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max(1, int(output_limit)),
        }
        if thinking:
            generation["thinkingConfig"] = dict(thinking)
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

    def request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return _post_json(
            url,
            headers={"x-goog-api-key": config["key"], "Accept": "application/json"},
            payload=payload,
        )

    thinking, budget = _gemini_thinking_config(config)
    output_limit = _gemini_output_limit(max_tokens, thinking, budget)
    log.info(
        "Gemini request model=%s thinking=%s effort=%s max_output=%s",
        model,
        thinking or "unsupported/omitted",
        _thinking_effort(config),
        output_limit,
    )
    try:
        data = request(build_payload(thinking, output_limit))
    except AIServiceError as exc:
        if thinking and _is_thinking_parameter_error(exc):
            _THINKING_UNSUPPORTED.add(cache_key)
            log.warning("当前 Gemini 模型不支持 thinkingConfig，降级为普通请求：%s", model)
            thinking, budget = None, 0
            data = request(build_payload(None, int(max_tokens)))
        else:
            raise

    content, thoughts, finish, usage = _parse_gemini_response(data)

    # 只返回非 thought 的 part；若只剩思考内容或思考耗尽 maxOutputTokens，自动关闭思考重试。
    if not content and _thinking_enabled(config) and thinking is not None:
        log.warning(
            "Gemini 未生成最终回答（finish=%s, thought_chars=%s），自动关闭思考重试一次",
            finish,
            len(thoughts),
        )
        retry_thinking, retry_budget = _gemini_thinking_config(config, enabled_override=False)
        retry_limit = _gemini_output_limit(max_tokens, retry_thinking, retry_budget)
        try:
            data = request(build_payload(retry_thinking, retry_limit))
        except AIServiceError as exc:
            if retry_thinking and _is_thinking_parameter_error(exc):
                _THINKING_UNSUPPORTED.add(cache_key)
                retry_thinking = None
                data = request(build_payload(None, int(max_tokens)))
            else:
                raise
        content, thoughts, finish, usage = _parse_gemini_response(data)

    if not content:
        if thoughts or finish in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS"}:
            raise AIServiceError(
                "模型只返回了思考过程，没有生成最终回答。请重试，"
                "或在设置中关闭「模型思考」/降低思考强度。",
                status_code=502,
                code="reasoning_only",
            )
        raise AIServiceError(
            f"Gemini 返回了空内容（finishReason={finish}），请稍后重试。",
            code="empty_content",
        )
    if finish in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS"}:
        log.warning("Gemini 输出达到长度上限，回答可能被截断（finish=%s）", finish)
    if usage:
        log.info(
            "Gemini tokens: prompt=%s thoughts=%s total=%s",
            usage.get("promptTokenCount"),
            usage.get("thoughtsTokenCount"),
            usage.get("totalTokenCount"),
        )
    return content


def chat(
    messages: Sequence[Mapping[str, str]],
    *,
    provider: str | None = None,
    temperature: float = 0.5,
    max_tokens: int = 1800,
    json_mode: bool = False,
) -> str:
    """统一的 Chat 入口。"""
    config = _provider_config(provider)
    log.info(
        "AI chat via %s/%s (json=%s, thinking=%s/%s)",
        config["provider"],
        config["model"],
        json_mode,
        _thinking_enabled(config),
        _thinking_effort(config),
    )
    if config["provider"] == "deepseek":
        return _deepseek_chat(
            messages,
            config=config,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    if config["provider"] == "openai":
        return _openai_chat(
            messages,
            config=config,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    return _gemini_chat(
        messages,
        config=config,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )


# --------------------------------------------------------------------------- #
# 流式 Chat
# --------------------------------------------------------------------------- #
def _open_stream(
    url: str, *, headers: Mapping[str, str], payload: Mapping[str, Any]
) -> requests.Response:
    """打开 SSE 流；HTTP 错误在返回前转换为统一异常。"""
    try:
        response = requests.post(
            url,
            headers={
                **headers,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=payload,
            stream=True,
            timeout=_timeout(),
        )
    except requests.Timeout as exc:
        raise AIServiceError(
            "AI 流式请求超时。网络较慢或文本过长，请稍后重试。",
            status_code=504,
            code="timeout",
        ) from exc
    except requests.RequestException as exc:
        raise AIServiceError(
            "无法连接 AI 服务，请检查网络或代理设置。", status_code=502, code="network"
        ) from exc
    log.info("POST(stream) %s -> %s", _mask_url(url), response.status_code)
    if response.status_code >= 400:
        body = response.text
        response.close()
        _raise_http_error(response.status_code, body)
    return response


def _iter_sse_data(response: requests.Response) -> Iterator[str]:
    """逐行读取 SSE 的 data 字段，返回其原始字符串。"""
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        if data:
            yield data


def _stream_deepseek(
    messages: Sequence[Mapping[str, str]],
    *,
    config: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> Iterator[str]:
    model = str(config["model"])
    enabled = _thinking_enabled(config)
    effort = _thinking_effort(config)
    cache_key = _thinking_key(config)
    url = f"{config['base']}/chat/completions"
    thinking_mode: str | None = None
    if cache_key not in _THINKING_UNSUPPORTED:
        thinking_mode = "enabled" if enabled else "disabled"
    param_fallback = False
    reasoning_fallback = False
    reasoning_chars = 0
    finish: str | None = None

    while True:
        if thinking_mode == "enabled":
            output_limit = max(int(max_tokens), 8192)
        elif reasoning_fallback:
            output_limit = max(int(max_tokens), 4096)
        else:
            output_limit = int(max_tokens)
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": True,
            "max_tokens": max(1, output_limit),
        }
        if thinking_mode:
            payload["thinking"] = {"type": thinking_mode}
            if thinking_mode == "enabled":
                payload["reasoning_effort"] = effort
        if thinking_mode != "enabled" and "reasoner" not in model.lower():
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        log.info(
            "DeepSeek stream model=%s thinking=%s effort=%s max_tokens=%s",
            model,
            thinking_mode or "unsupported/omitted",
            effort,
            output_limit,
        )
        try:
            response = _open_stream(
                url,
                headers={
                    "Authorization": f"Bearer {config['key']}",
                    "Accept": "text/event-stream",
                },
                payload=payload,
            )
        except AIServiceError as exc:
            if thinking_mode and not param_fallback and _is_thinking_parameter_error(exc):
                _THINKING_UNSUPPORTED.add(cache_key)
                log.warning("当前 DeepSeek 模型不支持 thinking 参数，流式请求降级：%s", model)
                thinking_mode = None
                param_fallback = True
                continue
            raise

        received: list[str] = []
        try:
            with response:
                for data in _iter_sse_data(response):
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] or {}
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        reasoning_chars += len(str(reasoning))
                    text = delta.get("content") or ""
                    if text:
                        received.append(str(text))
                        yield str(text)
                    if choice.get("finish_reason"):
                        finish = choice.get("finish_reason")
        except requests.RequestException as exc:
            raise AIServiceError(
                "AI 流式连接中断，已生成的文字可能不完整，请重试。",
                status_code=502,
                code="stream_interrupted",
            ) from exc

        if "".join(received).strip():
            if finish in {"length", "max_tokens"}:
                log.warning("DeepSeek 流式输出达到长度上限（finish=%s）", finish)
            return
        # 首轮只有思考内容 / 输出额度耗尽：关闭思考再流一次。
        if thinking_mode == "enabled" and not reasoning_fallback:
            log.warning(
                "DeepSeek 流式只返回了思考内容（finish=%s, reasoning_chars=%s），关闭思考重试",
                finish,
                reasoning_chars,
            )
            thinking_mode = "disabled"
            reasoning_fallback = True
            continue
        break

    if reasoning_chars:
        raise AIServiceError(
            "模型只返回了思考过程，没有生成最终回答。请重试，"
            "或在设置中关闭「模型思考」/降低思考强度。",
            status_code=502,
            code="reasoning_only",
        )
    raise AIServiceError(
        f"DeepSeek 流式返回了空内容（finish_reason={finish}），请稍后重试。",
        code="empty_content",
    )


def _stream_gemini(
    messages: Sequence[Mapping[str, str]],
    *,
    config: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> Iterator[str]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        text = str(message.get("content", ""))
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].append({"text": text})
        else:
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hallo"}]}]
    model = str(config["model"])
    url = f"{config['base']}/models/{model}:streamGenerateContent?alt=sse"
    cache_key = _thinking_key(config)
    thinking, budget = _gemini_thinking_config(config)
    param_fallback = False
    reasoning_fallback = False
    thoughts_chars = 0
    finish: str | None = None

    while True:
        output_limit = _gemini_output_limit(max_tokens, thinking, budget)
        generation: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max(1, output_limit),
        }
        if thinking:
            generation["thinkingConfig"] = dict(thinking)
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        log.info(
            "Gemini stream model=%s thinking=%s effort=%s max_output=%s",
            model,
            thinking or "unsupported/omitted",
            _thinking_effort(config),
            output_limit,
        )
        try:
            response = _open_stream(
                url,
                headers={
                    "x-goog-api-key": config["key"],
                    "Accept": "text/event-stream",
                },
                payload=payload,
            )
        except AIServiceError as exc:
            if thinking and not param_fallback and _is_thinking_parameter_error(exc):
                _THINKING_UNSUPPORTED.add(cache_key)
                log.warning("当前 Gemini 模型不支持 thinkingConfig，流式请求降级：%s", model)
                thinking, budget = None, 0
                param_fallback = True
                continue
            raise

        received: list[str] = []
        try:
            with response:
                for data in _iter_sse_data(response):
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    feedback = chunk.get("promptFeedback") or {}
                    if feedback.get("blockReason"):
                        raise AIServiceError(
                            f"Gemini 拒绝了该请求（{feedback.get('blockReason')}），请调整文本。",
                            status_code=400,
                            code="blocked",
                        )
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    candidate = candidates[0] or {}
                    if candidate.get("finishReason"):
                        finish = candidate.get("finishReason")
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        text = str(part.get("text") or "")
                        if not text:
                            continue
                        if part.get("thought"):
                            thoughts_chars += len(text)
                            continue
                        received.append(text)
                        yield text
        except requests.RequestException as exc:
            raise AIServiceError(
                "AI 流式连接中断，已生成的文字可能不完整，请重试。",
                status_code=502,
                code="stream_interrupted",
            ) from exc

        if "".join(received).strip():
            if finish in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS"}:
                log.warning("Gemini 流式输出达到长度上限（finish=%s）", finish)
            return
        if _thinking_enabled(config) and thinking is not None and not reasoning_fallback:
            log.warning(
                "Gemini 流式未生成最终回答（finish=%s, thought_chars=%s），关闭思考重试",
                finish,
                thoughts_chars,
            )
            thinking, budget = _gemini_thinking_config(config, enabled_override=False)
            reasoning_fallback = True
            continue
        break

    if thoughts_chars:
        raise AIServiceError(
            "模型只返回了思考过程，没有生成最终回答。请重试，"
            "或在设置中关闭「模型思考」/降低思考强度。",
            status_code=502,
            code="reasoning_only",
        )
    raise AIServiceError(
        f"Gemini 流式返回了空内容（finishReason={finish}），请稍后重试。",
        code="empty_content",
    )


def _stream_openai(
    messages: Sequence[Mapping[str, str]],
    *,
    config: Mapping[str, Any],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> Iterator[str]:
    model = str(config["model"])
    url = f"{config['base']}/chat/completions"
    headers = {"Accept": "text/event-stream"}
    if config.get("key"):
        headers["Authorization"] = f"Bearer {config['key']}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "stream": True,
        "max_tokens": max(1, int(max_tokens)),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    cache_key = _thinking_key(config)
    if cache_key not in _THINKING_UNSUPPORTED:
        payload["reasoning_effort"] = _openai_reasoning_effort(config)

    response = None
    for attempt in range(3):
        try:
            response = _open_stream(url, headers=headers, payload=payload)
            break
        except AIServiceError as exc:
            if exc.status_code != 400:
                raise
            message = (exc.message or "").lower()
            changed = False
            if "reasoning" in message and "reasoning_effort" in payload:
                payload.pop("reasoning_effort", None)
                _THINKING_UNSUPPORTED.add(cache_key)
                changed = True
            if "max_tokens" in message and "max_tokens" in payload:
                payload.pop("max_tokens", None)
                payload["max_completion_tokens"] = max(1, int(max_tokens))
                changed = True
            if not changed or attempt >= 2:
                raise
            log.warning("OpenAI 兼容流式接口拒绝可选参数，降级重试：%s", exc.message[:160])
    if response is None:  # pragma: no cover
        raise AIServiceError("OpenAI 兼容流式请求失败。", code="openai_error")

    received: list[str] = []
    finish: str | None = None
    try:
        with response:
            for data in _iter_sse_data(response):
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    continue
                text = delta.get("content") or ""
                if text:
                    received.append(str(text))
                    yield str(text)
                if choice.get("finish_reason"):
                    finish = choice.get("finish_reason")
    except requests.RequestException as exc:
        raise AIServiceError(
            "AI 流式连接中断，已生成的文字可能不完整，请重试。",
            status_code=502,
            code="stream_interrupted",
        ) from exc

    if "".join(received).strip():
        return
    # 有些兼容网关对 stream 支持不完整；没有正文时回退到非流式请求。
    log.warning("OpenAI 兼容接口流式未返回正文，回退到非流式请求：%s", finish)
    yield _openai_chat(
        messages,
        config=config,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )


def stream_chat(
    messages: Sequence[Mapping[str, str]],
    *,
    provider: str | None = None,
    temperature: float = 0.5,
    max_tokens: int = 1800,
    json_mode: bool = False,
) -> Iterator[str]:
    """统一的流式 Chat 入口，逐段 yield 正式回答文本（不含思维链）。"""
    config = _provider_config(provider)
    log.info(
        "AI stream via %s/%s (json=%s, thinking=%s/%s)",
        config["provider"],
        config["model"],
        json_mode,
        _thinking_enabled(config),
        _thinking_effort(config),
    )
    if config["provider"] == "deepseek":
        yield from _stream_deepseek(
            messages,
            config=config,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    elif config["provider"] == "openai":
        yield from _stream_openai(
            messages,
            config=config,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    else:
        yield from _stream_gemini(
            messages,
            config=config,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )



# --------------------------------------------------------------------------- #
# 联网搜索（DeepSeek 搜索接口不可用时自动降级）
# --------------------------------------------------------------------------- #
def web_search(query: str, *, count: int = 5, min_score: float = 1.5) -> dict[str, Any]:
    """尝试调用 DeepSeek 检索工具，返回高密度摘要；失败时返回 unavailable。"""
    query = (query or "").strip()
    if not query:
        return {"available": False, "summaries": [], "error": "空查询", "results": 0}
    try:
        config = _provider_config("deepseek")
    except AIServiceError as exc:
        return {"available": False, "summaries": [], "error": exc.message, "results": 0}
    url = os.environ.get(
        "DEEPSEEK_SEARCH_URL", "https://api.deepseek.com/v1/tools/search"
    ).strip()
    payload = {"query": query, "count": count, "need_content": False}
    try:
        data = _post_json(
            url,
            headers={"Authorization": f"Bearer {config['key']}", "Accept": "application/json"},
            payload=payload,
            timeout=(10, 25),
            retries=0,
        )
    except AIServiceError as exc:
        log.info("联网检索不可用，降级为模型知识：%s", exc.message)
        return {"available": False, "summaries": [], "error": exc.message, "results": 0}

    raw_results: Any = (
        data.get("results")
        or data.get("data")
        or data.get("items")
        or data.get("documents")
        or []
    )
    if isinstance(raw_results, dict):
        raw_results = raw_results.get("results") or []
    if not isinstance(raw_results, list):
        raw_results = []
    scored: list[tuple[float, str]] = []
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        summary = item.get("summary") or item.get("content") or item.get("snippet") or ""
        summary = _truncate(str(summary), 900)
        if not summary:
            continue
        score = _as_float(item.get("rerank_score") or item.get("score") or item.get("relevance"))
        scored.append((score if score is not None else 0.0, summary))
    high = [(score, summary) for score, summary in scored if score >= min_score]
    chosen = high if high else scored[:3]
    summaries = [summary for _, summary in chosen[:5]]
    log.info("联网检索：%s 条原始结果，选用 %s 条", len(raw_results), len(summaries))
    return {
        "available": True,
        "summaries": summaries,
        "results": len(raw_results),
        "error": None,
        "query": query,
    }


# --------------------------------------------------------------------------- #
# 书籍背景
# --------------------------------------------------------------------------- #
def generate_book_background(
    title: str, author: str | None, excerpt: str
) -> dict[str, Any]:
    """生成【全书概要 / 作者生平 / 历史文化背景】。"""
    query = " ".join(part for part in [title, author or "", "Hintergrund Entstehung Interpretation"] if part)
    search = web_search(query)
    summaries = search.get("summaries") or []
    reference = ""
    if summaries:
        joined = "\n\n".join(f"[{index + 1}] {summary}" for index, summary in enumerate(summaries))
        reference = f"""
下面是从网络检索到的参考资料（可能有噪声，请交叉判断，不必逐条引用）：
\"\"\"
{joined}
\"\"\"
"""
    user_prompt = _render_prompt(
        "prompt_background_user",
        {
            "title": title,
            "author": author or "（未知，请根据文本谨慎推断，无法确定时写“不详”）",
            "excerpt": _truncate(excerpt, 2600),
            "reference": reference,
        },
    )
    raw = chat(
        [
            {"role": "system", "content": _prompt_value("prompt_background_system")},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.45,
        max_tokens=2400,
        json_mode=True,
    )
    parsed: dict[str, Any] = {}
    try:
        loaded = _extract_json(raw)
        if isinstance(loaded, dict):
            parsed = loaded
    except ValueError:
        log.warning("全书背景 JSON 解析失败，使用原始文本兜底")

    summary = _first_text(parsed, "book_summary", "summary", "overview")
    author_bio = _first_text(parsed, "author_bio", "author", "authorInfo")
    context = _first_text(parsed, "cultural_context", "context", "historical_context", "background")
    if not any([summary, author_bio, context]):
        summary = _truncate(re.sub(r"\s+", " ", raw), 1200)
    source = "search+model" if search.get("available") and search.get("summaries") else "model_knowledge"
    return {
        "book_summary": summary,
        "author_bio": author_bio,
        "cultural_context": context,
        "source": source,
        "search": search,
    }


def _first_text(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)):
            joined = "\n".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                return joined
    return ""


# --------------------------------------------------------------------------- #
# 章节导读
# --------------------------------------------------------------------------- #
def _chapter_excerpt(text: str, limit: int = 11000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: limit - 2200]
    tail = text[-1800:]
    return f"{head}\n\n[…中间省略…]\n\n{tail}"


def generate_chapter_guide(
    book_title: str,
    chapter_title: str,
    chapter_text: str,
    *,
    language: str = "de",
) -> dict[str, Any]:
    """生成章节概要 + 1–3 个探究问题；JSON 失败时降级提取。"""
    system = _prompt_value("prompt_chapter_system")
    user_prompt = _render_prompt(
        "prompt_chapter_user",
        {
            "book_title": book_title,
            "chapter_title": chapter_title,
            "chapter_text": _chapter_excerpt(chapter_text),
            "language": language,
        },
    )
    raw = chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.55,
        max_tokens=1400,
        json_mode=True,
    )
    summary, questions, degraded = "", [], False
    try:
        parsed = _extract_json(raw)
        if isinstance(parsed, dict):
            summary = _first_text(parsed, "summary", "chapter_summary", "plot")
            raw_questions = parsed.get("questions") or parsed.get("guiding_questions") or []
            if isinstance(raw_questions, str):
                raw_questions = [raw_questions]
            if isinstance(raw_questions, (list, tuple)):
                questions = [str(q).strip() for q in raw_questions if str(q).strip()]
        elif isinstance(parsed, list):
            questions = [str(x).strip() for x in parsed if str(x).strip()][:3]
    except ValueError:
        degraded = True
    if not summary:
        summary = _fallback_summary(raw)
        degraded = True
    if not questions:
        questions = _fallback_questions(chapter_title, 0)
        degraded = True
    questions = [_truncate(q, 180) for q in questions[:3]]
    return {
        "summary": _truncate(summary, 700),
        "questions": questions,
        "degraded": degraded,
    }


def _fallback_summary(raw: str) -> str:
    text = re.sub(r"```.*?```", " ", raw, flags=re.S)
    text = re.sub(r"[#*_>`-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _truncate(text, 320) or "本章导读生成失败，可点击重新生成。"


def _fallback_questions(chapter_title: str, already: int) -> list[str]:
    defaults = [
        f"在本章「{chapter_title}」中，叙述者通过哪些德语词汇或句式营造气氛？",
        "主要人物的言行与内心之间存在哪些矛盾？请找出关键句。",
        "本章的叙述视角如何影响你对事件真相的判断？",
    ]
    return defaults[already:]


# --------------------------------------------------------------------------- #
# 划词讲解 & 多轮追问
# --------------------------------------------------------------------------- #
def _build_selection_prompt(
    template: str, selected_text: str, context_sentence: str
) -> str:
    template = (template or "").strip() or prompts.DEFAULT_SELECTION_PROMPT
    selected_text = selected_text.strip()
    context_sentence = context_sentence.strip()
    if "{selected_text}" not in template:
        template = f"【选中词句】\n{selected_text}\n\n" + template
    if "{context_sentence}" not in template:
        template = template.rstrip() + f"\n\n【上下文】\n{context_sentence}"
    # 单次正则替换，避免选中文本恰好包含占位符字符串时被二次替换。
    return re.sub(
        r"\{(selected_text|context_sentence)\}",
        lambda match: selected_text if match.group(1) == "selected_text" else context_sentence,
        template,
    )


def _selection_messages(
    selected_text: str,
    context_sentence: str,
    *,
    book_title: str = "",
    chapter_title: str = "",
    prompt_template: str | None = None,
) -> list[dict[str, str]]:
    template = prompt_template
    if template is None:
        template = _prompt_value("selection_prompt")
    user_prompt = _build_selection_prompt(template, selected_text, context_sentence)
    system = (
        "你是一位精通德语语法、词汇与德国文化的私教，回答面向中文母语的德语学习者。"
        "请严格依据原文语境，不编造；使用 Markdown 输出。"
    )
    if book_title:
        system += f" 当前阅读：《{book_title}》"
        if chapter_title:
            system += f" · {chapter_title}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]


def explain_selection(
    selected_text: str,
    context_sentence: str,
    *,
    book_title: str = "",
    chapter_title: str = "",
    prompt_template: str | None = None,
    provider: str | None = None,
) -> str:
    """生成划词解析（Markdown）。"""
    raw = chat(
        _selection_messages(
            selected_text,
            context_sentence,
            book_title=book_title,
            chapter_title=chapter_title,
            prompt_template=prompt_template,
        ),
        provider=provider,
        temperature=0.35,
        max_tokens=2200,
    )
    if not raw.strip():
        raise AIServiceError("AI 返回了空的解析内容，请重试。", code="empty_content")
    return raw.strip()


def explain_selection_stream(
    selected_text: str,
    context_sentence: str,
    *,
    book_title: str = "",
    chapter_title: str = "",
    prompt_template: str | None = None,
    provider: str | None = None,
) -> Iterator[str]:
    """流式生成划词解析，逐段返回正式回答。"""
    yield from stream_chat(
        _selection_messages(
            selected_text,
            context_sentence,
            book_title=book_title,
            chapter_title=chapter_title,
            prompt_template=prompt_template,
        ),
        provider=provider,
        temperature=0.35,
        max_tokens=2200,
    )


def _followup_messages(
    *,
    selected_text: str,
    context_sentence: str,
    explanation: str,
    history: Sequence[Mapping[str, str]],
    question: str,
    book_title: str = "",
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": _prompt_value("prompt_followup_system")
            + (f" 当前阅读：《{book_title}》。" if book_title else "")
            + "\n\n"
            + _render_prompt(
                "prompt_followup_context",
                {
                    "selected_text": selected_text,
                    "context_sentence": context_sentence,
                    "explanation": explanation[:4000],
                },
            ),
        }
    ]
    for message in history:
        role = str(message.get("role", ""))
        content = str(message.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question.strip()})
    return messages


def chat_followup(
    *,
    selected_text: str,
    context_sentence: str,
    explanation: str,
    history: Sequence[Mapping[str, str]],
    question: str,
    book_title: str = "",
) -> str:
    """在已有解析的基础上进行多轮追问。"""
    raw = chat(
        _followup_messages(
            selected_text=selected_text,
            context_sentence=context_sentence,
            explanation=explanation,
            history=history,
            question=question,
            book_title=book_title,
        ),
        temperature=0.45,
        max_tokens=1600,
    )
    if not raw.strip():
        raise AIServiceError("AI 返回了空回答，请重试。", code="empty_content")
    return raw.strip()


def chat_followup_stream(
    *,
    selected_text: str,
    context_sentence: str,
    explanation: str,
    history: Sequence[Mapping[str, str]],
    question: str,
    book_title: str = "",
) -> Iterator[str]:
    """流式多轮追问。"""
    yield from stream_chat(
        _followup_messages(
            selected_text=selected_text,
            context_sentence=context_sentence,
            explanation=explanation,
            history=history,
            question=question,
            book_title=book_title,
        ),
        temperature=0.45,
        max_tokens=1600,
    )


# --------------------------------------------------------------------------- #
# 元数据识别
# --------------------------------------------------------------------------- #
def identify_book(filename: str, excerpt: str) -> dict[str, Any]:
    raw = chat(
        [
            {"role": "system", "content": _prompt_value("prompt_identify_system")},
            {
                "role": "user",
                "content": _render_prompt(
                    "prompt_identify_user",
                    {"filename": filename, "excerpt": excerpt[:1600]},
                ),
            },
        ],
        temperature=0.2,
        max_tokens=300,
        json_mode=True,
    )
    try:
        parsed = _extract_json(raw)
    except ValueError:
        return {"title": None, "author": None, "confidence": "low", "reason": "解析失败"}
    if not isinstance(parsed, dict):
        return {"title": None, "author": None, "confidence": "low", "reason": "格式异常"}
    title = _truncate(_first_text(parsed, "title", "book_title"), 160) or None
    author = _truncate(_first_text(parsed, "author", "author_name"), 120) or None
    confidence = str(parsed.get("confidence") or "low").lower()
    return {
        "title": title,
        "author": author,
        "confidence": confidence,
        "reason": _truncate(_first_text(parsed, "reason"), 200),
    }


# --------------------------------------------------------------------------- #
# 状态与连通性测试
# --------------------------------------------------------------------------- #
def provider_status() -> dict[str, Any]:
    active = (db.get_setting("active_model", "deepseek") or "deepseek").lower()
    deepseek_key = _clean_key(db.get_setting("deepseek_key")) or _clean_key(
        os.environ.get("DEEPSEEK_API_KEY")
    )
    gemini_key = _clean_key(db.get_setting("gemini_key")) or _clean_key(
        os.environ.get("GEMINI_API_KEY")
    )
    openai_key = _clean_key(db.get_setting("openai_key")) or _clean_key(
        os.environ.get("OPENAI_API_KEY")
    )
    openai_model = (db.get_setting("openai_model") or os.environ.get("OPENAI_MODEL") or "").strip()
    effort = (db.get_setting("thinking_effort", "medium") or "medium").strip().lower()
    try:
        _provider_config(active)
        ready = True
    except AIServiceError:
        ready = False
    return {
        "active_model": active if active in {"deepseek", "gemini", "openai"} else "deepseek",
        "ready": ready,
        "deepseek_model": db.get_setting("deepseek_model", "deepseek-flash"),
        "gemini_model": db.get_setting("gemini_model", "gemini-3.8-flash"),
        "openai_model": openai_model,
        "openai_base_url": db.get_setting("openai_base_url", "https://api.openai.com/v1"),
        "has_deepseek_key": bool(deepseek_key),
        "has_gemini_key": bool(gemini_key),
        "has_openai_key": bool(openai_key),
        "openai_ready": bool(openai_model),
        "thinking_enabled": (db.get_setting("thinking_enabled", "1") or "1").strip().lower()
        not in {"0", "false", "off", "no"},
        "thinking_effort": effort if effort in THINKING_EFFORTS else "medium",
    }


def provider_ready(provider: str | None = None) -> bool:
    """当前供应商是否具备发起请求的最低配置。"""
    try:
        _provider_config(provider)
        return True
    except AIServiceError:
        return False


def test_connection(provider: str | None = None) -> dict[str, Any]:
    config = _provider_config(provider)
    started = time.monotonic()
    reply = chat(
        [
            {"role": "system", "content": "你是连接测试助手。"},
            {"role": "user", "content": "请只回复：Verbindung OK"},
        ],
        provider=config["provider"],
        temperature=0.0,
        max_tokens=30,
    )
    latency = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "provider": config["provider"],
        "model": config["model"],
        "thinking_enabled": _thinking_enabled(config),
        "thinking_effort": _thinking_effort(config),
        "latency_ms": latency,
        "reply": _truncate(reply, 100),
    }
