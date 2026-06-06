"""NapCat 消息补查与 B站卡片解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json

from ..core.bilibili_api import BilibiliAPI
from .message_context import IncomingMessageContext


@dataclass(slots=True)
class ResolvedBilibiliTarget:
    """从文本、消息段或 NapCat 详情中提取出的 B站目标。"""

    video_type: str
    video_id: str
    page: int
    source_text: str
    source_kind: str


class NapCatBilibiliResolver:
    """基于当前消息和适配器 API 解析 B站目标。"""

    _CARD_PREFIXES = ("[小程序]", "[json", "[xml]", "[share]")

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def resolve(self, message_context: IncomingMessageContext) -> ResolvedBilibiliTarget | None:
        if self._should_prefer_message_detail(message_context):
            detail_match = await self._resolve_from_message_detail(message_context)
            if detail_match is not None:
                self._ctx.logger.info(
                    "B站目标解析成功：来源=adapter.get_msg, source_kind=%s, video_id=%s",
                    detail_match.source_kind,
                    detail_match.video_id,
                )
                return detail_match

        direct = self._resolve_from_text(message_context.processed_plain_text, source_kind="processed_plain_text")
        if direct is not None:
            return direct

        fallback = self._resolve_from_text(message_context.plain_text, source_kind="plain_text")
        if fallback is not None:
            return fallback

        segment_match = self._resolve_from_segments(message_context.raw_segments)
        if segment_match is not None:
            self._ctx.logger.info(
                "B站目标解析成功：来源=raw_message, source_kind=%s, video_id=%s",
                segment_match.source_kind,
                segment_match.video_id,
            )
            return segment_match

        if self._should_prefer_message_detail(message_context):
            return None

        detail_match = await self._resolve_from_message_detail(message_context)
        if detail_match is not None:
            self._ctx.logger.info(
                "B站目标解析成功：来源=adapter.get_msg, source_kind=%s, video_id=%s",
                detail_match.source_kind,
                detail_match.video_id,
            )
            return detail_match
        return None

    @staticmethod
    def _resolve_from_text(text: str, *, source_kind: str) -> ResolvedBilibiliTarget | None:
        extracted = BilibiliAPI.extract_video_id(str(text or ""))
        if not extracted:
            return None
        video_type, video_id, page = extracted
        return ResolvedBilibiliTarget(
            video_type=video_type,
            video_id=video_id,
            page=page,
            source_text=str(text or ""),
            source_kind=source_kind,
        )

    def _resolve_from_segments(self, segments: list[dict[str, Any]]) -> ResolvedBilibiliTarget | None:
        for segment in segments:
            resolved = self._resolve_from_segment(segment)
            if resolved is not None:
                return resolved
        return None

    def _resolve_from_segment(self, segment: dict[str, Any]) -> ResolvedBilibiliTarget | None:
        if not isinstance(segment, dict):
            return None

        direct = self._resolve_from_nested_value(segment.get("data"), source_kind="raw_segment.data")
        if direct is not None:
            return direct

        segment_type = str(segment.get("type", "") or "").strip().lower()
        if segment_type == "text":
            return self._resolve_from_text(str(segment.get("data", "") or ""), source_kind="raw_segment.text")

        if segment_type == "dict":
            return self._resolve_from_nested_value(segment.get("data"), source_kind="raw_segment.dict")

        return None

    async def _resolve_from_message_detail(self, message_context: IncomingMessageContext) -> ResolvedBilibiliTarget | None:
        if not message_context.message_id:
            return None

        self._ctx.logger.info(
            "NapCat get_msg 开始: message_id=%s, session_id=%s",
            message_context.message_id,
            message_context.effective_stream_id,
        )
        detail = await self._ctx.api.call(
            "adapter.napcat.message.get_msg",
            version="1",
            message_id=message_context.message_id,
        )
        if not isinstance(detail, dict):
            self._ctx.logger.warning(
                "NapCat get_msg 返回为空或非字典: message_id=%s, detail_type=%s",
                message_context.message_id,
                type(detail).__name__,
            )
            return None

        self._ctx.logger.info("NapCat get_msg 返回成功: message_id=%s", message_context.message_id)
        if not self._match_message_scope(detail, message_context):
            self._ctx.logger.warning(
                "NapCat 消息详情归属校验失败: message_id=%s, user_id=%s, group_id=%s",
                message_context.message_id,
                message_context.user_id,
                message_context.group_id,
            )
            return None

        detail_text = self._collect_text_candidates_from_detail(detail)
        resolved = self._resolve_from_text(detail_text, source_kind="adapter.get_msg.text")
        if resolved is not None:
            return resolved
        self._ctx.logger.info("NapCat get_msg 文本字段未命中: message_id=%s", message_context.message_id)

        detail_segments = detail.get("message") or detail.get("raw_message") or []
        if isinstance(detail_segments, list):
            segment_match = self._resolve_from_segments([segment for segment in detail_segments if isinstance(segment, dict)])
            if segment_match is not None:
                segment_match.source_kind = f"adapter.get_msg.{segment_match.source_kind}"
                self._ctx.logger.info(
                    "NapCat get_msg 结构化字段命中: message_id=%s, source_kind=%s, video_id=%s",
                    message_context.message_id,
                    segment_match.source_kind,
                    segment_match.video_id,
                )
                return segment_match

        nested_match = self._resolve_from_nested_value(detail, source_kind="adapter.get_msg.detail")
        if nested_match is not None:
            self._ctx.logger.info(
                "NapCat get_msg 深层结构命中: message_id=%s, source_kind=%s, video_id=%s",
                message_context.message_id,
                nested_match.source_kind,
                nested_match.video_id,
            )
            return nested_match

        self._ctx.logger.info("NapCat get_msg 最终解析失败: message_id=%s", message_context.message_id)
        return None

    def _resolve_from_nested_value(self, value: Any, *, source_kind: str) -> ResolvedBilibiliTarget | None:
        extracted = self._extract_candidate_texts(value)
        for index, text in enumerate(extracted):
            resolved = self._resolve_from_text(text, source_kind=f"{source_kind}.{index}")
            if resolved is not None:
                return resolved
        return None

    def _extract_candidate_texts(self, value: Any) -> list[str]:
        candidates: list[str] = []
        self._collect_candidate_texts(value, candidates, seen=set())
        deduped: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in deduped:
                continue
            deduped.append(normalized)
        return deduped

    def _collect_candidate_texts(self, value: Any, candidates: list[str], *, seen: set[int]) -> None:
        if value is None:
            return

        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                candidates.append(normalized)
                parsed_json = self._try_parse_json_text(normalized)
                if parsed_json is not None:
                    self._collect_candidate_texts(parsed_json, candidates, seen=seen)
            return

        object_id = id(value)
        if object_id in seen:
            return
        seen.add(object_id)

        if isinstance(value, dict):
            prioritized_keys = (
                "source_url",
                "url",
                "jumpUrl",
                "jump_url",
                "qqdocurl",
                "title",
                "desc",
                "prompt",
                "content",
                "text",
                "data",
                "meta",
                "detail_1",
                "news",
                "miniapp",
                "subtitle",
            )
            for key in prioritized_keys:
                if key in value:
                    self._collect_candidate_texts(value.get(key), candidates, seen=seen)
            for key, nested_value in value.items():
                if key in prioritized_keys:
                    continue
                self._collect_candidate_texts(nested_value, candidates, seen=seen)
            return

        if isinstance(value, list):
            for item in value:
                self._collect_candidate_texts(item, candidates, seen=seen)

    @staticmethod
    def _try_parse_json_text(text: str) -> Any | None:
        if not text or text[0] not in "[{":
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _match_message_scope(detail: dict[str, Any], message_context: IncomingMessageContext) -> bool:
        detail_user_id = str(detail.get("user_id", "") or "")
        detail_group_id = str(detail.get("group_id", "") or "")
        if message_context.group_id:
            return detail_group_id == message_context.group_id and detail_user_id == message_context.user_id
        return not detail_group_id and detail_user_id == message_context.user_id

    def _collect_text_candidates_from_detail(self, detail: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("raw_message", "message", "alt_message"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
        return "\n".join(parts)

    def _should_prefer_message_detail(self, message_context: IncomingMessageContext) -> bool:
        processed_plain_text = message_context.processed_plain_text.strip()
        if not processed_plain_text:
            return False
        if any(processed_plain_text.startswith(prefix) for prefix in self._CARD_PREFIXES):
            return True
        if "哔哩哔哩：" in processed_plain_text and "http" not in processed_plain_text.lower() and "bv" not in processed_plain_text.lower():
            return True
        return False
