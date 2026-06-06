"""OpenAI 兼容 ASR 实现。"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass

import aiohttp

from .base import BaseAsrProvider


@dataclass(slots=True)
class OpenAICompatibleAsrProvider(BaseAsrProvider):
    base_url: str
    api_key: str
    model: str
    timeout_sec: int
    max_retries: int = 2
    retry_interval_sec: float = 5.0
    language: str = "zh"
    prompt: str = ""

    async def transcribe(self, audio_path: str) -> str:
        if not self.base_url or not self.api_key:
            return ""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        retries = max(0, int(self.max_retries))
        retry_interval = max(0.0, float(self.retry_interval_sec))

        for attempt in range(retries + 1):
            try:
                form = aiohttp.FormData()
                form.add_field("model", self.model)
                form.add_field("language", self.language)
                if self.prompt:
                    form.add_field("prompt", self.prompt)
                with open(audio_path, "rb") as audio_file:
                    form.add_field("file", audio_file, filename="audio.wav", content_type="audio/wav")
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_sec)) as session:
                        async with session.post(f"{self.base_url.rstrip('/')}/audio/transcriptions", data=form, headers=headers) as response:
                            if response.status != 200:
                                if attempt < retries:
                                    await asyncio.sleep(retry_interval)
                                    continue
                                return ""
                            data = await response.json()
                            return str(data.get("text", "") or "").strip()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if attempt < retries:
                    await asyncio.sleep(retry_interval)
                    continue
                return ""
        return ""
