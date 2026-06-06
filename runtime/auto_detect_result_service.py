"""自动检测结果装配服务。"""

from __future__ import annotations

from typing import Any

from ..core.models import BilibiliVideoRef, VideoAnalysisResult
from ..core.text_render_service import TextRenderService
from .message_formatter import MessageFormatter


class AutoDetectResultService:
    """负责将视频分析结果装配成自动检测链最终重写文本。"""

    def __init__(self, formatter: MessageFormatter, text_renderer: TextRenderService) -> None:
        self._formatter = formatter
        self._text_renderer = text_renderer

    def build_success_text(self, *, original_text: str, result: VideoAnalysisResult) -> str:
        simplified_text = self._text_renderer.simplify_bilibili_links(original_text, result.metadata.video_id)
        resolved_text = self._resolve_text_by_level(result)
        return self._formatter.build_auto_detect_final_text(
            simplified_text=simplified_text,
            resolved_text=resolved_text,
        )

    def build_fallback_text(
        self,
        *,
        original_text: str,
        video_ref: BilibiliVideoRef,
        result: VideoAnalysisResult | None = None,
    ) -> str:
        metadata = result.metadata if result is not None else None
        resolved_video_id = metadata.video_id if metadata is not None and metadata.video_id else video_ref.video_id
        simplified_text = self._text_renderer.simplify_bilibili_links(original_text, resolved_video_id)

        if result is not None:
            resolved_text = self._resolve_text_by_level(result)
            if resolved_text.strip():
                return self._formatter.build_auto_detect_final_text(
                    simplified_text=simplified_text,
                    resolved_text=resolved_text,
                )

        fallback_text = self._formatter.build_auto_detect_fallback_text(
            metadata.video_id if metadata is not None and metadata.video_id else video_ref.video_id,
            metadata.page if metadata is not None else video_ref.page,
            title=metadata.title if metadata is not None else "",
            author=metadata.author if metadata is not None else "",
        )
        return self._formatter.build_auto_detect_final_text(
            simplified_text=simplified_text,
            resolved_text=fallback_text,
        )

    def resolve_result_level(self, result: VideoAnalysisResult | None) -> str:
        if result is None:
            return "minimal"
        return str(result.result_level or "minimal").strip() or "minimal"

    def describe_result(self, result: VideoAnalysisResult | None) -> dict[str, bool | str]:
        return {
            "result_level": self.resolve_result_level(result),
            "has_metadata": bool(result is not None and self._build_metadata_text(result).strip()),
            "has_raw_info": bool(result is not None and str(result.raw_info_text or "").strip() and self.resolve_result_level(result) == "raw_info"),
            "has_summary": bool(result is not None and str(result.summary_text or "").strip() and self.resolve_result_level(result) == "summary"),
            "fallback_reason": str(result.fallback_reason or "") if result is not None else "",
        }

    def _build_metadata_text(self, result: VideoAnalysisResult) -> str:
        metadata = result.metadata
        if any(
            [
                str(metadata.title or "").strip(),
                str(metadata.author or "").strip(),
                str(metadata.description or "").strip(),
                metadata.duration_sec is not None,
            ]
        ):
            return self._text_renderer.build_basic_info_text(metadata)
        return ""

    def _resolve_text_by_level(self, result: VideoAnalysisResult) -> str:
        level = self.resolve_result_level(result)
        if level == "summary":
            return str(result.summary_text or "").strip()
        if level == "raw_info":
            return str(result.raw_info_text or "").strip()
        if level == "metadata":
            return self._build_metadata_text(result)
        return ""
