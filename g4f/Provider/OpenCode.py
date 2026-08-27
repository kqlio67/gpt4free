from __future__ import annotations

import uuid
from typing import Any
import requests

from ..typing import AsyncResult, Messages
from .template import OpenaiTemplate


class OpenCode(OpenaiTemplate):
    label = "OpenCode Zen"
    url = "https://opencode.ai"
    base_url = "https://opencode.ai/zen/v1"

    working = True
    needs_auth = False
    supports_stream = True
    supports_system_message = True
    supports_message_history = True

    default_model = "hy3-free"
    free_models = [
        "hy3-free",
        "mimo-v2.5-free",
        "nemotron-3.5-lightning-free",
        "nemotron-3-ultra-free",
        "laguna-s-2.1-free",
    ]
    fallback_models = free_models
    models = free_models
    model_aliases = {
        "hy3": "hy3-free",
        "mimo-v2.5": "mimo-v2.5-free",
        "nemotron-3.5": "nemotron-3.5-lightning-free",
        "nemotron-3-ultra": "nemotron-3-ultra-free",
        "laguna-2.1": "laguna-s-2.1-free",
    }

    @classmethod
    def is_provider_api_key(cls, api_key: str | None) -> bool:
        """Check if the given key is a real user API key (not public/empty/internal)."""
        return bool(
            api_key
            and api_key != "public"
            and not api_key.startswith("g4f_")
            and not api_key.startswith("gfs_")
        )

    @classmethod
    def get_models(cls, api_key: str = None, timeout: int = 5, **kwargs: Any) -> list[str]:
        """
        Dynamically fetch models:
        - If paid user API key provided -> fetches all 63+ models from OpenCode Zen API.
        - If no API key / public mode -> returns only verified working free models.
        """
        if not cls.is_provider_api_key(api_key):
            return cls.free_models

        try:
            headers = {
                "User-Agent": "opencode/1.18.23 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
                "Authorization": f"Bearer {api_key}",
            }
            response = requests.get(f"{cls.base_url}/models", headers=headers, timeout=timeout)
            if response.ok:
                data = response.json().get("data", [])
                loaded_models = [m.get("id") for m in data if m.get("id")]
                if loaded_models:
                    return loaded_models
        except Exception:
            pass
        return cls.free_models

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        stream: bool = True,
        api_key: str = None,
        headers: dict = None,
        **kwargs: Any,
    ) -> AsyncResult:
        if not cls.is_provider_api_key(api_key):
            api_key = "public"

        opencode_headers = {
            "User-Agent": "opencode/1.18.23 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
            "x-opencode-client": "cli",
            "x-opencode-project": "global",
            "x-opencode-session": f"ses_{uuid.uuid4().hex[:24]}",
            "x-opencode-request": f"msg_{uuid.uuid4().hex[:24]}",
            **(headers or {}),
        }
        async for chunk in super().create_async_generator(
            model=model,
            messages=messages,
            stream=stream,
            api_key=api_key,
            headers=opencode_headers,
            **kwargs,
        ):
            yield chunk
