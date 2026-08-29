from __future__ import annotations

import json
import time
import asyncio
import requests
from typing import Any, Optional

from ..requests.cdp import CDPSession
from ..requests import StreamSession
from .. import debug
from .template import OpenaiTemplate


SITEKEY = "0x4AAAAAADlBNBTRb73O02Vo"


async def get_turnstile_token_async(model: str = "zai-org/GLM-5.3-Flash", timeout: int = 30) -> tuple[str, str, dict]:
    """Launch CDP session on deepinfra.com, render Turnstile with action infer_chat, and get verified token."""
    session = CDPSession(headless=False)
    await session.start()

    try:
        url = f"https://deepinfra.com/{model}"
        debug.log(f"[DeepInfra] Navigating to {url}...")
        await session.navigate(url)

        # Wait until window.turnstile is ready
        for _ in range(12):
            has_ts = await session.evaluate_js("typeof window.turnstile !== 'undefined'")
            if has_ts:
                break
            await asyncio.sleep(0.5)

        # Render Turnstile widget with action 'infer_chat'
        js_render = f"""
        (() => {{
            if (!document.getElementById("g4f-di-ts-box")) {{
                const box = document.createElement("div");
                box.id = "g4f-di-ts-box";
                box.style.width = "300px";
                box.style.height = "65px";
                box.style.position = "fixed";
                box.style.top = "10px";
                box.style.right = "10px";
                box.style.zIndex = "9999999";
                document.body.appendChild(box);
            }}

            window.g4fDiToken = null;
            if (window.turnstile) {{
                window.turnstile.render("#g4f-di-ts-box", {{
                    sitekey: "{SITEKEY}",
                    action: "infer_chat",
                    callback: function(token) {{
                        window.g4fDiToken = token;
                    }}
                }});
            }}
        }})()
        """
        await session.evaluate_js(js_render)

        start_time = time.time()
        while time.time() - start_time < timeout:
            await asyncio.sleep(1)
            await session.bypass_turnstile()

            token = await session.evaluate_js("window.g4fDiToken")
            if token:
                ua = await session.get_user_agent()
                cookies = await session.get_cookies()
                debug.log("[DeepInfra] Successfully obtained Turnstile token!")
                return token, ua, cookies

        raise RuntimeError("Timed out waiting for DeepInfra Turnstile solve.")
    finally:
        await session.close()


class DeepInfra(OpenaiTemplate):
    url = "https://deepinfra.com"
    login_url = "https://deepinfra.com/dash/api_keys"
    base_url = "https://api.deepinfra.com/v1/openai"

    working = True
    active_by_default = True

    default_model = "zai-org/GLM-5.3-Flash"

    _cached_token: Optional[str] = None
    _cached_ua: Optional[str] = None
    _cached_cookies: Optional[dict] = None
    _cached_time: float = 0
    _lock = asyncio.Lock()

    @classmethod
    async def get_quota(cls, **kwargs):
        return {}

    @classmethod
    def get_models(cls, **kwargs):
        if not cls.models:
            url = "https://api.deepinfra.com/models/featured"
            response = requests.get(url, timeout=kwargs.get("timeout", 15))
            models = response.json()

            cls.models = {
                model["model_name"]: {"id": model["model_name"], **model}
                for model in models
                if model.get("type") == "text-generation"
                or model.get("reported_type") == "text-to-image"
            }
            cls.image_models = [
                model["model_name"]
                for model in models
                if model.get("reported_type") == "text-to-image"
            ]
            if cls.live == 0 and cls.models:
                cls.live += 1

        return cls.models

    @classmethod
    async def create_async_generator(
        cls, model: str, messages: Messages, api_key: str = None, headers: dict = None, **kwargs: Any
    ) -> AsyncResult:
        if not api_key or not cls.is_provider_api_key(api_key):
            api_key = None
            async with cls._lock:
                # Token valid for ~2 minutes
                if not cls._cached_token or (time.time() - cls._cached_time > 100):
                    token, ua, cookies = await get_turnstile_token_async(model)
                    cls._cached_token = token
                    cls._cached_ua = ua
                    cls._cached_cookies = cookies
                    cls._cached_time = time.time()

            if headers is None:
                headers = {}
            headers["X-DeepInfra-Source"] = "web-page"
            headers["X-DeepInfra-Turnstile"] = cls._cached_token
            if cls._cached_ua:
                headers["User-Agent"] = cls._cached_ua
            if cls._cached_cookies:
                headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cls._cached_cookies.items()])

        for attempt in range(2):
            try:
                async for chunk in super().create_async_generator(
                    model, messages, api_key=api_key, headers=headers, **kwargs
                ):
                    yield chunk
                return
            except Exception as e:
                # If token expired or rejected, refresh once
                if attempt == 0 and ("403" in str(e) or "Captcha" in str(e)):
                    debug.log("[DeepInfra] Captcha rejected, refreshing token...")
                    async with cls._lock:
                        token, ua, cookies = await get_turnstile_token_async(model)
                        cls._cached_token = token
                        cls._cached_ua = ua
                        cls._cached_cookies = cookies
                        cls._cached_time = time.time()
                    headers["X-DeepInfra-Turnstile"] = token
                    if ua:
                        headers["User-Agent"] = ua
                    if cookies:
                        headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies.items()])
                    continue
                raise e

    @classmethod
    def get_headers(
        cls, stream: bool, api_key: str = None, headers: dict = None
    ) -> dict:
        if not api_key or not cls.is_provider_api_key(api_key):
            if headers is None:
                headers = {}
            headers["X-DeepInfra-Source"] = "web-page"
            headers["Origin"] = "https://deepinfra.com"
            headers["Referer"] = "https://deepinfra.com/"
            api_key = None
        return super().get_headers(stream, api_key, headers)
