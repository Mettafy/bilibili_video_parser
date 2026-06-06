"""指令结果装配服务。"""

from __future__ import annotations

from ..core.models import VideoAnalysisResult
from ..core.summary_service import SummaryService


class CommandResultService:
    """负责将视频分析结果装配成指令链直接发送文本。"""

    def __init__(self, summary_service: SummaryService) -> None:
        self._summary_service = summary_service

    def build_result_text(self, result: VideoAnalysisResult) -> str:
        result_level = str(result.result_level or "minimal").strip() or "minimal"

        if result_level == "summary" and result.raw_summary:
            return self._build_summary_result_text(result)

        if result_level == "raw_info" and result.raw_info:
            return self._build_raw_info_result_text(result)

        if result_level == "metadata":
            return self._build_metadata_result_text(result)

        return self._build_minimal_result_text(result)

    def describe_output(self, result: VideoAnalysisResult) -> dict[str, bool | str]:
        output_level = str(result.result_level or "minimal").strip() or "minimal"
        return {
            "output_level": output_level,
            "summary_used": output_level == "summary" and bool(result.raw_summary),
            "raw_info_used": output_level == "raw_info" and bool(result.raw_info),
            "metadata_used": output_level == "metadata",
            "summary_failed": output_level != "summary",
        }

    def _build_summary_result_text(self, result: VideoAnalysisResult) -> str:
        return self._summary_service.build_video_info_text(result.metadata, result.raw_summary)

    def _build_raw_info_result_text(self, result: VideoAnalysisResult) -> str:
        base_info_text = self._summary_service.build_basic_info_text(result.metadata, include_status_note=False)
        raw_detail_text = self._summary_service.build_raw_detail_text(result.raw_info)
        return f"{base_info_text}\n\n{raw_detail_text}\n\n视频总结失败，已回退到原始信息。".strip()

    def _build_metadata_result_text(self, result: VideoAnalysisResult) -> str:
        base_info_text = self._summary_service.build_basic_info_text(result.metadata, include_status_note=False)
        return f"{base_info_text}\n\n视频总结失败，详细内容暂时无法解析。".strip()

    @staticmethod
    def _build_minimal_result_text(result: VideoAnalysisResult) -> str:
        video_id = str(result.metadata.video_id or "").strip()
        if video_id:
            return f"视频解析失败：仅识别到视频 ID {video_id}，详细内容暂时无法解析。"
        return "视频解析失败，详细内容暂时无法解析。"
