"""视觉分析服务。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class VisualAnalysisRequest:
    """视觉分析请求。"""

    method: str
    frame_paths: list[str]
    prompt: str


class VisualService:
    """视觉分析编排层。"""

    def build_request(self, *, method: str, frame_paths: list[str], prompt: str) -> VisualAnalysisRequest:
        return VisualAnalysisRequest(method=method, frame_paths=list(frame_paths), prompt=prompt)

    def should_use_visual_analysis(self, *, method: str, video_duration_min: float, max_duration_min: float) -> bool:
        return method != "none" and video_duration_min <= max_duration_min
