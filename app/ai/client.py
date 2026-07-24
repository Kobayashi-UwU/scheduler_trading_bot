import json
import logging
import time

import httpx

from app.config import settings

log = logging.getLogger("ai_client")


class AIClientError(Exception):
    pass


def _headers(*, stream: bool) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.nvidia_api_key}",
        "Content-Type": "application/json",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers



def _build_payload(
    model: str, system_prompt: str, user_prompt: str, *, stream: bool
) -> dict:
    # NVIDIA NIM DeepSeek V4 models need explicit non-thinking kwargs.
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": settings.nvidia_max_tokens,
        "stream": stream,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "thinking": False,
        },
    }


def _extract_text_from_choice(choice: dict) -> str | None:
    delta = choice.get("delta") or {}
    message = choice.get("message") or {}
    for part in (
        delta.get("content"),
        message.get("content"),
        choice.get("text"),
    ):
        if isinstance(part, str) and part:
            return part
    return None


def _parse_stream(response: httpx.Response) -> str:
    chunks: list[str] = []
    for line in response.iter_lines():
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in event.get("choices", []):
            if text := _extract_text_from_choice(choice):
                chunks.append(text)
    text = "".join(chunks).strip()
    if not text:
        raise ValueError("empty streaming response")
    return text


def _parse_non_stream(data: dict) -> str:
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected completion payload: {data}") from exc
    text = (text or "").strip()
    if not text:
        raise ValueError("empty non-streaming response")
    return text


def _timeout_for_model(model: str) -> httpx.Timeout:
    read = 60.0 if model.endswith("-pro") else settings.nvidia_timeout_sec
    return httpx.Timeout(connect=30.0, read=read, write=30.0, pool=30.0)


def _call_once(
    client: httpx.Client,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    timeout: httpx.Timeout,
) -> str:
    # Prefer non-streaming for flash-tier models; they respond quickly and
    # avoid NVIDIA SSE quirks. Use streaming for slower pro-tier models.
    use_stream = model.endswith("-pro")
    if use_stream:
        payload = _build_payload(model, system_prompt, user_prompt, stream=True)
        with client.stream(
            "POST",
            settings.nvidia_api_url,
            json=payload,
            headers=_headers(stream=True),
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            try:
                return _parse_stream(response)
            except ValueError:
                log.warning("streaming returned no content for %s, retrying non-stream", model)

    payload = _build_payload(model, system_prompt, user_prompt, stream=False)
    response = client.post(
        settings.nvidia_api_url,
        json=payload,
        headers=_headers(stream=False),
        timeout=timeout,
    )
    response.raise_for_status()
    return _parse_non_stream(response.json())


def _models_to_try() -> list[str]:
    candidates = [
        settings.nvidia_model,
        settings.nvidia_fallback_model,
        "deepseek-ai/deepseek-v4-flash",
    ]
    ordered: list[str] = []
    for model in candidates:
        model = model.strip()
        if model and model not in ordered:
            ordered.append(model)
    # Flash responds reliably on NVIDIA free tier; pro often hangs indefinitely.
    ordered.sort(key=lambda m: m.endswith("-pro"))
    return ordered


def _is_retryable(exc: Exception) -> bool:
    """Transient failures (timeouts, network errors, rate limits, empty bodies) are
    worth retrying — the NVIDIA free tier is flaky but usually recovers. A 4xx other
    than 429 means the request itself is broken (bad key, bad payload); retrying that
    forever would just spin a thread without ever succeeding."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, ValueError):
        return True
    return False


def call_deepseek(system_prompt: str, user_prompt: str) -> str:
    """Call DeepSeek via the NVIDIA endpoint. Returns raw text content.

    Keeps retrying transient failures (timeouts, 5xx, rate limits, empty responses)
    indefinitely with capped exponential backoff, cycling through the configured
    models each pass — a flaky free-tier API call should never silently give up on a
    cycle. Only a non-retryable error (e.g. invalid API key, malformed request) raises
    immediately.
    """
    models = _models_to_try()
    attempt = 0

    with httpx.Client() as client:
        while True:
            for model in models:
                attempt += 1
                timeout = _timeout_for_model(model)
                try:
                    log.info("Calling NVIDIA model %s (attempt %d)", model, attempt)
                    return _call_once(
                        client, model, system_prompt, user_prompt, timeout=timeout
                    )
                except Exception as exc:
                    if not _is_retryable(exc):
                        raise AIClientError(
                            f"DeepSeek call failed (non-retryable): {exc}"
                        ) from exc
                    backoff = min(2 ** min(attempt, 6), settings.nvidia_retry_backoff_cap_sec)
                    log.warning(
                        "DeepSeek call failed for %s (attempt %d): %s — retrying in %.0fs",
                        model,
                        attempt,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
