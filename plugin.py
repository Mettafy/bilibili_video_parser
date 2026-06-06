"""Maisaka 版 B 站视频解析插件入口。"""

from __future__ import annotations

import asyncio
import logging

from pathlib import Path
from time import monotonic
from typing import Any
import hashlib
import json

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import BilibiliVideoParserConfig
from .core.asr.openai_compatible import OpenAICompatibleAsrProvider
from .core.asr_service import AsrService
from .core.cache_manager import CacheManager
from .core.metadata_service import MetadataService
from .core.pipeline import PipelineConfig, PipelineDependencies
from .core.pipeline import VideoPipeline
from .core.safe_delete import cleanup_old_temp_files, init_temp_dir
from .core.subtitle_service import SubtitleService
from .core.text_render_service import TextRenderService
from .core.video_analyzer import VideoAnalyzer
from .core.video_parser import VideoParser
from .core.visual_service import VisualService
from .runtime import AutoDetectResultService, CommandRequestParser, CommandResultService, HookInjector, LLMHostAdapter, MessageContextBuilder, MessageFormatter, NapCatBilibiliResolver
from .runtime.llm_host_adapter import HostTimeoutPolicy


HOOK_TIMEOUT_MS = 300_000


class BilibiliVideoParserMaisakaPlugin(MaiBotPlugin):
    """基于 Maisaka SDK 的 B 站视频解析插件。"""

    config_model = BilibiliVideoParserConfig
    _BILIBILI_TEXT_MARKERS = (
        "bilibili.com/video/",
        "b23.tv/",
        "b23.tv",
        "哔哩哔哩：",
        "[小程序]",
        "[json",
        "[xml]",
        "[share]",
    )

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).resolve().parent
        self._data_dir = self._plugin_dir / "data"
        self._cache_dir = self._data_dir / "cache"
        self._temp_dir = self._data_dir / "temp"
        self._message_formatter = MessageFormatter()
        self._hook_injector = HookInjector(self._message_formatter)
        self._command_parser = CommandRequestParser()
        self._auto_detect_result_service = None
        self._command_result_service = None
        self._host_llm_adapter: LLMHostAdapter | None = None
        self._napcat_resolver = None
        self._pipeline: VideoPipeline | None = None
        self._metadata_service = MetadataService()
        self._subtitle_service = SubtitleService()
        self._visual_service = VisualService()
        self._text_render_service = TextRenderService()
        self._summary_service = None
        self._video_analyzer: VideoAnalyzer | None = None
        self._video_parser: VideoParser | None = None
        self._cache_manager = CacheManager(self._cache_dir)
        self._host_task_snapshot: set[str] = set()
        self._cleanup_task = None

    async def on_load(self) -> None:
        self._ensure_runtime_dirs()
        init_temp_dir(str(self._data_dir))
        self._refresh_logger_level()
        self._video_parser = VideoParser(
            self.config.video.ffmpeg_path,
            ffmpeg_probe_timeout_sec=self.config.video.ffmpeg_probe_timeout_sec,
            ffmpeg_extract_audio_timeout_sec=self.config.video.ffmpeg_extract_audio_timeout_sec,
            ffmpeg_extract_frames_timeout_sec=self.config.video.ffmpeg_extract_frames_timeout_sec,
        )
        self._host_llm_adapter = self._build_host_llm_adapter()
        self._summary_service = self._build_summary_service()
        self._auto_detect_result_service = self._build_auto_detect_result_service()
        self._command_result_service = self._build_command_result_service()
        self._napcat_resolver = NapCatBilibiliResolver(self.ctx)
        self._video_analyzer = self._build_video_analyzer()
        self._pipeline = self._build_pipeline()
        await self._refresh_host_task_snapshot()
        await self._start_cleanup_task_after_delay()
        self.ctx.logger.info(
            "B站视频解析插件已加载: enabled=%s, visual_method=%s, summary_enabled=%s",
            self.config.plugin.enabled,
            self.config.analysis.visual_method,
            self.config.summary.enabled,
        )

    async def on_unload(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        self._host_task_snapshot.clear()
        self._host_llm_adapter = None

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data
        del version
        if scope == "self":
            self._refresh_logger_level()
            self._video_parser = VideoParser(
                self.config.video.ffmpeg_path,
                ffmpeg_probe_timeout_sec=self.config.video.ffmpeg_probe_timeout_sec,
                ffmpeg_extract_audio_timeout_sec=self.config.video.ffmpeg_extract_audio_timeout_sec,
                ffmpeg_extract_frames_timeout_sec=self.config.video.ffmpeg_extract_frames_timeout_sec,
            )
            self._host_llm_adapter = self._build_host_llm_adapter()
            self._summary_service = self._build_summary_service()
            self._auto_detect_result_service = self._build_auto_detect_result_service()
            self._command_result_service = self._build_command_result_service()
            self._napcat_resolver = NapCatBilibiliResolver(self.ctx)
            self._video_analyzer = self._build_video_analyzer()
            self._pipeline = self._build_pipeline()
            await self._refresh_host_task_snapshot()
            self.ctx.logger.info(
                "B站视频解析插件配置已刷新: enabled=%s, summary_enabled=%s",
                self.config.plugin.enabled,
                self.config.auto_detect.enable_summary,
            )

    @HookHandler(
        "chat.receive.after_process",
        name="bilibili_auto_detect",
        description="自动检测消息中的 B 站链接并注入上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_auto_detect(self, **kwargs: Any) -> dict[str, Any]:
        if not self.config.plugin.enabled or not self.config.trigger.auto_detect_enabled:
            return self._hook_injector.pass_through().to_hook_result()

        message = kwargs.get("message")
        if message is None:
            return self._hook_injector.pass_through().to_hook_result()

        stream_id = str(kwargs.get("stream_id", "") or "")
        message_context = MessageContextBuilder.build(message, stream_id=stream_id)
        session_id = message_context.effective_stream_id
        if not session_id:
            self.ctx.logger.warning(
                "自动检测跳过：缺少 session_id，无法建立稳定上下文: message_id=%s, platform=%s, fallback_stream_id=%s",
                message_context.message_id,
                message_context.platform,
                message_context.stream_id,
            )
            return self._hook_injector.pass_through().to_hook_result()

        if not self._allow_auto_detect_for_message(message_context):
            return self._hook_injector.pass_through().to_hook_result()

        if self._looks_like_bili_command(message_context.processed_plain_text, message_context.plain_text):
            self.ctx.logger.debug(
                "自动检测跳过：当前消息已命中 /bili 指令，由命令链独占处理: message_id=%s, session_id=%s",
                message_context.message_id,
                session_id,
            )
            return self._hook_injector.pass_through().to_hook_result()

        if not self._should_attempt_bilibili_detect(message_context):
            self.ctx.logger.debug(
                "自动检测跳过：普通消息未命中 B 站门禁: message_id=%s, session_id=%s",
                message_context.message_id,
                session_id,
            )
            return self._hook_injector.pass_through().to_hook_result()

        if not message_context.processed_plain_text.strip() and not message_context.raw_segments:
            self.ctx.logger.debug("自动检测跳过：processed_plain_text 为空")

        if self._pipeline is None:
            return self._hook_injector.pass_through().to_hook_result()

        if self._napcat_resolver is None:
            return self._hook_injector.pass_through().to_hook_result()

        resolved_target = await self._napcat_resolver.resolve(message_context)
        if resolved_target is None:
            self.ctx.logger.debug("自动检测跳过：未识别到有效视频引用")
            return self._hook_injector.pass_through().to_hook_result()

        video_ref = self._metadata_service.locate(resolved_target.source_text)
        if video_ref is None:
            video_ref = self._metadata_service.locate(resolved_target.video_id)
        if video_ref is None:
            self.ctx.logger.debug("自动检测跳过：解析结果未能构造视频引用")
            return self._hook_injector.pass_through().to_hook_result()

        video_ref.video_id = resolved_target.video_id
        video_ref.page = resolved_target.page
        video_ref.source = resolved_target.video_type

        self.ctx.logger.info(
            "自动检测命中 B 站视频: video_id=%s, page=%s, source_kind=%s, message_id=%s, session_id=%s",
            video_ref.video_id,
            video_ref.page,
            resolved_target.source_kind,
            message_context.message_id,
            session_id,
        )

        card_visual_text = ""
        auto_detect_state: dict[str, Any] = {"latest_result": None}
        try:
            card_visual_text = await self._resolve_card_visual_texts(message_context.card_visual_hashes)
            result = await self._run_auto_detect_pipeline(
                video_ref,
                card_visual_text=card_visual_text,
                state=auto_detect_state,
            )
            auto_detect_state["latest_result"] = result
            if self._auto_detect_result_service is None:
                raise RuntimeError("自动检测结果装配服务未初始化")
            result_snapshot = self._auto_detect_result_service.describe_result(result)
            rewritten_text = self._auto_detect_result_service.build_success_text(
                original_text=self._get_message_visible_text(message_context.raw_message),
                result=result,
            )
            self.ctx.logger.info(
                "自动检测完整分析完成: video_id=%s, message_id=%s, session_id=%s, output_length=%s, result_level=%s, has_metadata=%s, has_raw_info=%s, has_summary=%s, fallback_reason=%s",
                video_ref.video_id,
                message_context.message_id,
                session_id,
                len(rewritten_text),
                result_snapshot["result_level"],
                result_snapshot["has_metadata"],
                result_snapshot["has_raw_info"],
                result_snapshot["has_summary"],
                result_snapshot["fallback_reason"],
            )
        except asyncio.TimeoutError:
            if self._auto_detect_result_service is None:
                raise RuntimeError("自动检测结果装配服务未初始化")
            timeout_result = None
            timeout_snapshot = self._auto_detect_result_service.describe_result(timeout_result)
            rewritten_text = self._auto_detect_result_service.build_fallback_text(
                original_text=self._get_message_visible_text(message_context.raw_message),
                video_ref=video_ref,
                result=timeout_result,
            )
            self.ctx.logger.warning(
                "自动检测完整分析超时，已降级输出: video_id=%s, timeout_sec=%s, session_id=%s, result_level=%s, has_metadata=%s, has_raw_info=%s, has_summary=%s, fallback_reason=%s",
                video_ref.video_id,
                self.config.trigger_timeout.auto_detect_total_timeout_sec,
                session_id,
                timeout_snapshot["result_level"],
                timeout_snapshot["has_metadata"],
                timeout_snapshot["has_raw_info"],
                timeout_snapshot["has_summary"],
                timeout_snapshot["fallback_reason"],
            )
        except Exception:
            if self._auto_detect_result_service is None:
                raise RuntimeError("自动检测结果装配服务未初始化")
            failure_result = auto_detect_state.get("latest_result")
            if failure_result is not None and not failure_result.fallback_reason:
                failure_result.fallback_reason = "auto_detect_system_exception"
            failure_snapshot = self._auto_detect_result_service.describe_result(failure_result)
            rewritten_text = self._auto_detect_result_service.build_fallback_text(
                original_text=self._get_message_visible_text(message_context.raw_message),
                video_ref=video_ref,
                result=failure_result,
            )
            self.ctx.logger.error(
                "自动检测完整分析失败，已降级输出: video_id=%s, session_id=%s, result_level=%s, has_metadata=%s, has_raw_info=%s, has_summary=%s, fallback_reason=%s",
                video_ref.video_id,
                session_id,
                failure_snapshot["result_level"],
                failure_snapshot["has_metadata"],
                failure_snapshot["has_raw_info"],
                failure_snapshot["has_summary"],
                failure_snapshot["fallback_reason"],
                exc_info=True,
            )

        decision = self._hook_injector.rewrite_text(kwargs=kwargs, rewritten_text=rewritten_text)
        self.ctx.logger.info(
            "自动检测消息已重写到 Maisaka 消费层: message_id=%s, session_id=%s, raw_message_rewritten=%s",
            message_context.message_id,
            session_id,
            True,
        )
        return decision.to_hook_result()

    @Command("bili", description="解析 B 站视频", pattern=r"^/bili(?:\s+.+)?$", timeout_ms=600000)
    async def handle_bili_command(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        del kwargs
        if not self.config.plugin.enabled or not self.config.trigger.command_enabled:
            return False, "插件未启用命令模式", False

        message_context = MessageContextBuilder.build(message, stream_id=stream_id) if message is not None else None
        reply_stream_id = stream_id or (message_context.effective_stream_id if message_context is not None else "")
        raw_text = self._extract_command_text(message_context.raw_message if message_context is not None else message)
        try:
            invocation = self._command_parser.parse(
                raw_text,
                allow_extra_arguments=self.config.command.allow_extra_arguments,
                allow_force_visual=self.config.command.allow_force_visual,
                allow_force_asr=self.config.command.allow_force_asr,
            )
        except ValueError as exc:
            error_text = self._message_formatter.build_command_error_text(str(exc))
            await self.ctx.send.text(error_text, reply_stream_id)
            return False, error_text, True

        if self.config.command.show_processing_message:
            await self.ctx.send.text(self._message_formatter.build_processing_text(), reply_stream_id)

        if self._pipeline is None:
            error_text = self._message_formatter.build_command_error_text("流水线未初始化")
            await self.ctx.send.text(error_text, reply_stream_id)
            return False, error_text, True

        video_ref = self._metadata_service.locate(invocation.url_or_id)
        if video_ref is None:
            error_text = self._message_formatter.build_command_error_text("未识别到有效的 B 站视频链接或视频 ID")
            await self.ctx.send.text(error_text, reply_stream_id)
            return False, error_text, True

        try:
            result = await asyncio.wait_for(
                self._run_pipeline(
                    video_ref,
                    force_summary=self.config.summary.enabled,
                    command_force_visual=invocation.options.force_visual,
                    command_force_asr=invocation.options.force_asr,
                    card_visual_text=(
                        await self._resolve_card_visual_texts(message_context.card_visual_hashes)
                        if message_context is not None
                        else ""
                    ),
                ),
                timeout=max(1, int(self.config.trigger_timeout.command_total_timeout_sec)),
            )
        except asyncio.TimeoutError:
            error_text = self._message_formatter.build_command_error_text(
                f"命令链执行超时（>{int(self.config.trigger_timeout.command_total_timeout_sec)}秒）"
            )
            await self.ctx.send.text(error_text, reply_stream_id)
            return False, error_text, True
        if self._command_result_service is None:
            raise RuntimeError("指令结果装配服务未初始化")
        response_text = self._command_result_service.build_result_text(result)
        output_snapshot = self._command_result_service.describe_output(result)
        self.ctx.logger.info(
            "指令结果装配完成: output_level=%s, summary_used=%s, raw_info_used=%s, metadata_used=%s, summary_failed=%s",
            output_snapshot["output_level"],
            output_snapshot["summary_used"],
            output_snapshot["raw_info_used"],
            output_snapshot["metadata_used"],
            output_snapshot["summary_failed"],
        )
        await self.ctx.send.text(response_text, reply_stream_id)
        return True, "命令执行完成", True

    def _ensure_runtime_dirs(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    def _refresh_logger_level(self) -> None:
        logger_level = logging.DEBUG if self.config.plugin.debug else getattr(logging, self.config.plugin.log_level, logging.INFO)
        self.ctx.logger.setLevel(logger_level)

    def _build_host_llm_adapter(self) -> LLMHostAdapter:
        timeout_policy = HostTimeoutPolicy(
            enabled=self.config.host_timeout.enabled,
            request_timeout_min=self.config.host_timeout.task_request_timeout_min,
        )
        return LLMHostAdapter(
            self.ctx,
            timeout_policy=timeout_policy,
        )

    def _build_pipeline(self) -> VideoPipeline:
        if self._video_parser is None:
            raise RuntimeError("视频解析器未初始化")
        provider = None
        if self.config.asr.enabled:
            if self.config.asr.provider_type != "openai_compatible":
                raise RuntimeError(f"不支持的 ASR provider_type: {self.config.asr.provider_type}")
            provider = OpenAICompatibleAsrProvider(
                base_url=self.config.asr.base_url,
                api_key=self.config.asr.api_key,
                model=self.config.asr.model,
                timeout_sec=self.config.asr.timeout_sec,
                max_retries=self.config.asr.max_retries,
                retry_interval_sec=self.config.asr.retry_interval_sec,
                language=self.config.asr.language,
                prompt=self.config.asr.prompt,
            )
        deps = PipelineDependencies(
            metadata_service=self._metadata_service,
            video_parser=self._video_parser,
            video_analyzer=self._video_analyzer,
            subtitle_service=self._subtitle_service,
            visual_service=self._visual_service,
            summary_service=self._summary_service,
            text_render_service=self._text_render_service,
            asr_service=AsrService(provider),
            host_llm_adapter=self._host_llm_adapter,
            cache_manager=self._cache_manager,
        )
        return VideoPipeline(deps)

    def _build_video_analyzer(self) -> VideoAnalyzer:
        host_config = self.config.analysis.host.model_dump(mode="python")
        host_config["host_task_name"] = self.config.analysis.host.host_task_name
        return VideoAnalyzer(host_llm_adapter=self._host_llm_adapter, vlm_config=host_config)

    def _build_summary_service(self):
        from .core.summary_service import SummaryService

        return SummaryService(self._text_render_service, self._host_llm_adapter)

    def _build_auto_detect_result_service(self) -> AutoDetectResultService:
        return AutoDetectResultService(self._message_formatter, self._text_render_service)

    def _build_command_result_service(self) -> CommandResultService:
        if self._summary_service is None:
            raise RuntimeError("视频总结服务未初始化")
        return CommandResultService(self._summary_service)

    async def _run_pipeline(
        self,
        video_ref,
        *,
        force_summary: bool | None = None,
        command_force_visual: bool = False,
        command_force_asr: bool = False,
        card_visual_text: str = "",
    ):
        if self._pipeline is None:
            raise RuntimeError("流水线未初始化")
        pipeline_config = self._build_pipeline_config(
            force_summary=force_summary,
            command_force_visual=command_force_visual,
            command_force_asr=command_force_asr,
            card_visual_text=card_visual_text,
        )
        return await self._pipeline.run(video_ref, pipeline_config)

    def _build_pipeline_config(
        self,
        *,
        force_summary: bool | None,
        command_force_visual: bool = False,
        command_force_asr: bool = False,
        card_visual_text: str = "",
    ) -> PipelineConfig:
        effective_visual_method = self.config.analysis.visual_method
        summary_enabled = self.config.summary.enabled
        if self._host_llm_adapter is not None:
            preferred_visual_task_name = self._host_llm_adapter.resolve_preferred_task(
                self.config.analysis.host.host_task_name,
                ["vlm", "replyer"],
                allow_fallback=False,
            )
            preferred_summary_task_name = self._host_llm_adapter.resolve_preferred_task(
                self.config.summary.host_task_name,
                ["replyer", "planner"],
                allow_fallback=False,
            )
        else:
            preferred_visual_task_name = self.config.analysis.host.host_task_name
            preferred_summary_task_name = self.config.summary.host_task_name
        if effective_visual_method == "host" and preferred_visual_task_name not in self._host_task_snapshot:
            self.ctx.logger.warning(
                "宿主视觉任务不可用，已降级为 none: configured_task=%s",
                preferred_visual_task_name,
            )
            effective_visual_method = "none"
        if summary_enabled and preferred_summary_task_name not in self._host_task_snapshot:
            self.ctx.logger.warning(
                "宿主总结任务不可用，已关闭总结阶段: configured_task=%s",
                preferred_summary_task_name,
            )
            summary_enabled = False
        if force_summary is None:
            force_summary = self.config.auto_detect.enable_summary and summary_enabled
        cache_fingerprint = self._build_cache_fingerprint(
            effective_visual_method=effective_visual_method,
            effective_visual_task_name=preferred_visual_task_name,
            summary_enabled=summary_enabled,
            effective_summary_task_name=preferred_summary_task_name,
        )
        self.ctx.logger.info(
            "流水线配置生效: configured_visual_method=%s, effective_visual_method=%s, configured_visual_task=%s, effective_visual_task=%s, configured_summary_enabled=%s, effective_summary_enabled=%s, configured_summary_task=%s, effective_summary_task=%s, cache_fingerprint=%s",
            self.config.analysis.visual_method,
            effective_visual_method,
            self.config.analysis.host.host_task_name,
            preferred_visual_task_name,
            self.config.summary.enabled,
            summary_enabled,
            self.config.summary.host_task_name,
            preferred_summary_task_name,
            cache_fingerprint,
        )
        return PipelineConfig(
            auto_detect_enable_summary=force_summary,
            summary_enabled=summary_enabled,
            summary_fallback_to_raw_info=self.config.summary.fallback_to_raw_info,
            summary_custom_prompt=self.config.summary.custom_prompt,
            summary_max_chars=self.config.summary.summary_max_chars,
            visual_method=effective_visual_method,
            cache_enabled=self.config.cache.enabled,
            cleanup_on_success=self.config.cache.cleanup_on_success,
            sessdata=self.config.video.sessdata,
            max_duration_min=self.config.video.max_duration_min,
            max_size_mb=self.config.video.max_size_mb,
            visual_max_duration_min=self.config.analysis.visual_max_duration_min,
            max_frames=self.config.analysis.max_frames,
            lock_even_frames=self.config.analysis.lock_even_frames,
            frame_interval_sec=self.config.analysis.frame_interval_sec,
            visual_prompt=self._get_visual_prompt(),
            host_task_name=preferred_visual_task_name,
            host_temperature=self.config.analysis.host.temperature,
            host_max_tokens=self.config.analysis.host.max_tokens,
            summary_task_name=preferred_summary_task_name,
            summary_temperature=self.config.summary.temperature,
            summary_max_tokens=self.config.summary.max_tokens,
            asr_config=self.config.asr.model_dump(mode="python"),
            video_api_timeout_sec=self.config.video.api_timeout_sec,
            short_url_timeout_sec=self.config.video.short_url_timeout_sec,
            subtitle_timeout_sec=self.config.video.subtitle_timeout_sec,
            download_url_timeout_sec=self.config.video.download_url_timeout_sec,
            download_timeout_sec=self.config.video.download_timeout_sec,
            ffmpeg_probe_timeout_sec=self.config.video.ffmpeg_probe_timeout_sec,
            ffmpeg_extract_audio_timeout_sec=self.config.video.ffmpeg_extract_audio_timeout_sec,
            ffmpeg_extract_frames_timeout_sec=self.config.video.ffmpeg_extract_frames_timeout_sec,
            media_pipeline_timeout_sec=self.config.video.media_pipeline_timeout_sec,
            retry_max_attempts=self.config.video.retry_max_attempts,
            retry_interval_sec=self.config.video.retry_interval_sec,
            temp_file_max_age_min=self.config.cache.temp_file_max_age_min,
            parallel_frame_analysis=self.config.analysis.parallel_frame_analysis,
            parallel_frame_analysis_limit=self.config.analysis.parallel_frame_analysis_limit,
            command_force_visual=command_force_visual,
            command_force_asr=command_force_asr,
            cache_summary=self.config.cache.cache_summary,
            cache_raw_info=self.config.cache.cache_raw_info,
            cache_visual_result=self.config.cache.cache_visual_result,
            cache_subtitle=self.config.cache.cache_subtitle,
            cache_asr=self.config.cache.cache_asr,
            asr_max_audio_duration_min=self.config.asr.max_audio_duration_min,
            card_visual_text=card_visual_text,
            cache_fingerprint=cache_fingerprint,
        )

    async def _run_auto_detect_pipeline(self, video_ref, *, card_visual_text: str = "", state: dict[str, Any] | None = None):
        if self._pipeline is None:
            raise RuntimeError("流水线未初始化")

        timeout_sec = max(1, int(self.config.trigger_timeout.auto_detect_total_timeout_sec))
        pipeline_config = self._build_pipeline_config(
            force_summary=self.config.auto_detect.enable_summary and self.config.summary.enabled,
            card_visual_text=card_visual_text,
        )
        started_at = monotonic()
        prepared_result = await asyncio.wait_for(
            self._pipeline.prepare_metadata_result(video_ref, pipeline_config),
            timeout=timeout_sec,
        )
        if state is not None:
            state["latest_result"] = prepared_result
        remaining_timeout = timeout_sec - (monotonic() - started_at)
        if remaining_timeout <= 0:
            prepared_result.success = False
            prepared_result.fallback_reason = "auto_detect_total_timeout"
            return prepared_result
        try:
            enriched_result = await asyncio.wait_for(
                self._pipeline.enrich_result(prepared_result, pipeline_config),
                timeout=max(0.1, remaining_timeout),
            )
            if state is not None:
                state["latest_result"] = enriched_result
        except asyncio.TimeoutError:
            prepared_result.success = False
            prepared_result.fallback_reason = "auto_detect_total_timeout"
            if state is not None:
                state["latest_result"] = prepared_result
            return prepared_result
        if not enriched_result.fallback_reason and enriched_result.result_level == "metadata":
            enriched_result.fallback_reason = prepared_result.fallback_reason
        return enriched_result

    def _build_cache_fingerprint(
        self,
        *,
        effective_visual_method: str,
        effective_visual_task_name: str,
        summary_enabled: bool,
        effective_summary_task_name: str,
    ) -> str:
        payload = {
            "visual_method": effective_visual_method,
            "visual_task": effective_visual_task_name,
            "visual_prompt": self.config.analysis.host.frame_prompt,
            "summary_enabled": summary_enabled,
            "summary_task": effective_summary_task_name,
            "summary_prompt": self.config.summary.custom_prompt,
            "summary_max_chars": self.config.summary.summary_max_chars,
            "summary_max_tokens": self.config.summary.max_tokens,
            "summary_temperature": self.config.summary.temperature,
            "summary_fallback_to_raw_info": self.config.summary.fallback_to_raw_info,
            "asr_enabled": self.config.asr.enabled,
            "cache_visual_result": self.config.cache.cache_visual_result,
            "cache_subtitle": self.config.cache.cache_subtitle,
            "cache_asr": self.config.cache.cache_asr,
            "cache_summary": self.config.cache.cache_summary,
            "output_schema_version": "2",
        }
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.md5(serialized.encode("utf-8")).hexdigest()[:12]

    def _get_visual_prompt(self) -> str:
        return self.config.analysis.host.frame_prompt

    async def _resolve_card_visual_texts(self, card_visual_hashes: list[str]) -> str:
        normalized_hashes: list[str] = []
        for card_hash in card_visual_hashes:
            normalized = str(card_hash or "").strip()
            if normalized and normalized not in normalized_hashes:
                normalized_hashes.append(normalized)
        if not normalized_hashes:
            return ""

        descriptions: list[str] = []
        for card_hash in normalized_hashes:
            description = await self._get_image_description_by_hash(card_hash)
            if description and description not in descriptions:
                descriptions.append(description)
        return "\n".join(descriptions)

    async def _get_image_description_by_hash(self, image_hash: str) -> str:
        normalized_hash = str(image_hash or "").strip()
        if not normalized_hash:
            return ""

        try:
            record = await self.ctx.db.get(
                model_name="Images",
                filters={"image_hash": normalized_hash, "image_type": "image"},
                limit=1,
                single_result=True,
            )
        except Exception:
            self.ctx.logger.debug("卡片预览图描述查询失败: image_hash=%s", normalized_hash, exc_info=True)
            return ""

        if not isinstance(record, dict):
            return ""

        description = str(record.get("description", "") or "").strip()
        if not description:
            self.ctx.logger.debug("卡片预览图描述未命中: image_hash=%s", normalized_hash)
            return ""
        self.ctx.logger.debug("卡片预览图描述命中: image_hash=%s, description_length=%s", normalized_hash, len(description))
        return description

    @staticmethod
    def _looks_like_bili_command(*texts: str) -> bool:
        for text in texts:
            normalized = str(text or "").strip().lower()
            if normalized.startswith("/bili"):
                return True
        return False

    async def _refresh_host_task_snapshot(self) -> None:
        if self._host_llm_adapter is None:
            return
        validation = await self._host_llm_adapter.validate_tasks(
            visual_task_name=self.config.analysis.host.host_task_name,
            summary_task_name=self.config.summary.host_task_name,
        )
        self._host_task_snapshot = validation.available_tasks
        self.ctx.logger.info(
            "宿主任务校验完成: visual=%s, summary=%s, tasks=%s",
            validation.visual_task_ready,
            validation.summary_task_ready,
            ",".join(sorted(validation.available_tasks)) or "<empty>",
        )
        if not validation.visual_task_ready and self.config.analysis.visual_method == "host":
            self.ctx.logger.warning(
                "宿主未提供视觉任务 %s，host 模式后续将无法执行完整视觉分析",
                self.config.analysis.host.host_task_name,
            )
        if self.config.summary.enabled and not validation.summary_task_ready:
            self.ctx.logger.warning(
                "宿主未提供总结任务 %s，总结阶段后续只能降级为原始信息",
                self.config.summary.host_task_name,
            )

    def _allow_auto_detect_for_message(self, message_context) -> bool:
        if message_context.is_group_message and not self.config.trigger.auto_detect_in_groups:
            return False
        if message_context.is_private_message and not self.config.trigger.auto_detect_in_private:
            return False
        return True

    def _should_attempt_bilibili_detect(self, message_context) -> bool:
        candidate_texts = (
            message_context.processed_plain_text,
            message_context.plain_text,
            self._get_message_visible_text(message_context.raw_message),
        )
        for text in candidate_texts:
            if self._contains_bilibili_text_marker(text):
                return True
            if self._contains_bilibili_id_pattern(text):
                return True

        for segment in message_context.raw_segments:
            if self._segment_looks_like_bilibili_card(segment):
                return True
        return False

    def _contains_bilibili_text_marker(self, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        return any(marker.lower() in normalized for marker in self._BILIBILI_TEXT_MARKERS)

    @staticmethod
    def _contains_bilibili_id_pattern(text: str) -> bool:
        normalized = str(text or "")
        if not normalized:
            return False
        if "BV" in normalized or "bv" in normalized:
            return True
        lowered = normalized.lower()
        if "av" in lowered:
            return True
        return False

    def _segment_looks_like_bilibili_card(self, segment: dict[str, Any]) -> bool:
        if not isinstance(segment, dict):
            return False
        segment_type = str(segment.get("type", "") or "").strip().lower()
        if segment_type in {"json", "xml", "share"}:
            return True

        raw_data = segment.get("data")
        if isinstance(raw_data, str):
            if self._contains_bilibili_text_marker(raw_data) or self._contains_bilibili_id_pattern(raw_data):
                return True
        elif isinstance(raw_data, dict):
            flattened = " ".join(str(value) for value in raw_data.values() if value is not None)
            if self._contains_bilibili_text_marker(flattened) or self._contains_bilibili_id_pattern(flattened):
                return True
        return False

    async def _start_cleanup_task_after_delay(self):
        await self._cleanup_task_after_delay()

    async def _cleanup_task_after_delay(self):
        import asyncio

        await asyncio.sleep(10)
        max_age_min = self.config.cache.temp_file_max_age_min
        if max_age_min > 0:
            cleanup_old_temp_files(max_age_min)
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task(max_age_min))
        else:
            self._cleanup_task = None

    async def _periodic_cleanup_task(self, max_age_min: int):
        import asyncio

        cleanup_interval_min = max(5, min(30, max_age_min))
        while True:
            try:
                await asyncio.sleep(cleanup_interval_min * 60)
                cleanup_old_temp_files(max_age_min)
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    @staticmethod
    def _extract_command_text(message: Any) -> str:
        if message is None:
            return ""
        if isinstance(message, dict):
            return str(message.get("processed_plain_text", "") or message.get("plain_text", "") or "")
        plain_text = message.plain_text
        if isinstance(plain_text, str):
            return plain_text
        return str(message)

    @staticmethod
    def _get_message_plain_text(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("plain_text", "") or "")
        plain_text = message.plain_text
        return plain_text if isinstance(plain_text, str) else ""

    @staticmethod
    def _get_message_processed_text(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("processed_plain_text", "") or message.get("plain_text", "") or "")
        processed_plain_text = message.processed_plain_text if hasattr(message, "processed_plain_text") else None
        if isinstance(processed_plain_text, str):
            return processed_plain_text
        plain_text = message.plain_text
        return plain_text if isinstance(plain_text, str) else ""

    @staticmethod
    def _get_message_visible_text(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("processed_plain_text", "") or message.get("plain_text", "") or "")
        processed_plain_text = message.processed_plain_text if hasattr(message, "processed_plain_text") else None
        if isinstance(processed_plain_text, str) and processed_plain_text:
            return processed_plain_text
        plain_text = message.plain_text
        return plain_text if isinstance(plain_text, str) else ""

    @staticmethod
    def _rewrite_message_text(message: Any, rewritten_text: str) -> None:
        if isinstance(message, dict):
            message["plain_text"] = rewritten_text
            message["processed_plain_text"] = rewritten_text
            return
        message.plain_text = rewritten_text
        if hasattr(message, "processed_plain_text"):
            message.processed_plain_text = rewritten_text

def create_plugin() -> BilibiliVideoParserMaisakaPlugin:
    """创建插件实例。"""

    return BilibiliVideoParserMaisakaPlugin()
