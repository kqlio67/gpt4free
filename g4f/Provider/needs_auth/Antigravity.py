"""
Google Antigravity Provider for gpt4free.

Features:
- Access to Gemini 3.7 (Flash High/Med/Low) with native Thinking & reasoning tokens.
- Access to Gemini 3.1 Pro / Flash-Lite and Claude Sonnet 4.6 / Opus 4.6.
- Multi-Account Pooling: pool multiple Google accounts for higher throughput and auto-failover on 429 quota limits.
- Zero-Configuration Token Discovery: automatically discovers tokens from:
  1. ~/.config/agy-proxy/accounts.json (multi-account pool)
  2. ~/.gemini/antigravity-cli/antigravity-oauth-token (Google CLI token)
  3. ~/.gemini/antigravity-ide/antigravity-oauth-token (Google IDE token)
  4. ~/.antigravity/oauth_creds.json (g4f default auth)

--------------------------------------------------------------------------------
Quick Start & Authentication:
--------------------------------------------------------------------------------
1. Authenticate via CLI:
   $ python -m g4f.Provider.needs_auth.Antigravity login

2. Or authenticate via Python:
   >>> import asyncio
   >>> from g4f.Provider import Antigravity
   >>> asyncio.run(Antigravity.login())

3. Check active account pool status:
   $ python -m g4f.Provider.needs_auth.Antigravity status

4. Basic Python Usage:
   >>> import g4f
   >>> from g4f.Provider import Antigravity
   >>> response = g4f.ChatCompletion.create(
   ...     model="gemini-3.7-flash-high",
   ...     provider=Antigravity,
   ...     messages=[{"role": "user", "content": "Hello!"}],
   ...     stream=True,
   ... )
   >>> for chunk in response:
   ...     print(chunk, end="", flush=True)

Note: Compatible with agy-proxy multi-account pool: https://github.com/kqlio67/agy-proxy
"""

import os
import sys
import json
import base64
import time
import secrets
import hashlib
import asyncio
import webbrowser
import threading
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union, Tuple
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from ...typing import AsyncResult, Messages, MediaListType
from ...errors import MissingAuthError, RateLimitError
from ...requests.raise_for_status import raise_for_status
from ...image.copy_images import save_response_media
from ...image import to_bytes, is_data_an_media
from ...providers.response import Usage, ImageResponse, ToolCalls, Reasoning
from ...providers.asyncio import get_running_loop
from ..base_provider import AsyncGeneratorProvider, ProviderModelMixin, AuthFileMixin
from ..helper import get_connector, get_system_prompt, format_media_prompt
from ... import debug


# Unsupported JSON Schema keys for Gemini API
_UNSUPPORTED_SCHEMA_KEYS = {
    "patternProperties", "$schema", "$id", "$defs", "definitions",
    "if", "then", "else", "not", "allOf", "anyOf", "oneOf",
    "default", "examples", "readOnly", "writeOnly",
    "contentEncoding", "contentMediaType", "additionalProperties",
    "enumDescriptions", "$comment",
}


def _sanitize_schema(schema: dict) -> dict:
    """Recursively remove JSON Schema keywords unsupported by the Gemini API."""
    if not isinstance(schema, dict):
        return schema
    result = {}
    for k, v in schema.items():
        if k in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if isinstance(v, dict):
            result[k] = _sanitize_schema(v)
        elif isinstance(v, list):
            result[k] = [_sanitize_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            result[k] = v
    return result


# Candidate token files in priority order (supports agy-proxy pool: https://github.com/kqlio67/agy-proxy)
CANDIDATE_TOKEN_FILES = [
    Path.home() / ".config" / "agy-proxy" / "accounts.json",
    Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
    Path.home() / ".gemini" / "antigravity-ide" / "antigravity-oauth-token",
    Path.home() / ".antigravity" / "oauth_creds.json",
]

_CID = ("1071006060591", "tmhssin2h21lcre235vtolojh4g403ep", "apps.googleusercontent.com")
OAUTH_CLIENT_ID = os.environ.get("ANTIGRAVITY_CLIENT_ID", f"{_CID[0]}-{_CID[1]}.{_CID[2]}")

_SEC = ("GOCSPX", "K58FWR486LdLJ1mLB8sXC4z6qDAf")
OAUTH_CLIENT_SECRET = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", f"{_SEC[0]}-{_SEC[1]}")
ANTIGRAVITY_REDIRECT_URI = "https://antigravity.google/oauth-callback"
ANTIGRAVITY_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "https://www.googleapis.com/auth/aicode",
    "openid",
]

BASE_URLS = [
    "https://daily-cloudcode-pa.googleapis.com/v1internal",
    "https://cloudcode-pa.googleapis.com/v1internal",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal",
]

ANTIGRAVITY_HEADERS = {
    "User-Agent": "antigravity/cli/1.1.20 (aidev_client; os_type=linux; arch=amd64; cl=970154694; auth_method=consumer)",
    "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
    "Client-Metadata": '{"ideType":"IDE_UNSPECIFIED","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}',
}


def generate_pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def encode_oauth_state(verifier: str, project_id: str = "") -> str:
    payload = {"verifier": verifier, "projectId": project_id}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def decode_oauth_state(state: str) -> Dict[str, str]:
    padded = state + "=" * (4 - len(state) % 4) if len(state) % 4 else state
    normalized = padded.replace("-", "+").replace("_", "/")
    try:
        decoded = base64.b64decode(normalized).decode("utf-8")
        parsed = json.loads(decoded)
        return {"verifier": parsed.get("verifier", ""), "projectId": parsed.get("projectId", "")}
    except Exception:
        return {"verifier": "", "projectId": ""}


class AccountSession:
    """Represents a single authenticated Google Account session in the pool."""

    def __init__(self, account_id: str, refresh_token: str, access_token: str = None,
                 expiry_timestamp: float = 0.0, project_id: str = None, email: str = None,
                 is_primary: bool = False, auth_method: str = "consumer"):
        self.account_id = account_id
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.expiry_timestamp = expiry_timestamp
        self.project_id = project_id
        self.email = email
        self.is_primary = is_primary
        self.auth_method = auth_method
        self.total_requests = 0
        self.is_rate_limited = False
        self.rate_limit_reset = 0.0
        self._lock = asyncio.Lock()

    async def get_valid_token(self) -> str:
        async with self._lock:
            now = time.time()
            if self.access_token and self.expiry_timestamp > now + 120:
                return self.access_token

            # Refresh token
            data = {
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post("https://oauth2.googleapis.com/token", data=data) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        raise RuntimeError(f"Token refresh failed for {self.email or self.account_id}: {err_text}")
                    res = await resp.json()
                    self.access_token = res["access_token"]
                    self.expiry_timestamp = now + res.get("expires_in", 3600)
                    return self.access_token

    async def ensure_project_id(self) -> str:
        if self.project_id:
            return self.project_id
        token = await self.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **ANTIGRAVITY_HEADERS,
        }
        async with aiohttp.ClientSession() as session:
            for base_url in BASE_URLS:
                try:
                    url = f"{base_url}:loadCodeAssist"
                    async with session.post(url, headers=headers, json={"metadata": {"ideType": "IDE_UNSPECIFIED"}}, timeout=ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            proj = data.get("cloudaicompanionProject")
                            if isinstance(proj, dict):
                                self.project_id = proj.get("id")
                            elif isinstance(proj, str):
                                self.project_id = proj
                            if self.project_id:
                                return self.project_id
                except Exception:
                    continue
        self.project_id = "default"
        return self.project_id


class MultiAccountManager:
    """Manages multi-account discovery, pool rotation, and failover."""

    def __init__(self):
        self.accounts: List[AccountSession] = []
        self._loaded = False

    def load_accounts(self):
        if self._loaded and self.accounts:
            return
        self.accounts.clear()

        # 1. Try ~/.config/agy-proxy/accounts.json
        pool_file = Path.home() / ".config" / "agy-proxy" / "accounts.json"
        if pool_file.exists():
            try:
                with pool_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                accs = data.get("accounts", [])
                for a in accs:
                    if not a.get("enabled", True):
                        continue
                    self.accounts.append(
                        AccountSession(
                            account_id=a.get("account_id", f"acc_{len(self.accounts)}"),
                            refresh_token=a.get("refresh_token"),
                            access_token=a.get("access_token"),
                            expiry_timestamp=float(a.get("expiry_timestamp", 0.0)),
                            project_id=a.get("project_id"),
                            email=a.get("email"),
                            is_primary=bool(a.get("is_primary", False)),
                            auth_method=a.get("auth_method", "consumer"),
                        )
                    )
            except Exception as e:
                debug.log(f"Failed to read pool accounts: {e}")

        # 2. Check candidate single token files if pool was empty
        if not self.accounts:
            for p in CANDIDATE_TOKEN_FILES:
                if p.exists() and p != pool_file:
                    try:
                        with p.open("r", encoding="utf-8") as f:
                            data = json.load(f)
                        token = data.get("refresh_token") or data.get("access_token")
                        if token:
                            self.accounts.append(
                                AccountSession(
                                    account_id="primary",
                                    refresh_token=data.get("refresh_token", token),
                                    access_token=data.get("access_token"),
                                    expiry_timestamp=float(data.get("expiry_timestamp", 0.0)),
                                    project_id=data.get("project_id"),
                                    email=data.get("email"),
                                    is_primary=True,
                                )
                            )
                            break
                    except Exception as e:
                        debug.log(f"Failed to read token from {p}: {e}")

        # 3. Check environment variable
        if not self.accounts and "ANTIGRAVITY_SERVICE_ACCOUNT" in os.environ:
            try:
                data = json.loads(os.environ["ANTIGRAVITY_SERVICE_ACCOUNT"])
                self.accounts.append(
                    AccountSession(
                        account_id="env_account",
                        refresh_token=data.get("refresh_token"),
                        access_token=data.get("access_token"),
                        project_id=data.get("project_id"),
                        email=data.get("email"),
                        is_primary=True,
                    )
                )
            except Exception as e:
                debug.log(f"Failed to parse ANTIGRAVITY_SERVICE_ACCOUNT: {e}")

        self._loaded = True

    def get_candidate_accounts(self) -> List[AccountSession]:
        self.load_accounts()
        now = time.time()
        healthy = [a for a in self.accounts if not a.is_rate_limited or a.rate_limit_reset <= now]
        # Sort least-used first
        healthy.sort(key=lambda a: (a.total_requests, not a.is_primary))
        return healthy or self.accounts


_account_mgr = MultiAccountManager()


class Antigravity(AsyncGeneratorProvider, ProviderModelMixin, AuthFileMixin):
    """
    Google Antigravity Provider for gpt4free.
    Features: Multi-Account Pooling, Gemini 3.7 Thinking, Claude Sonnet 4.6 & Opus 4.6.
    """

    label = "Google Antigravity"
    url = "https://antigravity.google"
    login_url = "https://antigravity.google"

    default_model = "gemini-3.7-flash-high"
    models = [
        "gemini-3.7-flash-extra-high",
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash-lite-image",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ]

    image_models = [
        "gemini-3.1-flash-lite-image",
    ]

    model_aliases = {
        "gemini-3.7-flash": "gemini-3.7-flash-high",
        "gemini-3.7-flash-xhigh": "gemini-3.7-flash-extra-high",
        "gemini-3.1-pro": "gemini-3.1-pro-high",
        "gemini-image": "gemini-3.1-flash-lite-image",
        "claude-3-7-sonnet": "claude-sonnet-4-6",
        "claude-3-5-sonnet": "claude-sonnet-4-6",
        "claude-3-opus": "claude-opus-4-6-thinking",
        "gpt-4o": "gemini-3.7-flash-high",
        "gpt-4o-mini": "gemini-3.1-flash-lite",
    }

    working = True
    supports_message_history = True
    supports_system_message = True
    supports_native_tools = True
    needs_auth = True
    active_by_default = True

    @classmethod
    def has_credentials(cls) -> bool:
        _account_mgr.load_accounts()
        return len(_account_mgr.accounts) > 0

    @classmethod
    def get_credentials_path(cls) -> Path:
        for p in CANDIDATE_TOKEN_FILES:
            if p.exists():
                return p
        return CANDIDATE_TOKEN_FILES[0]

    @classmethod
    async def login(cls, **kwargs):
        """Interactive login helper for gpt4free."""
        await cls.interactive_login()

    @classmethod
    def get_models(cls, **kwargs) -> List[str]:
        """Fetch available models dynamically from Google CloudCode API if authenticated."""
        if cls.has_credentials():
            try:
                get_running_loop(check_nested=True)
                dynamic_models = asyncio.run(cls._fetch_models())
                if dynamic_models:
                    cls.models = dynamic_models
            except Exception as e:
                debug.log(f"Dynamic model fetch fallback: {e}")
        return cls.models

    @classmethod
    async def _fetch_models(cls) -> List[str]:
        """Dynamically query Google CloudCode API for the latest real-time model list."""
        candidates = _account_mgr.get_candidate_accounts()
        if not candidates:
            return cls.models
        acc = candidates[0]
        try:
            token = await acc.get_valid_token()
            project_id = await acc.ensure_project_id()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **ANTIGRAVITY_HEADERS,
            }
            upstream_base = os.environ.get("CLOUDFLARE_UPSTREAM_URL", BASE_URLS[0]).rstrip("/")
            url = f"{upstream_base}:fetchAvailableModels"
            async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.post(url, headers=headers, json={"project": project_id}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_models = data.get("models", {})
                        fetched = [
                            k for k, v in raw_models.items()
                            if not v.get("isInternal", False) and not k.startswith("tab_")
                        ]
                        if fetched:
                            return fetched
        except Exception as e:
            debug.log(f"Failed to fetch dynamic models from Google API: {e}")
        return cls.models

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        stream: bool = True,
        media: MediaListType = None,
        tools: Optional[list] = None,
        **kwargs,
    ) -> AsyncResult:
        model = cls.model_aliases.get(model, model)
        if model not in cls.models and not any(model.startswith(m) for m in cls.models):
            model = cls.default_model

        candidates = _account_mgr.get_candidate_accounts()
        if not candidates:
            raise MissingAuthError(
                "No Antigravity credentials found. Please authenticate via:\n"
                "  • python -m g4f.Provider.needs_auth.Antigravity login\n"
                "  • agy auth login (or create ~/.config/agy-proxy/accounts.json)"
            )

        # Build contents
        contents = []
        system_parts = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role in ("system", "developer"):
                if isinstance(content, str) and content:
                    system_parts.append({"text": content})
                continue

            gemini_role = "model" if role == "assistant" else "user"
            parts = []

            if isinstance(content, str) and content:
                parts.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append({"text": part.get("text", "")})

            if parts:
                if contents and contents[-1]["role"] == gemini_role:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": gemini_role, "parts": parts})

        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Hello"}]}]

        # Generation config
        thinking_config = {"includeThoughts": True}
        if "-extra-high" in model or kwargs.get("thinking_level") in ("extra_high", "extra-high", "xhigh"):
            thinking_config["thinkingBudget"] = 32768
        elif "-high" in model or kwargs.get("thinking_level") == "high":
            thinking_config["thinkingBudget"] = 16384
        elif "-medium" in model or kwargs.get("thinking_level") == "medium":
            thinking_config["thinkingBudget"] = 8192
        elif "-low" in model or kwargs.get("thinking_level") == "low":
            thinking_config["thinkingBudget"] = 4096

        generation_config = {
            "maxOutputTokens": kwargs.get("max_tokens", 8192),
            "thinkingConfig": thinking_config,
        }
        if "temperature" in kwargs and kwargs["temperature"] is not None:
            generation_config["temperature"] = kwargs["temperature"]
        if "service_tier" in kwargs and kwargs["service_tier"]:
            generation_config["serviceTier"] = kwargs["service_tier"]

        inner_request = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            inner_request["systemInstruction"] = {
                "role": "user",
                "parts": system_parts,
            }

        # Multi-Account Failover Loop
        last_error = None
        for acc in candidates:
            try:
                token = await acc.get_valid_token()
                project_id = await acc.ensure_project_id()

                payload = {
                    "project": project_id,
                    "model": model,
                    "request": inner_request,
                }

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    **ANTIGRAVITY_HEADERS,
                }

                upstream_base = os.environ.get("CLOUDFLARE_UPSTREAM_URL", BASE_URLS[0]).rstrip("/")
                url = f"{upstream_base}:streamGenerateContent?alt=sse"

                timeout = ClientTimeout(total=120)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=headers, json=payload) as resp:
                        if resp.status == 429:
                            acc.is_rate_limited = True
                            acc.rate_limit_reset = time.time() + 300
                            debug.log(f"[{acc.email}] Hit 429 rate limit, rotating to next account...")
                            continue

                        if resp.status != 200:
                            err_body = await resp.text()
                            raise RuntimeError(f"HTTP {resp.status}: {err_body}")

                        acc.total_requests += 1

                        # Stream and parse SSE chunks
                        buffer = ""
                        async for chunk in resp.content.iter_any():
                            if not chunk:
                                continue
                            buffer += chunk.decode("utf-8", errors="replace")
                            lines = buffer.split("\n")
                            buffer = lines.pop()

                            for line in lines:
                                line = line.strip()
                                if line.startswith("data:"):
                                    data_str = line[5:].strip()
                                    if not data_str or data_str == "[DONE]":
                                        continue
                                    try:
                                        chunk_obj = json.loads(data_str)
                                        response_obj = chunk_obj.get("response", chunk_obj)
                                        candidates_list = response_obj.get("candidates", [])
                                        if not candidates_list:
                                            continue
                                        candidate = candidates_list[0]
                                        parts = candidate.get("content", {}).get("parts", [])

                                        for p in parts:
                                            if p.get("thought") is True:
                                                thought_txt = p.get("text", "")
                                                if thought_txt:
                                                    yield Reasoning(thought_txt)
                                            elif "inlineData" in p:
                                                inline = p["inlineData"]
                                                mime = inline.get("mimeType", "image/png")
                                                data_b64 = inline.get("data", "")
                                                prompt_txt = format_media_prompt(messages) if messages else ""
                                                yield ImageResponse([f"data:{mime};base64,{data_b64}"], prompt=prompt_txt)
                                            elif "text" in p:
                                                yield p["text"]
                                    except Exception:
                                        pass
                        return  # Successfully finished generation!
            except Exception as e:
                last_error = e
                debug.log(f"[{acc.email or acc.account_id}] Request failed: {e}")
                continue

        if last_error:
            raise last_error
        raise RuntimeError("All Antigravity accounts in pool failed.")

    @classmethod
    async def interactive_login(cls):
        """Interactive PKCE OAuth login command for adding accounts to pool."""
        print("\n" + "=" * 55)
        print("  Google Antigravity OAuth Login")
        print("=" * 55 + "\n")

        verifier, challenge = generate_pkce_pair()
        state = encode_oauth_state(verifier)

        params = {
            "client_id": OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": ANTIGRAVITY_REDIRECT_URI,
            "scope": " ".join(ANTIGRAVITY_SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        print("1. Opening authorization page in your browser...")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        print(f"   If browser did not open, open this URL:\n   {auth_url}\n")
        print("2. After authorizing, copy and paste the full redirect URL or code below:")
        code_input = input("Authorization Code / URL: ").strip()

        if not code_input:
            print("Aborted: No code entered.")
            return

        if "code=" in code_input:
            parsed = urlparse(code_input)
            code = parse_qs(parsed.query).get("code", [code_input])[0]
        else:
            code = code_input

        print("\nExchanging authorization code for tokens...")
        token_data = {
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": ANTIGRAVITY_REDIRECT_URI,
            "code_verifier": verifier,
        }

        project_id = "default"
        async with aiohttp.ClientSession() as session:
            async with session.post("https://oauth2.googleapis.com/token", data=token_data) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    print(f"❌ Token exchange failed: {err_txt}")
                    return
                tokens = await resp.json()

            # Discover email
            email = "unknown@gmail.com"
            async with session.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            ) as resp:
                if resp.status == 200:
                    uinfo = await resp.json()
                    email = uinfo.get("email", email)

            # Discover Project ID via loadCodeAssist
            headers = {
                "Authorization": f"Bearer {tokens['access_token']}",
                "Content-Type": "application/json",
                **ANTIGRAVITY_HEADERS,
            }
            for base_url in BASE_URLS:
                try:
                    url = f"{base_url}:loadCodeAssist"
                    async with session.post(url, headers=headers, json={"metadata": {"ideType": "IDE_UNSPECIFIED"}}, timeout=ClientTimeout(total=8)) as p_resp:
                        if p_resp.status == 200:
                            p_data = await p_resp.json()
                            proj = p_data.get("cloudaicompanionProject")
                            if isinstance(proj, dict):
                                project_id = proj.get("id", "default")
                            elif isinstance(proj, str):
                                project_id = proj
                            if project_id and project_id != "default":
                                break
                except Exception:
                    continue

        # Save into ~/.config/agy-proxy/accounts.json
        pool_dir = Path.home() / ".config" / "agy-proxy"
        pool_dir.mkdir(parents=True, exist_ok=True)
        pool_file = pool_dir / "accounts.json"

        existing_data = {"version": "1.0", "accounts": []}
        if pool_file.exists():
            try:
                with pool_file.open("r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                pass

        accounts = existing_data.get("accounts", [])
        new_account = {
            "account_id": f"acc_{len(accounts) + 1}",
            "email": email,
            "refresh_token": tokens["refresh_token"],
            "access_token": tokens["access_token"],
            "expiry_timestamp": time.time() + tokens.get("expires_in", 3600),
            "project_id": project_id,
            "is_primary": len(accounts) == 0,
            "enabled": True,
        }
        # Update or append
        updated = False
        for idx, acc in enumerate(accounts):
            if acc.get("email") == email:
                accounts[idx] = new_account
                updated = True
                break
        if not updated:
            accounts.append(new_account)
        existing_data["accounts"] = accounts

        with pool_file.open("w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2)

        # Reload pool in-memory
        _account_mgr._loaded = False
        _account_mgr.load_accounts()

        print(f"\n✅ Successfully authenticated account: {email}")
        print(f"   Project ID: {project_id}")
        print(f"📁 Saved to account pool: {pool_file}\n")


def cli_main():
    """CLI entry point for python -m g4f.Provider.needs_auth.Antigravity."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Google Antigravity Authentication for gpt4free",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")
    subparsers.add_parser("login", help="Log in Google account via OAuth PKCE")
    subparsers.add_parser("status", help="Show active accounts and pool status")

    args = parser.parse_args()
    if args.command == "login":
        asyncio.run(Antigravity.interactive_login())
    elif args.command == "status":
        _account_mgr.load_accounts()
        print("\n" + "=" * 55)
        print("  Google Antigravity Account Pool Status")
        print("=" * 55)
        if _account_mgr.accounts:
            print(f"✓ Found {len(_account_mgr.accounts)} account(s) in pool:\n")
            for acc in _account_mgr.accounts:
                tag = "(Primary)" if acc.is_primary else "(Secondary)"
                print(f"  • {tag} {acc.email or 'Google Account'} (ID: {acc.account_id})")
        else:
            print("✗ No credentials found in pool.")
            print("  Run `python -m g4f.Provider.needs_auth.Antigravity login` to add an account.")
        print()
    else:
        parser.print_help()


if __name__ == "__main__":
    cli_main()
