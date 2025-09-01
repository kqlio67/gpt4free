import sys, re
from pathlib import Path
from os import path

sys.path.append(str(Path(__file__).parent.parent.parent))

import g4f

g4f.debug.logging = True

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
from ..providers.response import JsonConversation, Reasoning, Usage
from .base_provider import AsyncGeneratorProvider, ProviderModelMixin
from .helper import get_last_user_message
from ..requests import sse_stream, ClientSession


class {name}(AsyncGeneratorProvider, ProviderModelMixin):
    url = "https://example.com"
    label = "Example"
    working = True
    supports_stream = True
    supports_message_history = True
    supports_system_message = True
    
    default_model = ''
    models = ['']
    fallback_models = ['']

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        model_name = cls.get_model(model)
        
        headers = {{
            "authority": "example.com",
            "accept": "application/json",
            "origin": cls.url,
            "referer": f"{{cls.url}}/chat",
        }}
        async with ClientSession(headers=headers) as session:
            prompt = get_last_user_message(messages)
            data = {{
                "prompt": prompt,
                "model": model_name,
                "stream": True
            }}
            async with session.post(f"{{cls.url}}/api/chat", json=data, proxy=proxy) as response:
                response.raise_for_status()
                async for chunk in response.content:
                    if chunk:
                        yield chunk.decode()
""",
    "OpenaiTemplate": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .template import OpenaiTemplate


class {name}(OpenaiTemplate):
    api_base = "https://example.com/v1"
    label = "Example"
    supports_gpt_4o_vision = True
    
    default_model = ''
    models = ['']
    fallback_models = ['']
""",
    "needs_auth": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .base_provider import AsyncGeneratorProvider, ProviderModelMixin


class {name}(AsyncGeneratorProvider, ProviderModelMixin):
    url = "https://example.com"
    label = "Example"
    working = True
    supports_stream = True
    needs_auth = True
    supports_message_history = True
    supports_system_message = True
    
    default_model = ''
    models = ['']
    fallback_models = ['']

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        auth: str = "...",
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        yield "Not implemented"
""",
    "ImageProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .base_provider import AsyncGeneratorProvider
from ..image import ImageResponse


class {name}(AsyncGeneratorProvider):
    url = "https://example.com"
    label = "Example"
    working = True
    supports_stream = False
    
    default_model = ''
    models = ['']

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        yield ImageResponse("https://example.com/image.png", "prompt")
""",
    "AudioProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .base_provider import AsyncGeneratorProvider
from ..providers.response import AudioResponse


class {name}(AsyncGeneratorProvider):
    url = "https://example.com"
    label = "Example"
    working = True
    supports_stream = False
    
    default_model = ''
    models = ['']

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        yield AudioResponse("https://example.com/audio.mp3", "prompt")
""",
    "VideoProvider": """
from __future__ import annotations

from ..typing import AsyncResult, Messages
from .base_provider import AsyncGeneratorProvider
from ..providers.response import VideoResponse


class {name}(AsyncGeneratorProvider):
    url = "https://example.com"
    label = "Example"
    working = True
    supports_stream = False
    
    default_model = ''
    models = ['']

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        proxy: str = None,
        **kwargs
    ) -> AsyncResult:
        yield VideoResponse("https://example.com/video.mp4", "prompt")
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
    response = g4f.ChatCompletion.create(
        model=g4f.models.gpt_4o,
        messages=[{"role": "user", "content": prompt}],
        timeout=300,
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
    response = g4f.ChatCompletion.create(
        model=g4f.models.gpt_4o,
        messages=[{"role": "user", "content": prompt}],
        timeout=300,
    )

    if code := read_code(response):
        with open(provider_path, "w") as file:
            file.write(code)
        print("Updated at:", provider_path)

if not path.isfile(provider_path):
    create_provider(provider_path, name)
else:
    update_provider(provider_path, name)
