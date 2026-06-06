"""核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BilibiliVideoRef:
    """B 站视频引用。"""

    url: str
    video_id: str
    page: int = 1
    source: str = "unknown"


@dataclass(slots=True)
class BilibiliVideoMetadata:
    """视频元数据。"""

    video_id: str
    title: str = ""
    author: str = ""
    description: str = ""
    duration_sec: int | None = None
    total_pages: int = 1
    page: int = 1
    page_title: str = ""
    total_duration_sec: int | None = None
    cover_url: str = ""
    aid: int | None = None
    cid: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VideoAnalysisResult:
    """分析结果聚合。"""

    metadata: BilibiliVideoMetadata
    result_level: str = "minimal"
    subtitle_text: str = ""
    asr_text: str = ""
    card_visual_text: str = ""
    visual_text: str = ""
    summary_text: str = ""
    raw_info_text: str = ""
    raw_info: dict[str, Any] = field(default_factory=dict)
    raw_summary: str = ""
    frame_descriptions: list[str] = field(default_factory=list)
    cache_key: str = ""
    video_path: str = ""
    frames_dir: str = ""
    audio_path: str = ""
    success: bool = False
    error: str = ""
    fallback_reason: str = ""
