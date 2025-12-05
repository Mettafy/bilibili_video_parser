# -*- coding: utf-8 -*-
"""
服务层模块

本模块是插件服务层的统一导出入口，提供高层次的业务逻辑封装。

服务层设计理念：
- 将复杂的业务流程封装为简单的服务接口
- 协调多个底层模块（API、解析器、分析器等）
- 提供统一的错误处理和结果封装

模块结构：
├── video_service.py    - 视频处理服务
└── summary_service.py  - 总结生成服务

VideoService（视频处理服务）：
- 封装视频处理的完整流程
- 协调视频下载、抽帧、字幕获取、ASR等
- 支持多种视觉分析方式（VLM、豆包）
- 返回 VideoProcessResult 结果对象

SummaryService（总结生成服务）：
- 封装总结生成的完整流程
- 支持多种总结模式（VLM帧分析、豆包、纯文本）
- 生成个性化回复（结合麦麦人设）
- 构建原生信息文本

导出的类：
- VideoService: 视频处理服务
- VideoProcessResult: 视频处理结果数据类
- SummaryService: 总结生成服务

使用示例：
    from .core.services import VideoService, SummaryService
    
    # 视频处理
    video_service = VideoService(video_parser, get_config)
    result = await video_service.process_video(video_id, BilibiliAPI)
    
    # 总结生成
    summary_service = SummaryService(video_analyzer, get_config)
    summary_result = await summary_service.generate_summary(
        frame_paths=result.frame_paths,
        video_info={...},
        text_content=result.get_text_content()
    )

Author: 约瑟夫.k && 白泽
"""
from .video_service import VideoService, VideoProcessResult
from .summary_service import SummaryService

__all__ = ['VideoService', 'VideoProcessResult', 'SummaryService']