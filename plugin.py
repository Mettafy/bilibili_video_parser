# -*- coding: utf-8 -*-
"""
B站视频内容解析插件 - 插件主入口模块

本模块是B站视频解析插件的主入口，负责：
1. 插件注册和生命周期管理
2. 配置Schema定义和管理
3. 组件（Handler/Command）的注册
4. 定时清理任务的启动和停止

插件功能概述：
- 自动检测模式：检测消息中的B站链接，自动解析视频内容
- 命令模式：通过 /bili 命令手动触发视频解析
- 支持多种视觉分析方式：MaiBot VLM、插件内置VLM、豆包视频模型
- 支持字幕获取和ASR语音识别
- 支持多P视频和合集

主要类：
- BilibiliVideoParserPlugin: 插件主类，继承自BasePlugin

配置节：
- plugin: 插件基本信息
- trigger: 触发方式配置
- video: 全局视频配置
- analysis: 视觉分析配置
- analysis.default: Default模式配置（使用MaiBot VLM）
- analysis.builtin: Builtin模式配置（使用插件内置VLM）
- analysis.doubao: Doubao模式配置（使用豆包视频模型）
- cache: 缓存配置

依赖模块：
- core.handlers: 事件处理器
- core.cache_manager: 缓存管理
- core.video_parser: 视频解析（ffmpeg）
- core.video_analyzer: 视频分析（VLM）
- core.safe_delete: 安全文件删除

Author: 约瑟夫.k && 白泽
Version: 1.0.0
"""
import asyncio
from typing import List, Tuple, Type, Optional
from pathlib import Path

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    ComponentInfo,
    ConfigField,
    get_logger,
)

from .core.handlers import BilibiliAutoDetectHandler, BilibiliCommandHandler
from .core.cache_manager import CacheManager
from .core.video_parser import VideoParser
from .core.video_analyzer import VideoAnalyzer
from .core.safe_delete import init_temp_dir, cleanup_old_temp_files

logger = get_logger("bilibili_video_parser")


@register_plugin
class BilibiliVideoParserPlugin(BasePlugin):
    """B站视频内容解析插件"""

    # 插件基本信息
    plugin_name: str = "bilibili_video_parser"
    enable_plugin: bool = False
    dependencies: List[str] = []
    python_dependencies: List[str] = ["aiohttp>=3.8.0"]
    config_file_name: str = "config.toml"

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本信息",
        "trigger": "触发方式配置",
        "summary": "总结生成配置",
        "video": "视频处理配置",
        "analysis": "视觉分析配置",
        "analysis.default": "Default模式配置（使用MaiBot VLM）",
        "analysis.builtin": "Builtin模式配置（使用插件内置VLM，支持动态参数传递）",
        "analysis.doubao": "Doubao模式配置（使用豆包视频模型，支持动态参数传递）",
    }

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(
                type=str,
                default="3.1.0",
                description="配置文件版本"
            ),
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用插件"
            ),
        },
        "trigger": {
            "auto_detect_enabled": ConfigField(
                type=bool,
                default=True,
                description="是否自动检测B站链接（用户发送链接时自动解析）"
            ),
            "command_enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用命令触发（/bili 命令）"
            ),
        },
        "summary": {
            "enable_summary": ConfigField(
                type=bool,
                default=True,
                description="是否生成最终总结。开启时会调用模型生成总结；关闭时直接将原生视频信息发送给回复系统"
            ),
            "summary_max_chars": ConfigField(
                type=int,
                default=200,
                description="总结最大字数。范围60-6000，小数会自动取整，超出范围使用默认值200"
            ),
        },
        "video": {
            "max_duration_min": ConfigField(
                type=float,
                default=60.0,
                description="视频最大时长(分钟)，超过此时长的视频将被跳过不处理"
            ),
            "max_size_mb": ConfigField(
                type=int,
                default=300,
                description="视频最大文件大小(MB)，超过此大小的视频将被跳过"
            ),
            "sessdata": ConfigField(
                type=str,
                default="",
                description="B站SESSDATA Cookie（可选，用于获取字幕）。不填写时将跳过字幕获取"
            ),
            "enable_asr": ConfigField(
                type=bool,
                default=False,
                description="是否启用ASR语音识别（可选）。开启后会从视频音轨中提取语音进行识别，作为字幕的补充"
            ),
            "cache_enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用视频解析结果缓存。开启后，相同视频不会重复解析"
            ),
            "temp_file_max_age_min": ConfigField(
                type=int,
                default=60,
                description="临时文件最大保留时间（分钟）。设为0表示处理完成后立即删除"
            ),
            "retry_max_attempts": ConfigField(
                type=int,
                default=3,
                description="网络请求最大重试次数"
            ),
            "retry_interval_sec": ConfigField(
                type=float,
                default=2.0,
                description="网络请求重试间隔（秒）"
            ),
        },
        "analysis": {
            "visual_method": ConfigField(
                type=str,
                default="default",
                description="视觉分析方式：default（使用MaiBot主程序的VLM）、builtin（使用插件内置VLM）、doubao（使用豆包视频模型）或 none（不进行视觉分析）"
            ),
        },
        "analysis.default": {
            "visual_max_duration_min": ConfigField(
                type=float,
                default=10.0,
                description="进行视觉分析的最大视频时长(分钟)。超过此时长的视频将只使用字幕+ASR+视频信息，不进行视觉分析"
            ),
            "frame_interval_sec": ConfigField(
                type=int,
                default=10,
                description="抽帧间隔(秒)，每隔多少秒抽取一帧。系统会根据视频时长和此间隔自动计算抽帧数量（最多10帧）"
            ),
        },
        "analysis.builtin": {
            "visual_max_duration_min": ConfigField(
                type=float,
                default=10.0,
                description="进行视觉分析的最大视频时长(分钟)。超过此时长的视频将只使用字幕+ASR+视频信息，不进行视觉分析"
            ),
            "frame_interval_sec": ConfigField(
                type=int,
                default=10,
                description="抽帧间隔(秒)，每隔多少秒抽取一帧。系统会根据视频时长和此间隔自动计算抽帧数量（最多10帧）"
            ),
            "client_type": ConfigField(
                type=str,
                default="openai",
                description="API服务类型：openai（兼容OpenAI格式）或 gemini（Google Gemini格式）"
            ),
            "base_url": ConfigField(
                type=str,
                default="https://api.siliconflow.cn/v1",
                description="API基础URL"
            ),
            "api_key": ConfigField(
                type=str,
                default="",
                description="API密钥"
            ),
            "model": ConfigField(
                type=str,
                default="Qwen/Qwen2.5-VL-72B-Instruct",
                description="模型标识符（API服务商提供的模型ID）"
            ),
            "timeout": ConfigField(
                type=int,
                default=60,
                description="请求超时时间（秒）"
            ),
            "max_retries": ConfigField(
                type=int,
                default=2,
                description="最大重试次数"
            ),
            "retry_interval": ConfigField(
                type=int,
                default=5,
                description="重试间隔时间（秒）"
            ),
            "frame_prompt": ConfigField(
                type=str,
                default="",
                description="自定义帧分析提示词（留空使用默认提示词）"
            ),
        },
        "analysis.doubao": {
            "visual_max_duration_min": ConfigField(
                type=float,
                default=10.0,
                description="进行视觉分析的最大视频时长(分钟)。超过此时长的视频将只使用字幕+ASR+视频信息，不进行视觉分析"
            ),
            "api_key": ConfigField(
                type=str,
                default="",
                description="豆包API密钥（也可通过环境变量 ARK_API_KEY 设置）"
            ),
            "model_id": ConfigField(
                type=str,
                default="doubao-seed-1-6-flash-250828",
                description="豆包模型ID"
            ),
            "fps": ConfigField(
                type=float,
                default=1.0,
                description="抽帧频率（0.2-5），值越高理解越精细但token消耗越大"
            ),
            "base_url": ConfigField(
                type=str,
                default="https://ark.cn-beijing.volces.com/api/v3",
                description="豆包API基础URL"
            ),
            "timeout": ConfigField(
                type=int,
                default=120,
                description="请求超时时间（秒）"
            ),
            "max_retries": ConfigField(
                type=int,
                default=2,
                description="最大重试次数"
            ),
            "retry_interval": ConfigField(
                type=int,
                default=10,
                description="重试间隔时间（秒）"
            ),
            "video_prompt": ConfigField(
                type=str,
                default="",
                description="自定义视频分析提示词（留空使用默认提示词）"
            ),
        },
    }

    def __init__(self, plugin_dir: str, **kwargs):
        super().__init__(plugin_dir, **kwargs)
        
        # 初始化管理器实例（在__init__中初始化，确保get_plugin_components可以使用）
        plugin_path = Path(__file__).parent
        data_dir = plugin_path / "data"
        
        # 初始化目录结构
        # data/
        # ├── index.json          # 缓存索引
        # ├── cache/              # 视频解析结果缓存
        # │   └── *.json          # 每个视频的缓存数据
        # └── temp/               # 临时文件目录
        #     ├── videos/         # 临时视频文件
        #     ├── frames/         # 临时帧图片目录
        #     └── audio/          # 临时音频文件
        
        # 初始化安全删除模块的临时目录
        init_temp_dir(str(data_dir))
        
        self.cache_manager = CacheManager(str(data_dir))
        self.video_parser = VideoParser(data_dir=str(data_dir))
        
        # 获取VLM配置（根据visual_method决定使用哪个配置）
        vlm_config = self._get_vlm_config()
        self.video_analyzer = VideoAnalyzer(vlm_config=vlm_config)  # 采用懒加载，首次使用时自动初始化
        
        # 检查ffmpeg（同步操作）
        if self.video_parser.check_ffmpeg():
            logger.debug("[BilibiliVideoParser] ffmpeg检查成功")
        else:
            logger.warning("[BilibiliVideoParser] ffmpeg不可用，视频解析功能将受限")
        
        # 定时清理任务句柄
        self._cleanup_task: Optional[asyncio.Task] = None
        
        # 启动定时清理任务（延迟启动，确保插件完全初始化）
        asyncio.create_task(self._start_cleanup_task_after_delay())
        
        logger.info("[BilibiliVideoParser] 插件初始化完成")
    
    def _get_vlm_config(self) -> dict:
        """获取VLM配置（根据visual_method决定使用哪个配置）
        
        对于builtin模式，采用动态参数传递策略：
        - 只传递用户在配置文件中实际定义的参数
        - 不同API服务商可能支持不同的参数
        - 用户可以自由添加服务商特有的参数
        """
        visual_method = self.get_config("analysis.visual_method", "default")
        
        # 基础配置
        config = {
            "visual_method": visual_method,
        }
        
        if visual_method == "builtin":
            # 使用插件内置VLM配置
            # 动态获取所有builtin配置项，让用户自由定义参数
            config["use_builtin"] = True
            
            # 必需参数（有默认值）
            config["client_type"] = self.get_config("analysis.builtin.client_type", "openai")
            config["base_url"] = self.get_config("analysis.builtin.base_url", "https://api.siliconflow.cn/v1")
            config["api_key"] = self.get_config("analysis.builtin.api_key", "")
            config["model"] = self.get_config("analysis.builtin.model", "Qwen/Qwen2.5-VL-72B-Instruct")
            config["timeout"] = self.get_config("analysis.builtin.timeout", 60)
            config["max_retries"] = self.get_config("analysis.builtin.max_retries", 2)
            config["retry_interval"] = self.get_config("analysis.builtin.retry_interval", 5)
            config["frame_prompt"] = self.get_config("analysis.builtin.frame_prompt", "")
            
            # 动态参数：只有用户配置了才传递
            # 这些参数不同服务商可能不支持，所以不设默认值
            optional_params = ["temperature", "max_tokens", "top_p", "top_k", "presence_penalty", "frequency_penalty"]
            for param in optional_params:
                value = self.get_config(f"analysis.builtin.{param}", None)
                if value is not None:
                    config[param] = value
            
            # 获取所有用户自定义的额外参数（服务商特有参数）
            # 通过遍历配置获取所有analysis.builtin.*的配置项
            builtin_config = self.get_config("analysis.builtin", {})
            if isinstance(builtin_config, dict):
                known_params = {
                    "visual_max_duration_min", "frame_interval_sec", "client_type",
                    "base_url", "api_key", "model", "timeout", "max_retries",
                    "retry_interval", "frame_prompt"
                } | set(optional_params)
                
                for key, value in builtin_config.items():
                    if key not in known_params and value is not None:
                        # 用户自定义的额外参数，直接传递
                        config[key] = value
        else:
            # 使用MaiBot VLM（default模式）或不使用VLM（doubao/none模式）
            config["use_builtin"] = False
        
        return config

    async def _start_cleanup_task_after_delay(self):
        """延迟启动定时清理任务
        
        在插件初始化完成后，延迟10秒再启动定时任务，确保插件完全初始化
        后再开始定时任务，避免初始化过程中的竞争条件。
        """
        await asyncio.sleep(10)
        
        # 获取临时文件最大保留时间配置
        max_age_min = self.get_config("video.temp_file_max_age_min", 60)
        
        if max_age_min > 0:
            # 启动时执行一次清理
            cleanup_old_temp_files(max_age_min)
            
            # 启动定时清理任务
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task(max_age_min))
            
            # 动态计算清理间隔：最小5分钟，最大30分钟
            cleanup_interval_min = max(5, min(30, max_age_min))
            logger.info(f"[BilibiliVideoParser] 定时清理任务已启动（间隔{cleanup_interval_min}分钟，保留{max_age_min}分钟内的临时文件）")
        else:
            logger.info("[BilibiliVideoParser] 临时文件即时删除模式已启用")
    
    async def _periodic_cleanup_task(self, max_age_min: int):
        """定时清理任务
        
        清理间隔动态调整：
        - 最小5分钟，最大30分钟
        - 默认等于 max_age_min
        
        Args:
            max_age_min: 文件最大保留时间（分钟）
        """
        # 动态计算清理间隔：最小5分钟，最大30分钟
        cleanup_interval_min = max(5, min(30, max_age_min))
        
        while True:
            try:
                # 等待清理间隔
                await asyncio.sleep(cleanup_interval_min * 60)
                
                # 执行清理（cleanup_old_temp_files内部会记录info日志）
                cleanup_old_temp_files(max_age_min)
                
            except asyncio.CancelledError:
                logger.debug("[BilibiliVideoParser] 定时清理任务已取消")
                break
            except Exception as e:
                logger.error(f"[BilibiliVideoParser] 定时清理任务异常: {e}")
                # 继续运行，不因异常退出

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """获取插件组件列表"""
        components = []
        
        # 注册自动检测处理器
        if self.get_config("trigger.auto_detect_enabled", True):
            auto_detect_handler = BilibiliAutoDetectHandler
            # 传递管理器实例
            auto_detect_handler.cache_manager = self.cache_manager
            auto_detect_handler.video_parser = self.video_parser
            auto_detect_handler.video_analyzer = self.video_analyzer
            components.append((
                auto_detect_handler.get_handler_info(),
                auto_detect_handler
            ))
        
        # 注册命令处理器（BaseCommand，使用intercept_message_level=1让消息对replyer可见）
        if self.get_config("trigger.command_enabled", True):
            command_handler = BilibiliCommandHandler
            # 传递管理器实例
            command_handler.cache_manager = self.cache_manager
            command_handler.video_parser = self.video_parser
            command_handler.video_analyzer = self.video_analyzer
            components.append((
                command_handler.get_command_info(),  # BaseCommand使用get_command_info
                command_handler
            ))
        
        return components