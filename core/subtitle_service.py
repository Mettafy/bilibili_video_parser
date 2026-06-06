"""字幕服务。"""

from __future__ import annotations

from typing import Any


class SubtitleService:
    """字幕提取与清洗。"""

    def normalize(self, subtitle_payload: Any) -> str:
        if subtitle_payload is None:
            return ""
        if isinstance(subtitle_payload, str):
            return self._cleanup(subtitle_payload)
        if isinstance(subtitle_payload, list):
            lines: list[str] = []
            for item in subtitle_payload:
                if isinstance(item, dict):
                    content = str(item.get("content", "") or item.get("text", "")).strip()
                    if content:
                        lines.append(content)
            return self._cleanup("\n".join(lines))
        if isinstance(subtitle_payload, dict):
            data = subtitle_payload.get("body") or subtitle_payload.get("content") or subtitle_payload.get("subtitle") or subtitle_payload
            return self.normalize(data)
        return self._cleanup(str(subtitle_payload))

    def build_subtitle_text(self, subtitle_payload: Any) -> str:
        normalized = self.normalize(subtitle_payload)
        if not normalized:
            return ""
        return f"字幕内容：\n{normalized}"

    @staticmethod
    def _cleanup(text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        return "\n".join(lines)
