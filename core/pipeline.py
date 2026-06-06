"""统一处理流水线。"""

from __future__ import annotations

import asyncio
import logging
import math
import os

from dataclasses import dataclass
from typing import Any

from ..runtime.llm_host_adapter import LLMHostAdapter
from .asr_service import AsrService
from .bilibili_api import BilibiliAPI
from .cache_manager import CacheManager
from .metadata_service import MetadataService
from .models import BilibiliVideoMetadata, BilibiliVideoRef, VideoAnalysisResult
from .summary_service import SummaryService
from .safe_delete import cleanup_temp_files
from .subtitle_service import SubtitleService
from .text_render_service import TextRenderService
from .video_analyzer import VideoAnalyzer
from .video_parser import VideoParser
from .visual_service import VisualService


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineConfig:
    auto_detect_enable_summary: bool
    summary_enabled: bool
    summary_fallback_to_raw_info: bool
    summary_custom_prompt: str
    summary_max_chars: int
    visual_method: str
    cache_enabled: bool
    cleanup_on_success: bool
    sessdata: str = ""
    max_duration_min: float = 60.0
    max_size_mb: int = 300
    visual_max_duration_min: float = 10.0
    max_frames: int = 5
    lock_even_frames: bool = True
    frame_interval_sec: int = 10
    host_task_name: str = "vlm"
    host_temperature: float = 0.2
    host_max_tokens: int = 1024
    summary_task_name: str = "replyer"
    summary_temperature: float = 0.4
    summary_max_tokens: int = 1024
    asr_config: dict[str, Any] | None = None
    video_api_timeout_sec: int = 15
    short_url_timeout_sec: int = 15
    subtitle_timeout_sec: int = 20
    download_url_timeout_sec: int = 20
    retry_max_attempts: int = 3
    retry_interval_sec: float = 2.0
    download_timeout_sec: int = 300
    ffmpeg_probe_timeout_sec: int = 20
    ffmpeg_extract_audio_timeout_sec: int = 120
    ffmpeg_extract_frames_timeout_sec: int = 180
    media_pipeline_timeout_sec: int = 360
    temp_file_max_age_min: int = 60
    parallel_frame_analysis: bool = False
    parallel_frame_analysis_limit: int = 1
    visual_prompt: str = ""
    command_force_visual: bool = False
    command_force_asr: bool = False
    cache_summary: bool = True
    cache_raw_info: bool = True
    cache_visual_result: bool = True
    cache_subtitle: bool = True
    cache_asr: bool = True
    asr_max_audio_duration_min: float = 30.0
    card_visual_text: str = ""
    cache_fingerprint: str = ""


@dataclass(slots=True)
class PipelineDependencies:
    metadata_service: MetadataService
    video_parser: VideoParser
    video_analyzer: VideoAnalyzer
    subtitle_service: SubtitleService
    visual_service: VisualService
    summary_service: SummaryService
    text_render_service: TextRenderService
    asr_service: AsrService
    host_llm_adapter: LLMHostAdapter | None
    cache_manager: CacheManager


class VideoPipeline:
    def __init__(self, dependencies: PipelineDependencies) -> None:
        self._deps = dependencies

    async def run(self, video_ref: BilibiliVideoRef, config: PipelineConfig) -> VideoAnalysisResult:
        try:
            return await asyncio.wait_for(self._run_staged(video_ref, config), timeout=max(1, int(config.media_pipeline_timeout_sec)))
        except asyncio.TimeoutError:
            return await self._build_failure_result(video_ref, config, error="媒体处理超时", fallback_reason="pipeline_timeout")
        except Exception as exc:
            logger.exception("视频处理流水线执行失败: video_id=%s, page=%s", video_ref.video_id, video_ref.page)
            return await self._build_failure_result(video_ref, config, error=str(exc) or "媒体处理失败", fallback_reason="pipeline_exception")

    async def _run_staged(self, video_ref: BilibiliVideoRef, config: PipelineConfig) -> VideoAnalysisResult:
        prepared_result = await self.prepare_metadata_result(video_ref, config)
        return await self.enrich_result(prepared_result, config)

    async def prepare_metadata_result(self, video_ref: BilibiliVideoRef, config: PipelineConfig) -> VideoAnalysisResult:
        metadata = await self._build_metadata(video_ref, config)
        cache_key = self._deps.cache_manager.build_cache_key(metadata.video_id, metadata.page, config.cache_fingerprint)

        result = VideoAnalysisResult(metadata=metadata, cache_key=cache_key)
        result.result_level = "metadata"
        result.raw_info_text = self._deps.summary_service.build_basic_info_text(metadata)
        result.success = True
        return result

    async def enrich_result(self, result: VideoAnalysisResult, config: PipelineConfig) -> VideoAnalysisResult:
        if result.result_level in {"summary", "raw_info"} and result.success:
            return result

        metadata = result.metadata
        cache_key = result.cache_key or self._deps.cache_manager.build_cache_key(metadata.video_id, metadata.page, config.cache_fingerprint)
        result.cache_key = cache_key
        try:
            cached = self._deps.cache_manager.get_cache(cache_key) if config.cache_enabled else None
            if cached:
                cached, cleaned = self._deps.summary_service.sanitize_cached_summary(cached)
                if cleaned and cached is not None:
                    self._deps.cache_manager.save_cache(cache_key, cached)
                if cached:
                    logger.info(
                        "视频分析命中缓存: video_id=%s, page=%s, cache_key=%s, cache_fingerprint=%s, has_subtitle=%s, has_asr=%s, has_summary=%s",
                        metadata.video_id,
                        metadata.page,
                        cache_key,
                        config.cache_fingerprint,
                        bool((cached.get("raw_info") or {}).get("subtitle_text")) if isinstance(cached, dict) else False,
                        bool((cached.get("raw_info") or {}).get("asr_text")) if isinstance(cached, dict) else False,
                        bool(cached.get("summary")) if isinstance(cached, dict) else False,
                    )
                    return self._from_cache(metadata, cache_key, cached, card_visual_text=result.card_visual_text)

            if metadata.duration_sec is not None:
                video_minutes = metadata.duration_sec // 60
                if video_minutes >= int(config.max_duration_min) + 1:
                    return self._finalize_failure_result(
                        result,
                        error=f"视频时长超过限制（>{int(config.max_duration_min)}分钟）",
                        fallback_reason="duration_limit_exceeded",
                    )

            result.subtitle_text = await self._fetch_subtitle(metadata, config)
            if result.subtitle_text:
                logger.info(
                    "字幕获取成功: video_id=%s, page=%s, subtitle_length=%s",
                    metadata.video_id,
                    metadata.page,
                    len(result.subtitle_text),
                )
            else:
                logger.info("字幕获取失败: video_id=%s, page=%s", metadata.video_id, metadata.page)
            need_visual_analysis = self._need_visual_analysis(metadata, config)
            enable_asr = bool(config.asr_config and config.asr_config.get("enabled"))
            if config.command_force_visual:
                need_visual_analysis = config.visual_method != "none"
            if config.command_force_asr:
                enable_asr = True
            if metadata.duration_sec and enable_asr:
                audio_minutes = metadata.duration_sec / 60.0
                if audio_minutes > max(0.0, float(config.asr_max_audio_duration_min)):
                    logger.info(
                        "ASR 识别跳过: video_id=%s, page=%s, audio_minutes=%.2f, limit=%.2f",
                        metadata.video_id,
                        metadata.page,
                        audio_minutes,
                        float(config.asr_max_audio_duration_min),
                    )
                    enable_asr = False

            if need_visual_analysis or enable_asr:
                download_info = await BilibiliAPI.get_video_download_url(
                    metadata.video_id,
                    page=metadata.page,
                    max_attempts=config.retry_max_attempts,
                    retry_interval=config.retry_interval_sec,
                    timeout_sec=config.download_url_timeout_sec,
                )
                if download_info:
                    result.video_path = await BilibiliAPI.download_video(
                        download_info["url"],
                        max_size_mb=config.max_size_mb,
                        timeout_sec=config.download_timeout_sec,
                        max_attempts=config.retry_max_attempts,
                        retry_interval=config.retry_interval_sec,
                    ) or ""

            analyzer = None
            extracted_frame_paths: list[str] = []
            if need_visual_analysis and result.video_path:
                effective_visual_method = config.visual_method
                if effective_visual_method == "host":
                    extracted_frame_paths = await self._extract_frame_paths(result.video_path, metadata, config)
                    if extracted_frame_paths:
                        result.frames_dir = self._resolve_frames_dir(extracted_frame_paths)
                    analyzer = self._build_frame_analyzer(effective_visual_method, config)
                    result.frame_descriptions = await self._deps.summary_service.build_frame_descriptions(
                        extracted_frame_paths,
                        analyzer,
                        effective_visual_method,
                        enable_parallel=config.parallel_frame_analysis,
                        parallel_limit=config.parallel_frame_analysis_limit,
                        prompt=config.visual_prompt,
                    )
                    result.visual_text = "\n".join(result.frame_descriptions)

            if enable_asr and result.video_path:
                result.audio_path = await self._deps.video_parser.extract_audio(result.video_path) or ""
                if result.audio_path:
                    logger.info(
                        "ASR 开始识别: video_id=%s, page=%s, audio_path=%s",
                        metadata.video_id,
                        metadata.page,
                        result.audio_path,
                    )
                    result.asr_text = await self._deps.asr_service.transcribe(result.audio_path)
                    if result.asr_text:
                        logger.info(
                            "ASR 识别成功: video_id=%s, page=%s, asr_length=%s",
                            metadata.video_id,
                            metadata.page,
                            len(result.asr_text),
                        )
                    else:
                        logger.info("ASR 识别失败: video_id=%s, page=%s", metadata.video_id, metadata.page)
                else:
                    logger.warning(
                        "ASR 音频提取失败: video_id=%s, page=%s",
                        metadata.video_id,
                        metadata.page,
                    )
            elif not enable_asr:
                logger.info("ASR 识别跳过: video_id=%s, page=%s, reason=disabled_or_limited", metadata.video_id, metadata.page)

            result.card_visual_text = str(config.card_visual_text or "").strip()
            logger.info(
                "卡片视觉描述结果: video_id=%s, page=%s, card_visual_available=%s, card_visual_length=%s",
                metadata.video_id,
                metadata.page,
                bool(result.card_visual_text),
                len(result.card_visual_text),
            )

            has_visual = bool(result.frame_descriptions) or bool(result.visual_text)
            has_text = bool(result.subtitle_text) or bool(result.asr_text)

            raw_info = {
                "subtitle_text": result.subtitle_text if config.cache_subtitle else "",
                "asr_text": result.asr_text if config.cache_asr else "",
                "frame_descriptions": result.frame_descriptions if config.cache_visual_result else [],
                "visual_analysis": result.visual_text if config.cache_visual_result else "",
                "visual_method": config.visual_method if has_visual else "none",
            }
            result.raw_info = self._build_runtime_raw_info(raw_info, result.card_visual_text)

            if not has_visual and not has_text:
                result.raw_info_text = self._deps.summary_service.build_basic_info_text(metadata)
                logger.info(
                    "视频分析无字幕且无 ASR/视觉，降级为基础信息: video_id=%s, page=%s",
                    metadata.video_id,
                    metadata.page,
                )
                result.result_level = "metadata"
                result.success = True
                return result

            should_generate_summary = config.summary_enabled and config.auto_detect_enable_summary
            if not should_generate_summary:
                result.raw_info_text = self._deps.summary_service.build_raw_info_text(metadata, result.raw_info)
                logger.info(
                    "视频分析跳过总结阶段: video_id=%s, page=%s, subtitle_used=%s, asr_used=%s, visual_used=%s",
                    metadata.video_id,
                    metadata.page,
                    bool(result.subtitle_text),
                    bool(result.asr_text),
                    has_visual,
                )
                if config.cache_enabled and config.cache_raw_info:
                    self._save_cache(cache_key, metadata, raw_info, None, config.cache_fingerprint)
                result.result_level = "raw_info"
                result.success = True
                return result

            frame_paths_for_summary = extracted_frame_paths

            summary_result = await self._deps.summary_service.generate_summary(
                metadata=metadata,
                frame_paths=frame_paths_for_summary,
                frame_descriptions=result.frame_descriptions,
                card_visual_text=result.card_visual_text,
                subtitle_text=result.subtitle_text,
                asr_text=result.asr_text,
                text_content=self._merge_text_content(result.subtitle_text, result.asr_text),
                visual_method=config.visual_method if has_visual else "none",
                analyzer=analyzer if need_visual_analysis and result.video_path else None,
                enable_parallel=config.parallel_frame_analysis,
                parallel_limit=config.parallel_frame_analysis_limit,
                frame_prompt=config.visual_prompt,
                summary_task_name=config.summary_task_name,
                summary_temperature=config.summary_temperature,
                summary_max_tokens=config.summary_max_tokens,
                summary_max_chars=config.summary_max_chars,
                custom_prompt=config.summary_custom_prompt,
            )
            if summary_result.success and summary_result.raw_summary:
                result.raw_summary = summary_result.raw_summary
                if summary_result.frame_descriptions:
                    result.frame_descriptions = summary_result.frame_descriptions
                    raw_info["frame_descriptions"] = result.frame_descriptions if config.cache_visual_result else []
                result.summary_text = self._deps.summary_service.build_video_info_text(metadata, summary_result.raw_summary)
                logger.info(
                    "视频总结生成成功: video_id=%s, page=%s, subtitle_used=%s, asr_used=%s, card_visual_used=%s, visual_used=%s, summary_length=%s",
                    metadata.video_id,
                    metadata.page,
                    bool(result.subtitle_text),
                    bool(result.asr_text),
                    bool(result.card_visual_text),
                    has_visual,
                    len(result.raw_summary),
                )
                result.result_level = "summary"
                if config.cache_enabled:
                    safe_summary = None if self._deps.summary_service.is_bad_summary(summary_result.raw_summary) else summary_result.raw_summary
                    cache_summary = safe_summary if config.cache_summary and not result.card_visual_text else None
                    self._save_cache(cache_key, metadata, raw_info, cache_summary, config.cache_fingerprint)
            else:
                result.raw_info_text = self._deps.summary_service.build_raw_info_text(metadata, result.raw_info)
                if config.summary_fallback_to_raw_info:
                    result.result_level = "raw_info"
                else:
                    result.raw_info_text = self._deps.summary_service.build_basic_info_text(metadata)
                    result.result_level = "metadata"
                logger.info(
                    "视频总结失败，回退 raw_info/basic_info: video_id=%s, page=%s, subtitle_used=%s, asr_used=%s, card_visual_used=%s, visual_used=%s",
                    metadata.video_id,
                    metadata.page,
                    bool(result.subtitle_text),
                    bool(result.asr_text),
                    bool(result.card_visual_text),
                    has_visual,
                )
                if config.cache_enabled and config.cache_raw_info:
                    self._save_cache(cache_key, metadata, raw_info, None, config.cache_fingerprint)

            result.success = True
            return result
        except Exception as exc:
            logger.exception("视频处理阶段失败: video_id=%s, page=%s", metadata.video_id, metadata.page)
            return self._finalize_failure_result(
                result,
                error=str(exc) or "视频处理失败",
                fallback_reason="processing_exception",
            )
        finally:
            if config.cleanup_on_success and config.temp_file_max_age_min == 0:
                cleanup_temp_files(result.video_path or None, result.frames_dir or None, result.audio_path or None)

    async def _build_failure_result(self, video_ref: BilibiliVideoRef, config: PipelineConfig, *, error: str, fallback_reason: str) -> VideoAnalysisResult:
        try:
            metadata = await self._build_metadata(video_ref, config)
        except Exception:
            logger.exception("失败回退阶段获取 metadata 失败: video_id=%s, page=%s", video_ref.video_id, video_ref.page)
            metadata = self._build_minimal_metadata(video_ref)
        result = VideoAnalysisResult(
            metadata=metadata,
            cache_key=self._deps.cache_manager.build_cache_key(metadata.video_id, metadata.page, config.cache_fingerprint),
        )
        return self._finalize_failure_result(result, error=error, fallback_reason=fallback_reason)

    def _finalize_failure_result(self, result: VideoAnalysisResult, *, error: str, fallback_reason: str) -> VideoAnalysisResult:
        result.error = error
        result.fallback_reason = fallback_reason
        result.success = False
        if result.raw_info:
            result.raw_info_text = self._deps.summary_service.build_raw_info_text(result.metadata, result.raw_info)
            result.result_level = "raw_info"
            return result

        metadata_text = self._deps.summary_service.build_basic_info_text(result.metadata)
        result.raw_info_text = metadata_text
        has_metadata = any(
            [
                str(result.metadata.title or "").strip(),
                str(result.metadata.author or "").strip(),
                str(result.metadata.description or "").strip(),
                result.metadata.duration_sec is not None,
            ]
        )
        result.result_level = "metadata" if has_metadata else "minimal"
        return result

    @staticmethod
    def _build_minimal_metadata(video_ref: BilibiliVideoRef) -> BilibiliVideoMetadata:
        return BilibiliVideoMetadata(
            video_id=video_ref.video_id,
            page=video_ref.page,
        )

    async def _build_metadata(self, video_ref: BilibiliVideoRef, config: PipelineConfig) -> BilibiliVideoMetadata:
        metadata = await self._deps.metadata_service.build_metadata(video_ref)
        if video_ref.source == "short":
            resolved = await BilibiliAPI.resolve_short_url(video_ref.video_id, timeout_sec=config.short_url_timeout_sec)
            if resolved:
                metadata.video_id, metadata.page = resolved
        video_info = await BilibiliAPI.get_video_info(
            metadata.video_id,
            config.sessdata,
            metadata.page,
            max_attempts=config.retry_max_attempts,
            retry_interval=config.retry_interval_sec,
            timeout_sec=config.video_api_timeout_sec,
        )
        if video_info:
            metadata.title = str(video_info.get("title", "") or "")
            metadata.author = str((video_info.get("owner") or {}).get("name", "") or "")
            metadata.description = str(video_info.get("desc", "") or "")
            metadata.duration_sec = int(video_info.get("duration") or 0) if video_info.get("duration") is not None else None
            metadata.page = int(video_info.get("page") or metadata.page)
            metadata.page_title = str(video_info.get("page_title", "") or "")
            metadata.total_pages = int(video_info.get("total_pages") or 1)
            metadata.total_duration_sec = int(video_info.get("total_duration") or 0) if video_info.get("total_duration") is not None else None
            metadata.aid = video_info.get("aid")
            metadata.cid = video_info.get("cid")
        return metadata

    async def _fetch_subtitle(self, metadata: BilibiliVideoMetadata, config: PipelineConfig) -> str:
        if not metadata.aid or not metadata.cid:
            logger.info(
                "字幕获取已跳过: video_id=%s, page=%s, reason=missing_aid_or_cid",
                metadata.video_id,
                metadata.page,
            )
            return ""
        subtitle = await BilibiliAPI.get_subtitle(
            metadata.aid,
            metadata.cid,
            sessdata=config.sessdata,
            max_attempts=config.retry_max_attempts,
            retry_interval=config.retry_interval_sec,
            timeout_sec=config.subtitle_timeout_sec,
        )
        return subtitle or ""

    def _need_visual_analysis(self, metadata: BilibiliVideoMetadata, config: PipelineConfig) -> bool:
        if config.visual_method == "none":
            return False
        if metadata.duration_sec is None:
            return config.visual_max_duration_min > 0
        video_minutes = metadata.duration_sec // 60
        return config.visual_max_duration_min > 0 and video_minutes < int(config.visual_max_duration_min) + 1

    async def _extract_frame_paths(self, video_path: str, metadata: BilibiliVideoMetadata, config: PipelineConfig) -> list[str]:
        duration = metadata.duration_sec or self._deps.video_parser.get_video_duration(video_path)
        max_frames = max(1, int(config.max_frames))
        interval_sec = max(1, int(config.frame_interval_sec))
        if config.lock_even_frames:
            if duration:
                return await self._deps.video_parser.extract_frames_equidistant(video_path, float(duration), count=max_frames)
            return await self._deps.video_parser.extract_frames(video_path, interval_sec=interval_sec, max_frames=max_frames)
        if duration:
            n_frames = max(1, int(math.ceil(float(duration) / interval_sec)))
            return await self._deps.video_parser.extract_frames_equidistant(video_path, float(duration), count=n_frames)
        return await self._deps.video_parser.extract_frames(video_path, interval_sec=interval_sec, max_frames=max_frames)

    def _build_frame_analyzer(self, visual_method: str, config: PipelineConfig):
        if visual_method == "host":
            self._deps.video_analyzer.set_config(
                {
                    "host_task_name": config.host_task_name,
                    "temperature": config.host_temperature,
                    "max_tokens": config.host_max_tokens,
                    "frame_prompt": config.visual_prompt,
                }
            )
            return self._deps.video_analyzer
        return None

    @staticmethod
    def _merge_text_content(subtitle_text: str, asr_text: str) -> str:
        parts: list[str] = []
        if subtitle_text:
            parts.append(f"【字幕内容】\n{subtitle_text}")
        if asr_text:
            parts.append(f"【语音识别内容】\n{asr_text}")
        return "\n\n".join(parts)

    @staticmethod
    def _resolve_frames_dir(frame_paths: list[str]) -> str:
        if not frame_paths:
            return ""
        return os.path.dirname(frame_paths[0])

    @staticmethod
    def _build_runtime_raw_info(raw_info: dict[str, Any], card_visual_text: str) -> dict[str, Any]:
        runtime_raw_info = dict(raw_info)
        normalized_card_visual_text = str(card_visual_text or "").strip()
        if normalized_card_visual_text:
            runtime_raw_info["card_visual_text"] = normalized_card_visual_text
        return runtime_raw_info

    def _save_cache(self, cache_key: str, metadata: BilibiliVideoMetadata, raw_info: dict[str, Any], summary: str | None, cache_fingerprint: str) -> None:
        has_visual = bool(raw_info.get("frame_descriptions")) or bool(raw_info.get("visual_analysis"))
        has_text = bool(raw_info.get("subtitle_text")) or bool(raw_info.get("asr_text"))
        if not has_visual and not has_text:
            return
        cache_data = {
            "cache_schema_version": 2,
            "cache_fingerprint": cache_fingerprint,
            "video_id": metadata.video_id,
            "page": metadata.page,
            "page_title": metadata.page_title,
            "total_pages": metadata.total_pages,
            "title": metadata.title,
            "author": metadata.author,
            "description": metadata.description,
            "duration": metadata.duration_sec,
            "total_duration": metadata.total_duration_sec,
            "raw_info": raw_info,
            "summary": summary,
            "has_subtitle": bool(raw_info.get("subtitle_text")),
            "has_asr": bool(raw_info.get("asr_text")),
        }
        logger.info(
            "视频分析保存缓存: video_id=%s, page=%s, cache_key=%s, cache_fingerprint=%s, has_summary=%s, has_raw_info=%s",
            metadata.video_id,
            metadata.page,
            cache_key,
            cache_fingerprint,
            bool(summary),
            bool(raw_info),
        )
        self._deps.cache_manager.save_cache(cache_key, cache_data)

    def _from_cache(self, metadata: BilibiliVideoMetadata, cache_key: str, payload: dict[str, Any], *, card_visual_text: str = "") -> VideoAnalysisResult:
        raw_info = payload.get("raw_info", {}) if isinstance(payload, dict) else {}
        normalized_card_visual_text = str(card_visual_text or "").strip()
        summary = payload.get("summary") if isinstance(payload, dict) and not normalized_card_visual_text else None
        result = VideoAnalysisResult(metadata=metadata, cache_key=cache_key)
        result.subtitle_text = str(raw_info.get("subtitle_text", "") or "")
        result.asr_text = str(raw_info.get("asr_text", "") or "")
        result.card_visual_text = normalized_card_visual_text
        result.frame_descriptions = list(raw_info.get("frame_descriptions", []) or [])
        result.visual_text = str(raw_info.get("visual_analysis", "") or "")
        result.raw_info = dict(raw_info)
        if result.card_visual_text:
            result.raw_info["card_visual_text"] = result.card_visual_text
        result.raw_info_text = self._deps.summary_service.build_raw_info_text(metadata, result.raw_info)
        if summary:
            result.result_level = "summary"
            result.raw_summary = str(summary)
            result.summary_text = self._deps.summary_service.build_video_info_text(metadata, str(summary))
        else:
            result.result_level = "raw_info"
        result.success = True
        return result
