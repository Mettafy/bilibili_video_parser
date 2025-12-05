# -*- coding: utf-8 -*-
"""
事件处理器模块 - 自动检测模式和命令模式

本模块实现了插件的两种触发模式：
1. 自动检测模式（BilibiliAutoDetectHandler）
2. 命令模式（BilibiliCommandHandler）

自动检测模式：
- 继承自 BaseEventHandler
- 监听 ON_MESSAGE 事件
- 自动检测消息中的B站链接
- 处理视频后将信息添加到消息中，交给主回复系统
- 支持 enable_summary 开关控制是否生成总结
- 静默失败，不影响正常消息流程

命令模式：
- 继承自 BaseCommand
- 响应 /bili 命令
- 处理视频后直接生成个性化回复
- 使用 intercept_message_level=1，让消息对replyer可见但不触发回复
- 失败时返回友好的错误提示

主要类：
- BilibiliAutoDetectHandler: 自动检测处理器
- BilibiliCommandHandler: 命令处理器

处理流程：
1. 提取视频ID和分P号
2. 检查缓存
3. 获取视频信息
4. 下载视频（如需要）
5. 抽帧/视觉分析
6. 获取字幕/ASR
7. 生成总结或个性化回复
8. 保存缓存
9. 清理临时文件

错误处理：
- 自动检测模式：静默失败，返回None让主回复系统处理原始消息
- 命令模式：返回友好的错误提示给用户

依赖：
- bilibili_api: B站API封装
- cache_manager: 缓存管理
- video_parser: 视频解析
- video_analyzer: 视频分析
- services.video_service: 视频处理服务
- services.summary_service: 总结生成服务
- retry_utils: 重试工具

Author: 约瑟夫.k && 白泽
"""
import re
from typing import Tuple, Optional, TYPE_CHECKING
from src.plugin_system import (
    BaseEventHandler,
    BaseCommand,
    EventType,
    MaiMessages,
    get_logger,
)

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages
from .bilibili_api import BilibiliAPI
from .cache_manager import CacheManager
from .video_parser import VideoParser
from .video_analyzer import VideoAnalyzer
from .services.video_service import VideoService
from .services.summary_service import SummaryService
from .retry_utils import (
    ErrorType,
    NonRetryableError,
    get_friendly_error_message,
)

logger = get_logger("bilibili_handlers")


class BilibiliAutoDetectHandler(BaseEventHandler):
    """B站链接自动检测处理器
    
    自动检测模式：
    - 检测到B站链接后，处理视频并生成总结
    - 将总结添加到消息中，交给主回复系统决定是否回复
    """
    
    event_type = EventType.ON_MESSAGE
    handler_name = "bilibili_auto_detect"
    handler_description = "自动检测消息中的B站链接并解析"
    weight = 50
    intercept_message = True

    # 这些属性由plugin在注册前设置
    cache_manager: Optional[CacheManager] = None
    video_parser: Optional[VideoParser] = None
    video_analyzer: Optional[VideoAnalyzer] = None

    async def execute(
        self,
        message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], None, Optional[MaiMessages]]:
        """执行自动检测
        
        Returns:
            Tuple[bool, bool, Optional[str], None, Optional[MaiMessages]]:
            (是否执行成功, 是否需要继续处理, 可选的返回消息, None, 可选的修改后消息)
        """
        try:
            if not message or not message.plain_text:
                return True, True, None, None, None
            
            # 检查是否启用自动检测
            if not self.get_config("trigger.auto_detect_enabled", True):
                return True, True, None, None, None
            
            # 检查消息是否已被命令处理器处理过（避免重复处理）
            if message.plain_text.startswith("[视频解析]"):
                logger.debug("[BilibiliAutoDetect] 消息已被命令处理器处理，跳过")
                return True, True, None, None, None
            
            # 检查消息是否包含视频总结标记（避免重复处理）
            if "关于这个B站视频《" in message.plain_text:
                logger.debug("[BilibiliAutoDetect] 消息已包含视频总结，跳过")
                return True, True, None, None, None
            
            # 提取视频ID和分P号
            video_info = BilibiliAPI.extract_video_id(message.plain_text)
            if not video_info:
                return True, True, None, None, None
            
            video_type, video_id, page = video_info
            
            # 如果是短链接，需要先解析
            if video_type == 'short':
                resolved = await BilibiliAPI.resolve_short_url(video_id)
                if not resolved:
                    logger.warning(f"[BilibiliAutoDetect] 短链接解析失败: {video_id}")
                    return True, True, None, None, None
                video_id, page = resolved
                video_type = 'bv' if video_id.startswith('BV') else 'av'
            
            if page > 1:
                logger.info(f"[BilibiliAutoDetect] 检测到B站视频: {video_id} P{page}")
            else:
                logger.info(f"[BilibiliAutoDetect] 检测到B站视频: {video_id}")
            
            # 检查video_analyzer是否初始化
            if not self.video_analyzer or not self.video_analyzer.is_initialized():
                logger.warning("[BilibiliAutoDetect] 视频分析器未初始化，跳过处理")
                return True, True, None, None, None
            
            # 处理视频并修改消息
            modified_message = await self._process_video_auto_detect(message, video_id, page)
            
            # 返回修改后的消息，让MaiBot的主回复系统处理
            return True, True, None, None, modified_message
            
        except Exception as e:
            logger.error(f"[BilibiliAutoDetect] 执行异常: {e}")
            return True, True, None, None, None

    async def _process_video_auto_detect(
        self,
        message: MaiMessages,
        video_id: str,
        page: int = 1
    ) -> Optional[MaiMessages]:
        """自动检测模式的视频处理流程
        
        根据 enable_summary 配置决定流程：
        - enable_summary=true: 生成80-120字总结后发送给主回复系统
        - enable_summary=false: 直接将原生视频信息发送给主回复系统
        
        Args:
            message: 消息对象
            video_id: 视频ID
            page: 分P号（从1开始）
        """
        video_service = None
        summary_service = None
        process_result = None
        
        logger.debug(f"[BilibiliAutoDetect] 开始处理视频: video_id={video_id}, page={page}")
        
        try:
            # 获取是否启用总结的配置
            enable_summary = self.get_config("analysis.enable_summary", True)
            logger.debug(f"[BilibiliAutoDetect] enable_summary={enable_summary}")
            
            # 构建缓存key（包含分P号）
            cache_key = f"{video_id}_p{page}" if page > 1 else video_id
            logger.debug(f"[BilibiliAutoDetect] 缓存key: {cache_key}")
            
            # 检查缓存
            if self.get_config("video.cache_enabled", True) and self.cache_manager:
                cached = self.cache_manager.get_cache(cache_key)
                if cached:
                    title = cached.get('title', '')
                    author = cached.get('author', '')
                    description = cached.get('description', '')
                    duration = cached.get('duration')
                    total_duration = cached.get('total_duration')
                    cached_page = cached.get('page', page)
                    cached_page_title = cached.get('page_title', '')
                    cached_total_pages = cached.get('total_pages', 1)
                    
                    if enable_summary:
                        # 启用总结模式：使用缓存的总结
                        summary = cached.get('summary', '')
                        if summary:
                            video_info_text = self._build_video_info_text(
                                title=title,
                                author=author,
                                description=description,
                                summary=summary,
                                page=cached_page,
                                page_title=cached_page_title,
                                total_pages=cached_total_pages,
                                duration=duration,
                                total_duration=total_duration
                            )
                            new_text = f"{message.plain_text}\n\n{video_info_text}"
                            message.modify_plain_text(new_text)
                            return message
                    else:
                        # 不启用总结模式：使用缓存的原生信息
                        raw_info = cached.get('raw_info', {})
                        if raw_info:
                            video_info = {
                                'title': title,
                                'author': author,
                                'description': description,
                                'duration': duration,
                                'total_duration': total_duration,
                                'page': cached_page,
                                'page_title': cached_page_title,
                                'total_pages': cached_total_pages
                            }
                            summary_service = SummaryService(self.video_analyzer, self.get_config)
                            video_info_text = summary_service.build_raw_info_text(video_info, raw_info)
                            new_text = f"{message.plain_text}\n\n{video_info_text}"
                            message.modify_plain_text(new_text)
                            return message
            
            # 创建服务实例
            logger.debug("[BilibiliAutoDetect] 创建服务实例...")
            video_service = VideoService(self.video_parser, self.get_config)
            summary_service = SummaryService(self.video_analyzer, self.get_config)
            
            # 步骤1: 处理视频（下载、抽帧、获取字幕/ASR）
            logger.debug("[BilibiliAutoDetect] 步骤1: 开始处理视频...")
            try:
                process_result = await video_service.process_video(video_id, BilibiliAPI, page)
            except NonRetryableError as e:
                # 不可重试的错误（如视频不存在），静默失败，返回None让主回复系统处理原始消息
                logger.warning(f"[BilibiliAutoDetect] 视频处理失败（不可重试）: {e}")
                return None
            except Exception as e:
                # 其他异常，静默失败
                logger.error(f"[BilibiliAutoDetect] 视频处理异常: {e}")
                return None
            
            if not process_result.success:
                # 处理失败，静默返回None，让主回复系统处理原始消息
                logger.warning(f"[BilibiliAutoDetect] 视频处理失败: {process_result.error}")
                return None
            
            # 构建视频信息字典
            video_info = {
                'title': process_result.title,
                'description': process_result.description,
                'author': process_result.author,
                'duration': process_result.duration,
                'total_duration': process_result.total_duration,
                'video_id': video_id,
                'page': process_result.page,
                'page_title': process_result.page_title,
                'total_pages': process_result.total_pages,
            }
            
            # 获取帧描述（如果有帧的话）
            # 硬编码限制：最多分析5帧，避免过多API调用
            frame_descriptions = []
            MAX_ANALYZE_FRAMES = 5  # 硬编码：最大VLM分析帧数
            if process_result.frame_paths and self.video_analyzer and self.video_analyzer.is_initialized():
                max_analyze_frames = min(len(process_result.frame_paths), MAX_ANALYZE_FRAMES)
                for idx, frame_path in enumerate(process_result.frame_paths[:max_analyze_frames], start=1):
                    logger.debug(f"[BilibiliAutoDetect] 分析第 {idx}/{max_analyze_frames} 帧")
                    desc = await self.video_analyzer.analyze_frame(frame_path)
                    if desc and desc != "未识别":
                        frame_descriptions.append(f"帧{idx}: {desc}")
                    else:
                        frame_descriptions.append(f"帧{idx}: 画面内容未识别")
            
            # 构建原生信息字典
            raw_info = {
                'subtitle_text': process_result.subtitle_text or '',
                'asr_text': process_result.asr_text or '',
                'frame_descriptions': frame_descriptions,
                'visual_analysis': process_result.visual_analysis or '',
                'visual_method': process_result.visual_method
            }
            
            if enable_summary:
                # 步骤2: 生成总结
                logger.debug("[BilibiliAutoDetect] 步骤2: 生成总结...")
                summary_result = await summary_service.generate_summary(
                    frame_paths=process_result.frame_paths,
                    video_info=video_info,
                    text_content=process_result.get_text_content(),
                    visual_analysis=process_result.visual_analysis,
                    visual_method=process_result.visual_method
                )
                
                if not summary_result.success or not summary_result.raw_summary:
                    logger.error(f"[BilibiliAutoDetect] 生成总结失败: {summary_result.error}")
                    return None
                
                logger.info(f"[BilibiliAutoDetect] 视频分析完成: {process_result.title}")
                
                # 步骤3: 修改消息内容，交给主回复系统处理
                video_info_text = self._build_video_info_text(
                    title=process_result.title,
                    author=process_result.author,
                    description=process_result.description,
                    summary=summary_result.raw_summary,
                    page=process_result.page,
                    page_title=process_result.page_title,
                    total_pages=process_result.total_pages,
                    duration=process_result.duration,
                    total_duration=process_result.total_duration
                )
                new_text = f"{message.plain_text}\n\n{video_info_text}"
                message.modify_plain_text(new_text)
                
                # 步骤4: 保存缓存（包含原生信息和总结）
                logger.debug("[BilibiliAutoDetect] 步骤4: 保存缓存...")
                if self.get_config("video.cache_enabled", True) and self.cache_manager:
                    cache_data = {
                        "video_id": video_id,
                        "page": process_result.page,
                        "page_title": process_result.page_title,
                        "total_pages": process_result.total_pages,
                        "title": process_result.title,
                        "author": process_result.author,
                        "description": process_result.description,
                        "duration": process_result.duration,
                        "total_duration": process_result.total_duration,
                        "raw_info": raw_info,
                        "summary": summary_result.raw_summary,
                        "has_subtitle": bool(process_result.subtitle_text),
                        "has_asr": bool(process_result.asr_text)
                    }
                    self.cache_manager.save_cache(cache_key, cache_data)
            else:
                # 不生成总结，直接使用原生信息
                video_info_text = summary_service.build_raw_info_text(video_info, raw_info)
                new_text = f"{message.plain_text}\n\n{video_info_text}"
                message.modify_plain_text(new_text)
                
                # 保存缓存（仅原生信息，无总结）
                if self.get_config("video.cache_enabled", True) and self.cache_manager:
                    cache_data = {
                        "video_id": video_id,
                        "page": process_result.page,
                        "page_title": process_result.page_title,
                        "total_pages": process_result.total_pages,
                        "title": process_result.title,
                        "author": process_result.author,
                        "description": process_result.description,
                        "duration": process_result.duration,
                        "total_duration": process_result.total_duration,
                        "raw_info": raw_info,
                        "summary": None,  # 无总结
                        "has_subtitle": bool(process_result.subtitle_text),
                        "has_asr": bool(process_result.asr_text)
                    }
                    self.cache_manager.save_cache(cache_key, cache_data)
            
            return message
            
        except NonRetryableError as e:
            # 不可重试的错误，静默失败
            logger.warning(f"[BilibiliAutoDetect] 处理视频失败（不可重试）: {e}")
            return None
        except Exception as e:
            # 其他异常，静默失败
            logger.error(f"[BilibiliAutoDetect] 处理视频失败: {e}")
            return None
            
        finally:
            # 根据配置决定是否即时删除临时文件
            # temp_file_max_age_min=0 表示即时删除，>0 表示由定时任务清理
            if process_result:
                max_age_min = self.get_config("video.temp_file_max_age_min", 60)
                if max_age_min == 0:
                    process_result.cleanup()
    
    def _build_video_info_text(
        self,
        title: str,
        author: str,
        description: str,
        summary: str,
        page: int = 1,
        page_title: str = "",
        total_pages: int = 1,
        duration: int = None,
        total_duration: int = None
    ) -> str:
        """构建发送给回复系统的视频信息文本
        
        Args:
            title: 视频标题
            author: UP主名称
            description: 视频简介
            summary: 视频内容总结
            page: 分P号
            page_title: 分P标题
            total_pages: 总分P数
            duration: 当前分P时长（秒）
            total_duration: 合集总时长（秒）
            
        Returns:
            格式化的视频信息文本
        """
        # 构建标题（包含分P信息）
        if total_pages > 1:
            if page_title:
                title_text = f"关于这个B站视频《{title}》P{page}「{page_title}」："
            else:
                title_text = f"关于这个B站视频《{title}》P{page}："
        else:
            title_text = f"关于这个B站视频《{title}》："
        
        parts = [title_text]
        
        if author:
            parts.append(f"UP主：{author}")
        
        # 时长显示逻辑
        if total_pages > 1:
            # 多P视频：显示当前分P时长和合集总时长
            if duration:
                parts.append(f"当前分P时长：{self._format_duration(duration)}")
            if total_duration:
                parts.append(f"合集总时长：{self._format_duration(total_duration)}（共{total_pages}P）")
        else:
            # 单P视频：只显示时长
            if duration:
                parts.append(f"时长：{self._format_duration(duration)}")
        
        if description:
            # 限制简介长度
            max_desc_len = 200
            if len(description) > max_desc_len:
                description = description[:max_desc_len] + "..."
            parts.append(f"简介：{description}")
        
        parts.append(f"内容总结：{summary}")
        
        return "\n".join(parts)
    
    def _format_duration(self, seconds: int) -> str:
        """格式化时长为用户友好的字符串
        
        Args:
            seconds: 秒数
            
        Returns:
            格式化的时长字符串，如"4小时2分钟"、"48分钟"、"30秒"
        """
        if seconds < 60:
            return f"{seconds}秒"
        
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        parts = []
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0:
            parts.append(f"{minutes}分钟")
        # 只有在没有小时和分钟时才显示秒
        if not parts and secs > 0:
            parts.append(f"{secs}秒")
        
        return "".join(parts) if parts else "0秒"


class BilibiliCommandHandler(BaseCommand):
    """B站视频解析命令处理器
    
    命令模式：
    - 用户使用 /bili 命令触发
    - 处理视频获取原生信息（不生成总结）
    - 使用原生信息直接生成个性化回复
    - 直接返回给用户（强制回复）
    - 使用 intercept_message_level=1，让用户命令消息对replyer可见但不触发回复
    """
    
    # BaseCommand 类属性
    command_name = "bilibili_command"
    command_description = "通过/bili命令解析B站视频"
    command_pattern = r"^/bili\s+(?P<video_arg>.+)"

    # 这些属性由plugin在注册前设置
    cache_manager: Optional[CacheManager] = None
    video_parser: Optional[VideoParser] = None
    video_analyzer: Optional[VideoAnalyzer] = None
    
    # 插件配置（由plugin在注册前设置）
    _plugin_config: Optional[dict] = None

    @classmethod
    def set_plugin_config_class(cls, config: dict):
        """设置插件配置（类方法，用于在注册前设置）"""
        cls._plugin_config = config

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        """执行命令处理
        
        命令模式：使用原生视频信息生成个性化回复并直接发送给用户
        （永久跳过总结生成环节）
        
        Returns:
            Tuple[bool, Optional[str], int]:
            (是否执行成功, 可选的回复消息, 拦截消息力度)
            - intercept_message_level=1: 仅不触发回复，replyer可见
        """
        video_service = None
        summary_service = None
        process_result = None
        
        try:
            # 从matched_groups获取视频参数
            command_arg = self.matched_groups.get('video_arg', '').strip()
            if not command_arg:
                logger.debug("[BilibiliCommand] 未提供视频参数")
                await self.send_text("请提供B站视频ID或链接，例如: /bili BV1xx411c7mD")
                return True, None, 1
            
            logger.debug(f"[BilibiliCommand] 检测到命令: /bili {command_arg}")
            
            # 检查video_analyzer是否初始化
            if not self.video_analyzer or not self.video_analyzer.is_initialized():
                logger.warning("[BilibiliCommand] 视频分析器未初始化")
                await self.send_text("视频分析功能暂时不可用，请稍后再试")
                return True, None, 1
            
            # 提取视频ID和分P号
            video_info = BilibiliAPI.extract_video_id(command_arg)
            if not video_info:
                logger.debug(f"[BilibiliCommand] 未找到有效的视频ID: {command_arg}")
                await self.send_text("未找到有效的B站视频ID，支持格式: BV号、av号、B站链接、b23.tv短链接")
                return True, None, 1
            
            video_type, video_id, page = video_info
            
            # 如果是短链接，需要先解析
            if video_type == 'short':
                resolved = await BilibiliAPI.resolve_short_url(video_id)
                if not resolved:
                    logger.warning(f"[BilibiliCommand] 短链接解析失败: {video_id}")
                    await self.send_text("短链接解析失败，请使用完整的B站链接")
                    return True, None, 1
                video_id, page = resolved
                video_type = 'bv' if video_id.startswith('BV') else 'av'
            
            if page > 1:
                logger.info(f"[BilibiliCommand] 处理视频: {video_id} P{page}")
            else:
                logger.info(f"[BilibiliCommand] 处理视频: {video_id}")
            
            # 创建服务实例
            logger.debug("[BilibiliCommand] 创建服务实例...")
            video_service = VideoService(self.video_parser, self.get_config)
            summary_service = SummaryService(self.video_analyzer, self.get_config)
            
            # 构建缓存key（包含分P号）
            cache_key = f"{video_id}_p{page}" if page > 1 else video_id
            
            # 检查缓存（命令模式使用原生信息）
            video_title = None
            video_duration = None
            video_total_duration = None
            video_description = None
            video_author = None
            video_page = page
            video_page_title = ""
            video_total_pages = 1
            raw_info = None
            
            if self.get_config("video.cache_enabled", True) and self.cache_manager:
                cached = self.cache_manager.get_cache(cache_key)
                if cached:
                    video_title = cached.get('title', '')
                    video_duration = cached.get('duration')
                    video_total_duration = cached.get('total_duration')
                    video_description = cached.get('description', '')
                    video_author = cached.get('author', '')
                    video_page = cached.get('page', page)
                    video_page_title = cached.get('page_title', '')
                    video_total_pages = cached.get('total_pages', 1)
                    raw_info = cached.get('raw_info', {})
            
            # 如果没有缓存或缓存中没有原生信息，处理视频
            if not raw_info:
                logger.debug("[BilibiliCommand] 缓存未命中，开始处理视频...")
                try:
                    process_result = await video_service.process_video(video_id, BilibiliAPI, page)
                except NonRetryableError as e:
                    # 不可重试的错误，发送友好提示
                    error_msg = get_friendly_error_message(
                        e.error_type,
                        limit=self.get_config("video.max_duration_min", 30)
                    )
                    logger.warning(f"[BilibiliCommand] 视频处理失败（不可重试）: {e}")
                    await self.send_text(f"视频解析失败：{error_msg}")
                    return True, None, 1
                except Exception as e:
                    logger.error(f"[BilibiliCommand] 视频处理异常: {e}")
                    await self.send_text("视频解析失败，请稍后重试")
                    return True, None, 1
                
                if not process_result.success:
                    # 根据错误类型发送友好提示
                    error_msg = self._get_friendly_error_message(process_result.error)
                    logger.warning(f"[BilibiliCommand] 视频处理失败: {process_result.error}")
                    await self.send_text(f"视频解析失败：{error_msg}")
                    return True, None, 1
                
                video_title = process_result.title
                video_duration = process_result.duration
                video_total_duration = process_result.total_duration
                video_description = process_result.description
                video_author = process_result.author
                video_page = process_result.page
                video_page_title = process_result.page_title
                video_total_pages = process_result.total_pages
                
                # 获取帧描述（如果有帧的话）
                # 硬编码限制：最多分析5帧，避免过多API调用
                frame_descriptions = []
                MAX_ANALYZE_FRAMES = 5  # 硬编码：最大VLM分析帧数
                if process_result.frame_paths and self.video_analyzer and self.video_analyzer.is_initialized():
                    max_analyze_frames = min(len(process_result.frame_paths), MAX_ANALYZE_FRAMES)
                    for idx, frame_path in enumerate(process_result.frame_paths[:max_analyze_frames], start=1):
                        desc = await self.video_analyzer.analyze_frame(frame_path)
                        if desc and desc != "未识别":
                            frame_descriptions.append(f"帧{idx}: {desc}")
                        else:
                            frame_descriptions.append(f"帧{idx}: 画面内容未识别")
                
                # 构建原生信息字典
                raw_info = {
                    'subtitle_text': process_result.subtitle_text or '',
                    'asr_text': process_result.asr_text or '',
                    'frame_descriptions': frame_descriptions,
                    'visual_analysis': process_result.visual_analysis or '',
                    'visual_method': process_result.visual_method
                }
                
                # 保存缓存（包含原生信息，命令模式不生成总结）
                if self.get_config("video.cache_enabled", True) and self.cache_manager:
                    cache_data = {
                        "video_id": video_id,
                        "page": video_page,
                        "page_title": video_page_title,
                        "total_pages": video_total_pages,
                        "title": video_title,
                        "author": video_author,
                        "description": video_description,
                        "duration": video_duration,
                        "total_duration": video_total_duration,
                        "raw_info": raw_info,
                        "summary": None,  # 命令模式不生成总结
                        "has_subtitle": bool(process_result.subtitle_text) if process_result else False,
                        "has_asr": bool(process_result.asr_text) if process_result else False
                    }
                    self.cache_manager.save_cache(cache_key, cache_data)
            
            # 构建视频信息字典
            video_info_dict = {
                'title': video_title,
                'author': video_author,
                'description': video_description,
                'duration': video_duration,
                'total_duration': video_total_duration,
                'video_id': video_id,
                'page': video_page,
                'page_title': video_page_title,
                'total_pages': video_total_pages,
            }
            
            # 使用原生信息生成个性化回复（跳过总结生成）
            logger.debug("[BilibiliCommand] 生成个性化回复...")
            personalized_reply = await summary_service.generate_personalized_reply_from_raw_info(
                video_info=video_info_dict,
                raw_info=raw_info
            )
            
            # 将MessageRecv转换为DatabaseMessages用于引用回复
            reply_message = self._message_recv_to_database_messages()
            
            if personalized_reply:
                logger.info(f"[BilibiliCommand] 视频解析完成: {video_title}")
                # 主动发送个性化回复给用户，使用引用回复
                await self.send_text(
                    personalized_reply,
                    set_reply=True,
                    reply_message=reply_message
                )
            else:
                # 如果个性化回复生成失败，回退到发送原生信息摘要
                fallback_text = self._build_fallback_reply(video_title, video_author, raw_info)
                await self.send_text(
                    fallback_text,
                    set_reply=True,
                    reply_message=reply_message
                )
            
            # 修改消息内容，将视频原生信息添加到用户命令消息中
            # 这样存储到数据库后，replyer可以看到完整的视频信息
            video_info_text = summary_service.build_raw_info_text(video_info_dict, raw_info)
            original_text = self.message.processed_plain_text
            self.message.processed_plain_text = f"{original_text}\n\n{video_info_text}"
            
            # 返回 intercept_message_level=1，让用户命令消息对replyer可见但不触发回复
            return True, None, 1
            
        except NonRetryableError as e:
            # 不可重试的错误，发送友好提示
            error_msg = get_friendly_error_message(
                e.error_type,
                limit=self.get_config("video.max_duration_min", 30)
            )
            logger.warning(f"[BilibiliCommand] 命令执行失败（不可重试）: {e}")
            try:
                await self.send_text(f"视频解析失败：{error_msg}")
            except Exception:
                pass
            return True, None, 1
        except Exception as e:
            logger.error(f"[BilibiliCommand] 命令执行失败: {e}")
            # 尝试发送友好错误消息
            try:
                await self.send_text("视频解析失败，请稍后重试")
            except Exception:
                pass
            return True, None, 1
            
        finally:
            # 根据配置决定是否即时删除临时文件
            # temp_file_max_age_min=0 表示即时删除，>0 表示由定时任务清理
            if process_result:
                max_age_min = self.get_config("video.temp_file_max_age_min", 60)
                if max_age_min == 0:
                    process_result.cleanup()
    
    def _build_fallback_reply(
        self,
        title: str,
        author: str,
        raw_info: dict
    ) -> str:
        """构建回退回复（当个性化回复生成失败时使用）
        
        Args:
            title: 视频标题
            author: UP主名称
            raw_info: 原生信息字典
            
        Returns:
            格式化的回退回复文本
        """
        parts = [f"关于《{title}》"]
        
        if author:
            parts[0] += f"（UP主：{author}）"
        
        parts[0] += "："
        
        # 添加文本内容摘要
        text_content = raw_info.get('subtitle_text') or raw_info.get('asr_text', '')
        if text_content:
            # 截取前200字
            if len(text_content) > 200:
                text_content = text_content[:200] + "..."
            parts.append(f"内容：{text_content}")
        
        # 添加画面描述
        frame_descriptions = raw_info.get('frame_descriptions', [])
        if frame_descriptions:
            parts.append(f"画面：{'; '.join(frame_descriptions[:3])}")
        
        return "\n".join(parts)
    
    def _message_recv_to_database_messages(self) -> Optional["DatabaseMessages"]:
        """将MessageRecv转换为DatabaseMessages用于引用回复
        
        Returns:
            DatabaseMessages对象，如果转换失败则返回None
        """
        try:
            from src.common.data_models.database_data_model import DatabaseMessages
            
            msg = self.message
            msg_info = msg.message_info
            user_info = msg_info.user_info
            group_info = msg_info.group_info
            chat_stream = msg.chat_stream
            
            # 构建DatabaseMessages对象
            db_message = DatabaseMessages(
                message_id=msg_info.message_id,
                time=msg_info.time,
                chat_id=chat_stream.stream_id if chat_stream else "",
                processed_plain_text=msg.processed_plain_text,
                user_id=user_info.user_id if user_info else "",
                user_nickname=user_info.user_nickname if user_info else "",
                user_cardname=getattr(user_info, 'user_cardname', None) if user_info else None,
                user_platform=user_info.platform if user_info else "",
                chat_info_group_id=group_info.group_id if group_info else None,
                chat_info_group_name=group_info.group_name if group_info else None,
                chat_info_group_platform=getattr(group_info, 'group_platform', None) if group_info else None,
                chat_info_user_id=user_info.user_id if user_info else "",
                chat_info_user_nickname=user_info.user_nickname if user_info else "",
                chat_info_user_cardname=getattr(user_info, 'user_cardname', None) if user_info else None,
                chat_info_user_platform=user_info.platform if user_info else "",
                chat_info_stream_id=chat_stream.stream_id if chat_stream else "",
                chat_info_platform=chat_stream.platform if chat_stream else "",
                chat_info_create_time=chat_stream.create_time if chat_stream else 0.0,
                chat_info_last_active_time=chat_stream.last_active_time if chat_stream else 0.0,
            )
            
            return db_message
            
        except Exception as e:
            logger.error(f"[BilibiliCommand] 转换消息对象失败: {e}")
            return None
    
    def _get_friendly_error_message(self, error: Optional[str]) -> str:
        """根据错误信息返回友好的错误提示
        
        Args:
            error: 原始错误信息
            
        Returns:
            友好的错误提示
        """
        if not error:
            return "未知错误"
        
        error_lower = error.lower()
        
        # 根据错误信息关键词匹配
        if "不存在" in error or "not found" in error_lower or "404" in error:
            return "视频不存在或已被删除"
        if "时长超过" in error or "too long" in error_lower:
            return f"视频时长超过限制（>{self.get_config('video.max_duration_min', 30)}分钟）"
        if "文件过大" in error or "too large" in error_lower:
            return f"视频文件过大（>{self.get_config('video.max_size_mb', 200)}MB）"
        if "网络" in error or "network" in error_lower or "timeout" in error_lower:
            return "网络连接失败，请稍后重试"
        if "权限" in error or "permission" in error_lower or "403" in error:
            return "视频需要登录或会员才能观看"
        if "频繁" in error or "rate" in error_lower or "429" in error:
            return "请求过于频繁，请稍后重试"
        
        return error