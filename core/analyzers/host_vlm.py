"""宿主 VLM 适配。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


class HostVLMAnalyzer:
    def __init__(
        self,
        host_adapter: Any,
        task_name: str,
        temperature: float,
        max_tokens: int,
        prompt: str = "",
    ) -> None:
        self._host_adapter = host_adapter
        self._task_name = task_name
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompt = prompt

    async def analyze_frame(self, frame_path: str, prompt: str = "") -> str:
        image_base64 = base64.b64encode(Path(frame_path).read_bytes()).decode("utf-8")
        merged_prompt = prompt or self._prompt or "请用一句中文描述这张视频截图的画面要点，少于25字。仅描述画面中实际出现的内容，不要推测或编造。若无法判断，请回答'未识别'。"
        content = [
            {"type": "text", "text": merged_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
        ]
        result = await self._host_adapter.generate_text(
            prompt=[{"role": "user", "content": content}],
            model=self._task_name,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        if isinstance(result, dict):
            return str(result.get("response", "") or result.get("content", "") or "未识别").strip() or "未识别"
        return "未识别"
