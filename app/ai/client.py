import json
import logging

import httpx

from app.config import settings

log = logging.getLogger("ai_client")


class AIClientError(Exception):
    pass


def call_deepseek(system_prompt: str, user_prompt: str, max_retries: int = 2) -> str:
    """Call DeepSeek V4 Pro via the NVIDIA endpoint. Returns raw text content."""
    payload = {
        "model": settings.nvidia_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 2048,
        "chat_template_kwargs": {"thinking": False},
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.nvidia_api_key}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                settings.nvidia_api_url, json=payload, headers=headers, timeout=120.0
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            last_error = exc
            log.warning("DeepSeek call failed (attempt %d): %s", attempt + 1, exc)

    raise AIClientError(f"DeepSeek call failed after retries: {last_error}")
