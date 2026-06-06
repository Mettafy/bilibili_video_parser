"""视频分析器。"""

from __future__ import annotations

import re

from typing import Any, Optional

from .analyzers.host_vlm import HostVLMAnalyzer


class VideoAnalyzer:
    """统一的宿主视觉分析器。"""

    def __init__(self, *, host_llm_adapter: Any = None, vlm_config: Optional[dict[str, Any]] = None) -> None:
        self.host_llm_adapter = host_llm_adapter
        self.vlm_config = vlm_config or {}
        self._initialized = False
        self._init_attempted = False

    def set_config(self, vlm_config: dict[str, Any]) -> None:
        self.vlm_config = vlm_config
        self._initialized = False
        self._init_attempted = False

    def _ensure_initialized(self) -> bool:
        if self._initialized:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True
        try:
            if self.host_llm_adapter is not None:
                self._initialized = True
                return True
        except Exception:
            return False
        return False

    def is_initialized(self) -> bool:
        return self._ensure_initialized()

    async def analyze_frame(self, frame_path: str, custom_prompt: str = "") -> Optional[str]:
        if not self._ensure_initialized():
            return "未识别"
        if self.host_llm_adapter is not None:
            analyzer = HostVLMAnalyzer(
                self.host_llm_adapter,
                str(self.vlm_config.get("host_task_name", "vlm") or "vlm"),
                float(self.vlm_config.get("temperature", 0.2) or 0.2),
                int(self.vlm_config.get("max_tokens", 1024) or 1024),
                str(self.vlm_config.get("frame_prompt", "") or ""),
            )
            result = await analyzer.analyze_frame(frame_path, custom_prompt)
            return result.strip() if result else "未识别"
        return "未识别"

    def clean_summary(self, summary: str) -> str:
        patterns_to_remove = [
            r"【?改写说明】?[：:].+",
            r"【?说明】?[：:].+",
            r"【?注】?[：:].+",
            r"\*\*.+\*\*",
            r"^\s*[-•]\s*",
        ]
        lines = summary.split("\n")
        cleaned_lines: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            should_skip = False
            for pattern in patterns_to_remove:
                if re.search(pattern, line, re.IGNORECASE):
                    should_skip = True
                    break
            if not should_skip:
                cleaned_lines.append(line)
        result = " ".join(cleaned_lines)
        if not result.strip() and summary.strip():
            return summary.strip().split("\n")[0]
        return result
