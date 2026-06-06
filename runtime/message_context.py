"""消息上下文适配层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class IncomingMessageContext:
    """统一描述当前入站消息的稳定字段。"""

    raw_message: Any
    processed_plain_text: str = ""
    plain_text: str = ""
    session_id: str = ""
    stream_id: str = ""
    message_id: str = ""
    platform: str = ""
    user_id: str = ""
    group_id: str = ""
    is_group_message: bool = False
    is_private_message: bool = False
    raw_segments: list[dict[str, Any]] = field(default_factory=list)
    card_visual_hashes: list[str] = field(default_factory=list)

    @property
    def effective_stream_id(self) -> str:
        """返回宿主真实聊天流主键。"""

        return self.session_id or self.stream_id


class MessageContextBuilder:
    """把宿主传入的 message 规范化为插件可稳定消费的上下文对象。"""

    @staticmethod
    def build(message: Any, *, stream_id: str = "") -> IncomingMessageContext:
        if isinstance(message, dict):
            message_info = message.get("message_info") or {}
            user_info = message_info.get("user_info") if isinstance(message_info, dict) else {}
            group_info = message_info.get("group_info") if isinstance(message_info, dict) else None
            raw_segments = message.get("raw_message") or []
            normalized_segments = [segment for segment in raw_segments if isinstance(segment, dict)]
            card_visual_hashes = MessageContextBuilder._extract_card_visual_hashes(normalized_segments)
            session_id = str(message.get("session_id", "") or "")
            fallback_stream_id = str(message.get("stream_id", "") or stream_id or "")
            return IncomingMessageContext(
                raw_message=message,
                processed_plain_text=str(message.get("processed_plain_text", "") or ""),
                plain_text=str(message.get("plain_text", "") or ""),
                session_id=session_id,
                stream_id=fallback_stream_id,
                message_id=str(message.get("message_id", "") or ""),
                platform=str(message.get("platform", "") or ""),
                user_id=str((user_info or {}).get("user_id", "") or ""),
                group_id=str((group_info or {}).get("group_id", "") or ""),
                is_group_message=group_info is not None,
                is_private_message=group_info is None,
                raw_segments=normalized_segments,
                card_visual_hashes=card_visual_hashes,
            )

        raw_segments = []
        message_segments = getattr(message, "message_segments", None)
        if isinstance(message_segments, list):
            for segment in message_segments:
                if isinstance(segment, dict):
                    raw_segments.append(segment)
                elif hasattr(segment, "model_dump"):
                    try:
                        dumped = segment.model_dump(mode="python")
                        if isinstance(dumped, dict):
                            raw_segments.append(dumped)
                    except Exception:
                        continue

        processed_plain_text = message.processed_plain_text if hasattr(message, "processed_plain_text") else None
        plain_text = message.plain_text if hasattr(message, "plain_text") else ""
        session_id = message.session_id if hasattr(message, "session_id") else ""
        fallback_stream_id = message.stream_id if hasattr(message, "stream_id") else stream_id
        message_info = message.message_info if hasattr(message, "message_info") else None
        user_info = message_info.user_info if message_info is not None and hasattr(message_info, "user_info") else None
        group_info = message_info.group_info if message_info is not None and hasattr(message_info, "group_info") else None
        card_visual_hashes = MessageContextBuilder._extract_card_visual_hashes(raw_segments)
        return IncomingMessageContext(
            raw_message=message,
            processed_plain_text=str(processed_plain_text or ""),
            plain_text=str(plain_text or ""),
            session_id=str(session_id or ""),
            stream_id=str(fallback_stream_id or ""),
            message_id=str(message.message_id if hasattr(message, "message_id") else ""),
            platform=str(message.platform if hasattr(message, "platform") else ""),
            user_id=str(user_info.user_id if user_info is not None and hasattr(user_info, "user_id") else ""),
            group_id=str(group_info.group_id if group_info is not None and hasattr(group_info, "group_id") else ""),
            is_group_message=group_info is not None,
            is_private_message=group_info is None,
            raw_segments=raw_segments,
            card_visual_hashes=card_visual_hashes,
        )

    @staticmethod
    def _extract_card_visual_hashes(segments: list[dict[str, Any]]) -> list[str]:
        hashes: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type", "") or "").strip().lower()
            if segment_type not in {"image", "emoji"}:
                continue
            raw_hash = str(segment.get("hash", "") or "").strip()
            if raw_hash and raw_hash not in hashes:
                hashes.append(raw_hash)
        return hashes
