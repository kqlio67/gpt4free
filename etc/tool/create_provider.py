import sys, re
from pathlib import Path
from os import path

sys.path.append(str(Path(__file__).parent.parent.parent))

import g4f

g4f.debug.logging = True

# Provider creation settings
TIMEOUT = 600  # Timeout in seconds
MODEL_NAME = "gpt-oss-120b"  # Model name as string
FALLBACK_MODEL = g4f.models.gpt_oss_120b  # Fallback model from g4f.models

def read_code(text):
    if match := re.search(r"```(python|py|)\n(?P<code>[\S\s]+?)\n```", text):
        return match.group("code")
    return text

def input_command():
    print("Enter/Paste the cURL command. Ctrl-D or Ctrl-Z ( windows ) to save it.")
    contents = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        contents.append(line)
    return "\n".join(contents)

templates = {
    "AsyncGeneratorProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from ..providers.base_provider import AsyncGeneratorProvider
from ..providers.helper import get_last_user_message
from ..requests import StreamSession, sse_stream


class {name}(AsyncGeneratorProvider):
    url = "https://example.com"
    working = True
    supports_stream = True
    
    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        headers = {{
            "authority": "example.com",
            "accept": "application/json",
            "origin": cls.url,
            "referer": f"{{cls.url}}/",
        }}
        async with StreamSession(headers=headers, proxy=proxy, impersonate="chrome110") as session:
            prompt = get_last_user_message(messages)
            data = {{
                "prompt": prompt,
                "model": model,
                "stream": True
            }}
            async with session.post(f"{{cls.url}}/api/chat", json=data) as response:
                response.raise_for_status()
                async for chunk in sse_stream(response):
                    if chunk:
                        yield chunk
""",
    "AsyncAuthedProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from ..providers.base_provider import AsyncAuthedProvider
from ..providers.helper import get_last_user_message
from ..requests import StreamSession, sse_stream
from ..providers.response import AuthResult, JsonConversation


class {name}(AsyncAuthedProvider):
    url = "https://example.com"
    working = True
    
    @classmethod
    async def on_auth_async(cls, proxy: str = None, **kwargs) -> AsyncIterator:
        async with StreamSession(proxy=proxy, impersonate="chrome110") as session:
            # Do auth stuff...
            yield AuthResult(api_key="...")

    @classmethod
    async def create_authed(
        cls,
        model: str,
        messages: Messages,
        auth_result: AuthResult,
        proxy: str = None,
        conversation: JsonConversation = None,
        **kwargs
    ) -> AsyncResult:
        headers = {{
            "Authorization": f"Bearer {{auth_result.api_key}}",
        }}
        async with StreamSession(headers=headers, proxy=proxy, impersonate="chrome110") as session:
            prompt = get_last_user_message(messages)
            data = {{
                "prompt": prompt,
                "model": model,
                "stream": True
            }}
            async with session.post(f"{{cls.url}}/api/chat", json=data) as response:
                response.raise_for_status()
                async for chunk in sse_stream(response):
                    if chunk:
                        yield chunk
""",
    "OpenaiTemplate": """
from __future__ import annotations

from ..providers.base_provider import OpenaiProvider


class {name}(OpenaiProvider):
    url = "https://example.com"
    api_base = "https://example.com/v1"
    
    default_model = ''
    models = ['']
""",
    "ImageProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .base_provider import AsyncGeneratorProvider
from ..image import ImageResponse
from ..providers.helper import get_last_user_message


class {name}(AsyncGeneratorProvider):
    url = "https://example.com"
    working = True
    
    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        prompt = get_last_user_message(messages)
        yield ImageResponse("https://example.com/image.png", prompt)
""",
    "AudioProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .base_provider import AsyncGeneratorProvider
from ..providers.response import AudioResponse
from ..providers.helper import get_last_user_message


class {name}(AsyncGeneratorProvider):
    url = "https://example.com"
    working = True
    
    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        prompt = get_last_user_message(messages)
        yield AudioResponse("https://example.com/audio.mp3", prompt)
"""
}

def get_template():
    print("Select a template for the provider:")
    for i, template_name in enumerate(templates.keys(), 1):
        print(f"{i}. {template_name}")
    
    while True:
        try:
            choice = int(input(f"Enter a number (1-{len(templates)}): "))
            if 1 <= choice <= len(templates):
                return list(templates.values())[choice-1]
        except ValueError:
            pass
        print("Invalid choice. Please try again.")

name = input("Name: ")
provider_path = f"g4f/Provider/{name}.py"

example = get_template()

def create_provider(provider_path: str, name: str):
    command = input_command()

    prompt = f"""
Create a provider from a cURL command. The command is:
```bash
{command}
```
A example for a provider:
```python
{example}
```
The name for the provider class:
{name}
Replace "hello" with `format_prompt(messages)`.
And replace "gpt-3.5-turbo" with `model`.
"""

    print("Create code...")
    # Try to use model as string first, fallback to g4f.models if it fails
    try:
        response = g4f.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            timeout=TIMEOUT,
        )
    except:
        response = g4f.ChatCompletion.create(
            model=FALLBACK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=TIMEOUT,
        )

    if code := read_code(response):
        with open(provider_path, "w") as file:
            file.write(code)
        print("Saved at:", provider_path)
        with open("g4f/Provider/__init__.py", "a") as file:
            file.write(f"\nfrom .{name} import {name}")

def update_provider(provider_path: str, name: str):
    with open(provider_path, "r") as file:
        old_code = file.read()
    
    command = input_command()
    
    prompt = f"""
Update the provider from a cURL command. The command is:
```bash
{command}
```
The provider to update:
```python
{old_code}
```
The name for the provider class:
{name}
Replace "hello" with `format_prompt(messages)`.
And replace "gpt-3.5-turbo" with `model`.
"""

    print("Update code...")
    # Try to use model as string first, fallback to g4f.models if it fails
    try:
        response = g4f.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            timeout=TIMEOUT,
        )
    except:
        response = g4f.ChatCompletion.create(
            model=FALLBACK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=TIMEOUT,
        )

    if code := read_code(response):
        with open(provider_path, "w") as file:
            file.write(code)
        print("Updated at:", provider_path)

if not path.isfile(provider_path):
    create_provider(provider_path, name)
else:
    update_provider(provider_path, name)
