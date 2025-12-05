# -*- coding: utf-8 -*-
"""
B站视频解析插件核心模块

本模块是插件核心功能的统一导出入口，汇集了所有核心组件的公开接口。

模块结构：
├── bilibili_api.py      - B站API封装（视频信息、字幕、下载）
├── video_parser.py      - 视频解析器（ffmpeg抽帧、音频提取）
├── cache_manager.py     - 缓存管理器（视频解析结果缓存）
├── video_analyzer.py    - 视频分析器（VLM帧分析）
├── doubao_analyzer.py   - 豆包视频分析器（视频理解模型）
├── builtin_vlm.py       - 内置VLM客户端（OpenAI/Gemini兼容）
├── handlers.py          - 事件处理器（自动检测/命令模式）
├── retry_utils.py       - 重试工具（错误分类、重试机制）
├── safe_delete.py       - 安全删除工具（临时文件管理）
└── services/            - 服务层
    ├── video_service.py   - 视频处理服务
    └── summary_service.py - 总结生成服务

导出的主要类和函数：
- BilibiliAPI: B站API封装类
- VideoParser: 视频解析器
- CacheManager: 缓存管理器
- VideoAnalyzer: 视频分析器
- DoubaoAnalyzer: 豆包视频分析器
- BuiltinVLMClient: 内置VLM客户端
- VideoService, VideoProcessResult: 视频处理服务
- SummaryService: 总结生成服务
- safe_delete_*: 安全删除函数
- retry_async, with_retry: 重试工具

使用示例：
    from .core import BilibiliAPI, VideoService, CacheManager
    
    # 提取视频ID
    video_info = BilibiliAPI.extract_video_id("BV1xx411c7mD")
    
    # 获取视频信息
    info = await BilibiliAPI.get_video_info(video_id)

Author: 约瑟夫.k && 白泽
"""
from .bilibili_api import BilibiliAPI
from .video_parser import VideoParser
from .cache_manager import CacheManager
from .video_analyzer import VideoAnalyzer
from .doubao_analyzer import DoubaoAnalyzer
from .builtin_vlm import BuiltinVLMClient
from .services import VideoService, VideoProcessResult, SummaryService
from .safe_delete import (
    safe_delete_temp_file,
    safe_delete_temp_dir,
    cleanup_temp_files,
    cleanup_old_temp_files,
    init_temp_dir,
    get_temp_dir,
    get_temp_subdir,
)
from .retry_utils import (
    ErrorType,
    RetryableError,
    NonRetryableError,
    classify_bilibili_error,
    classify_http_error,
    get_friendly_error_message,
    retry_async,
    with_retry,
)

__all__ = [
    'BilibiliAPI',
    'VideoParser',
    'CacheManager',
    'VideoAnalyzer',
    'DoubaoAnalyzer',
    'BuiltinVLMClient',
    'VideoService',
    'VideoProcessResult',
    'SummaryService',
    'safe_delete_temp_file',
    'safe_delete_temp_dir',
    'cleanup_temp_files',
    'cleanup_old_temp_files',
    'init_temp_dir',
    'get_temp_dir',
    'get_temp_subdir',
    # 重试工具
    'ErrorType',
    'RetryableError',
    'NonRetryableError',
    'classify_bilibili_error',
    'classify_http_error',
    'get_friendly_error_message',
    'retry_async',
    'with_retry',
]