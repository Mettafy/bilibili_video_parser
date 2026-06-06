"""Maisaka 版 B 站视频解析插件配置模型。"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    config_version: str = Field(default="4.2.0", description="插件配置版本")
    enabled: bool = Field(default=True, description="是否启用插件")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="插件日志级别",
    )
    debug: bool = Field(default=False, description="是否启用调试日志")


class TriggerSectionConfig(PluginConfigBase):
    """入口触发配置。"""

    __ui_label__ = "触发"
    __ui_icon__ = "zap"
    __ui_order__ = 1

    auto_detect_enabled: bool = Field(default=True, description="是否启用自动检测")
    command_enabled: bool = Field(default=True, description="是否启用 /bili 命令")
    auto_detect_in_groups: bool = Field(default=True, description="是否在群聊中启用自动检测")
    auto_detect_in_private: bool = Field(default=True, description="是否在私聊中启用自动检测")


class TriggerTimeoutSectionConfig(PluginConfigBase):
    """入口链总预算配置。"""

    __ui_label__ = "触发超时"
    __ui_icon__ = "timer-reset"
    __ui_order__ = 2

    auto_detect_total_timeout_sec: int = Field(default=600, description="自动检测链总超时（秒）")
    command_total_timeout_sec: int = Field(default=600, description="/bili 命令链总超时（秒）")


class AutoDetectSectionConfig(PluginConfigBase):
    """自动检测主链配置。"""

    __ui_label__ = "自动检测"
    __ui_icon__ = "scan-search"
    __ui_order__ = 2

    enable_summary: bool = Field(default=True, description="自动检测时是否生成总结")


class CommandSectionConfig(PluginConfigBase):
    """工具型命令链配置。"""

    __ui_label__ = "命令"
    __ui_icon__ = "terminal-square"
    __ui_order__ = 3

    show_processing_message: bool = Field(default=True, description="命令链是否显示处理中提示")
    allow_force_visual: bool = Field(default=True, description="命令链是否允许 --force-visual")
    allow_force_asr: bool = Field(default=True, description="命令链是否允许 --force-asr")
    allow_extra_arguments: bool = Field(default=True, description="命令链是否允许额外参数")


class VideoSectionConfig(PluginConfigBase):
    """B 站网络与媒体处理配置。"""

    __ui_label__ = "视频"
    __ui_icon__ = "film"
    __ui_order__ = 4

    max_duration_min: float = Field(default=60.0, description="允许处理的视频最大时长（分钟）")
    max_size_mb: int = Field(default=300, description="允许下载的视频最大体积（MB）")
    sessdata: str = Field(default="", description="B 站 SESSDATA，用于获取更完整字幕")
    ffmpeg_path: str = Field(default="", description="ffmpeg 路径，留空时自动检测")
    api_timeout_sec: int = Field(default=15, description="常规 B 站 API 超时")
    short_url_timeout_sec: int = Field(default=15, description="短链解析超时")
    subtitle_timeout_sec: int = Field(default=20, description="字幕请求超时")
    download_url_timeout_sec: int = Field(default=20, description="下载地址请求超时")
    download_timeout_sec: int = Field(default=300, description="视频下载超时")
    ffmpeg_probe_timeout_sec: int = Field(default=20, description="ffmpeg 探测超时")
    ffmpeg_extract_audio_timeout_sec: int = Field(default=120, description="提取音频超时")
    ffmpeg_extract_frames_timeout_sec: int = Field(default=180, description="抽帧超时")
    media_pipeline_timeout_sec: int = Field(default=360, description="媒体总流程超时")
    retry_max_attempts: int = Field(default=3, description="B 站请求最大重试次数")
    retry_interval_sec: float = Field(default=2.0, description="B 站请求重试间隔")


class AnalysisHostSectionConfig(PluginConfigBase):
    """宿主视觉任务配置。"""

    __ui_label__ = "宿主视觉模型"
    __ui_icon__ = "bot"
    __ui_order__ = 6

    host_task_name: str = Field(default="vlm", description="宿主视觉任务名")
    temperature: float = Field(default=0.2, description="宿主视觉请求温度")
    max_tokens: int = Field(default=4000, description="宿主视觉请求最大输出")
    frame_prompt: str = Field(default="", description="自定义关键帧分析提示词")


class HostTimeoutSectionConfig(PluginConfigBase):
    """宿主能力调用超时预算。"""

    __ui_label__ = "宿主超时"
    __ui_icon__ = "timer"
    __ui_order__ = 7

    enabled: bool = Field(default=True, description="是否启用宿主能力调用超时覆盖")
    task_request_timeout_min: float = Field(default=5.0, description="宿主能力单次请求超时（分钟）")

    @field_validator("task_request_timeout_min")
    @classmethod
    def validate_task_request_timeout_min(cls, value: float) -> float:
        normalized_value = float(value)
        if normalized_value <= 0:
            raise ValueError("宿主能力单次请求超时必须大于 0 分钟")
        return normalized_value


class AnalysisSectionConfig(PluginConfigBase):
    """视觉分析总策略。"""

    __ui_label__ = "视觉策略"
    __ui_icon__ = "image"
    __ui_order__ = 5

    visual_method: Literal["host", "none"] = Field(
        default="host",
        description="视觉分析方式",
    )
    visual_max_duration_min: float = Field(default=10.0, description="允许视觉分析的视频最大时长（分钟）")
    max_frames: int = Field(default=5, description="最大抽帧数")
    lock_even_frames: bool = Field(default=True, description="是否锁定为等距抽帧")
    frame_interval_sec: int = Field(default=10, description="非等距模式的抽帧间隔")
    parallel_frame_analysis: bool = Field(default=False, description="是否并发分析关键帧")
    parallel_frame_analysis_limit: int = Field(default=2, description="关键帧并发分析上限")

    host: AnalysisHostSectionConfig = Field(default_factory=AnalysisHostSectionConfig)


class AsrSectionConfig(PluginConfigBase):
    """插件自带 ASR 配置。"""

    __ui_label__ = "插件独立 ASR"
    __ui_icon__ = "mic"
    __ui_order__ = 9

    enabled: bool = Field(default=False, description="是否启用插件自带 ASR")
    provider_type: Literal["openai_compatible"] = Field(default="openai_compatible", description="ASR 供应商类型")
    base_url: str = Field(default="", description="ASR 服务地址")
    api_key: str = Field(default="", description="ASR API 密钥")
    model: str = Field(default="whisper-1", description="ASR 模型名")
    timeout_sec: int = Field(default=120, description="ASR 请求超时")
    max_retries: int = Field(default=2, description="ASR 最大重试次数")
    retry_interval_sec: float = Field(default=5.0, description="ASR 重试间隔")
    max_audio_duration_min: float = Field(default=30.0, description="允许 ASR 的最大音频时长（分钟）")
    language: str = Field(default="zh", description="ASR 语言")
    prompt: str = Field(default="", description="ASR 识别提示词")
    fallback_to_subtitle_only: bool = Field(default=True, description="ASR 失败时是否仅保留字幕")


class SummarySectionConfig(PluginConfigBase):
    """总结生成配置。"""

    __ui_label__ = "总结"
    __ui_icon__ = "file-text"
    __ui_order__ = 10

    enabled: bool = Field(default=True, description="是否启用总结生成")
    host_task_name: str = Field(default="replyer", description="宿主总结任务名")
    temperature: float = Field(default=0.4, description="总结请求温度")
    max_tokens: int = Field(default=7000, description="总结请求最大输出")
    summary_max_chars: int = Field(default=200, description="总结目标字数提示，仅用于 prompt")
    fallback_to_raw_info: bool = Field(default=True, description="总结失败时是否回退原始信息")
    custom_prompt: str = Field(default="", description="自定义总结提示词")


class CacheSectionConfig(PluginConfigBase):
    """缓存与清理配置。"""

    __ui_label__ = "缓存"
    __ui_icon__ = "database"
    __ui_order__ = 11

    enabled: bool = Field(default=True, description="是否启用缓存")
    cache_summary: bool = Field(default=True, description="是否缓存总结")
    cache_raw_info: bool = Field(default=True, description="是否缓存原始信息")
    cache_visual_result: bool = Field(default=True, description="是否缓存视觉分析结果")
    cache_subtitle: bool = Field(default=True, description="是否缓存字幕")
    cache_asr: bool = Field(default=True, description="是否缓存 ASR")
    temp_file_max_age_min: int = Field(default=60, description="临时文件最大保留时间（分钟）")
    cleanup_on_success: bool = Field(default=True, description="处理成功后是否主动清理临时文件")


class BilibiliVideoParserConfig(PluginConfigBase):
    """B 站视频解析插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    trigger: TriggerSectionConfig = Field(default_factory=TriggerSectionConfig)
    trigger_timeout: TriggerTimeoutSectionConfig = Field(default_factory=TriggerTimeoutSectionConfig)
    video: VideoSectionConfig = Field(default_factory=VideoSectionConfig)
    analysis: AnalysisSectionConfig = Field(default_factory=AnalysisSectionConfig)
    host_timeout: HostTimeoutSectionConfig = Field(default_factory=HostTimeoutSectionConfig)
    auto_detect: AutoDetectSectionConfig = Field(default_factory=AutoDetectSectionConfig)
    command: CommandSectionConfig = Field(default_factory=CommandSectionConfig)
    asr: AsrSectionConfig = Field(default_factory=AsrSectionConfig)
    summary: SummarySectionConfig = Field(default_factory=SummarySectionConfig)
    cache: CacheSectionConfig = Field(default_factory=CacheSectionConfig)

    model_config = PluginConfigBase.model_config | {"populate_by_name": True}
