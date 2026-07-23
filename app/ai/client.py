import json
import logging
import time

import httpx

from app.config import settings

log = logging.getLogger("ai_client")


class AIClientError(Exception):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.nvidia_api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=30.0,
        read=settings.nvidia_timeout_sec,
        write=30.0,
        pool=30.0,
    )


def _build_payload(
    model: str, system_prompt: str, user_prompt: str, *, stream: bool
) -> dict:
    # NVIDIA NIM DeepSeek V4 models hang on non-streaming requests unless
    # chat_template_kwargs explicitly disables reasoning mode.
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
            delta = choice.get("delta", {})
            if content := delta.get("content"):
                chunks.append(content)
    text = "".join(chunks).strip()
    if not text:
        raise ValueError("empty streaming response")
    return text


def _call_once(
    client: httpx.Client,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    payload = _build_payload(model, system_prompt, user_prompt, stream=True)
    with client.stream(
        "POST",
        settings.nvidia_api_url,
        json=payload,
        headers=_headers(),
    ) as response:
        response.raise_for_status()
        return _parse_stream(response)


def _models_to_try() -> list[str]:
    models = [settings.nvidia_model]
    fallback = settings.nvidia_fallback_model.strip()
    if fallback and fallback not in models:
        models.append(fallback)
    return models


def call_deepseek(system_prompt: str, user_prompt: str, max_retries: int = 2) -> str:
    """Call DeepSeek via the NVIDIA endpoint. Returns raw text content."""
    models = _models_to_try()
    last_error: Exception | None = None

    with httpx.Client(timeout=_timeout()) as client:
        for model in models:
            for attempt in range(max_retries + 1):
                try:
                    log.info(
                        "Calling NVIDIA model %s (attempt %d/%d)",
                        model,
                        attempt + 1,
                        max_retries + 1,
                    )
                    return _call_once(client, model, system_prompt, user_prompt)
                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "DeepSeek call failed for %s (attempt %d): %s",
                        model,
                        attempt + 1,
                        exc,
                    )
                    if attempt < max_retries:
                        time.sleep(2**attempt)

    raise AIClientError(f"DeepSeek call failed after retries: {last_error}")
