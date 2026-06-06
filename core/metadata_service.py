"""视频元数据服务。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .bilibili_api import BilibiliAPI
from .models import BilibiliVideoMetadata, BilibiliVideoRef


class MetadataService:
    """负责识别链接并构造元数据。"""

    def locate(self, text: str) -> BilibiliVideoRef | None:
        extracted = BilibiliAPI.extract_video_id(str(text or ""))
        if not extracted:
            return None
        video_type, video_id, page = extracted
        return BilibiliVideoRef(url=str(text or ""), video_id=video_id, page=page, source=video_type)

    def locate_from_message(self, message: Any) -> BilibiliVideoRef | None:
        if message is None:
            return None
        if isinstance(message, dict):
            raw_text = str(message.get("plain_text", "") or message.get("raw_message", ""))
            return self.locate(raw_text)
        plain_text = message.plain_text
        if isinstance(plain_text, str) and plain_text.strip():
            located = self.locate(plain_text)
            if located is not None:
                return located
        return self.locate(str(message))

    async def build_metadata(self, video_ref: BilibiliVideoRef) -> BilibiliVideoMetadata:
        """构造元数据骨架。"""

        return BilibiliVideoMetadata(
            video_id=video_ref.video_id,
            page=video_ref.page,
            raw={"source": video_ref.source, "url": video_ref.url},
        )

    @staticmethod
    def to_cache_payload(metadata: BilibiliVideoMetadata, **extra: Any) -> dict[str, Any]:
        payload = asdict(metadata)
        payload.update(extra)
        return payload
