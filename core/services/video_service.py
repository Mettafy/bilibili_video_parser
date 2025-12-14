# -*- coding: utf-8 -*-
"""
视频处理服务模块 - 封装视频处理的完整流程

本模块提供视频处理的高层服务接口，协调多个底层模块完成：
1. 视频信息获取
2. 视频下载
3. 视频抽帧
4. 字幕获取
5. ASR语音识别
6. 视觉分析（VLM或豆包）

主要类：
- VideoService: 视频处理服务
- VideoProcessResult: 视频处理结果数据类

处理流程：
1. 获取视频基本信息（标题、简介、时长、UP主等）
2. 检查时长限制
3. 获取字幕（如果配置了SESSDATA）
4. 判断是否需要视觉分析
5. 下载视频（如需要）
6. 执行视觉分析：
   - 豆包模式：上传视频到豆包进行整体分析
   - VLM模式：抽帧后逐帧分析
7. 执行ASR语音识别（如启用）
8. 返回处理结果

视觉分析方式：
- default: 使用MaiBot VLM抽帧分析
- builtin: 使用插件内置VLM抽帧分析
- doubao: 使用豆包视频理解模型
- none: 不进行视觉分析

时长限制逻辑：
- max_duration_min: 视频最大时长限制（超过则跳过）
- visual_max_duration_min: 视觉分析最大时长限制（超过则只用字幕/ASR）
- 使用向下取整：11分50秒算11分钟

VideoProcessResult 字段：
- success: 是否成功
- error: 错误信息
- video_id, title, description, author, duration: 视频基本信息
- page, page_title, total_pages, total_duration: 分P信息
- frame_paths: 抽取的帧路径列表
- subtitle_text: 字幕文本
- asr_text: ASR识别文本
- visual_analysis: 豆包视觉分析结果
- visual_method: 使用的视觉分析方式
- video_path, frames_dir: 临时文件路径（用于清理）

使用示例：
    service = VideoService(video_parser, get_config)
    
    result = await service.process_video(
        video_id="BV1xx411c7mD",
        bilibili_api=BilibiliAPI,
        page=1
    )
    
    if result.success:
        print(f"标题: {result.title}")
        print(f"帧数: {len(result.frame_paths)}")
        print(f"字幕: {result.subtitle_text}")
    
    # 清理临时文件
    result.cleanup()

依赖：
- bilibili_api: B站API（由调用方传入）
- video_parser: 视频解析器
- doubao_analyzer: 豆包分析器（可选）
- safe_delete: 安全删除工具
- retry_utils: 重试工具

Author: 约瑟夫.k && 白泽
"""
import os
import math
import uuid
import subprocess
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Callable
from src.plugin_system import llm_api, get_logger
from ..safe_delete import safe_delete_temp_file, safe_delete_temp_dir, get_temp_subdir
from ..retry_utils import NonRetryableError, ErrorType

logger = get_logger("video_service")


@dataclass
class VideoProcessResult:
    """视频处理结果"""
    success: bool = False
    error: Optional[str] = None
    
    # 视频基础信息（始终获取，无需登录）
    video_id: str = ""
    title: str = ""
    description: str = ""  # 视频简介
    author: str = ""  # UP主名称
    duration: Optional[int] = None
    aid: Optional[int] = None
    cid: Optional[int] = None
    
    # 分P信息
    page: int = 1  # 分P号（从1开始）
    page_title: str = ""  # 分P标题
    total_pages: int = 1  # 总分P数
    total_duration: Optional[int] = None  # 合集总时长（秒）
    
    # 处理结果
    frame_paths: List[str] = field(default_factory=list)
    subtitle_text: Optional[str] = None
    asr_text: Optional[str] = None
    
    # 视觉分析结果（豆包或VLM）
    visual_analysis: Optional[str] = None  # 豆包视频理解结果
    visual_method: str = "default"  # 使用的视觉分析方式: default, builtin, doubao, none
    
    # 临时文件路径（用于清理）
    video_path: Optional[str] = None
    frames_dir: Optional[str] = None
    
    def get_text_content(self) -> Optional[str]:
        """获取文本内容（合并字幕和ASR）
        
        如果同时有字幕和ASR，会合并两者以获得更完整的内容
        """
        parts = []
        if self.subtitle_text:
            parts.append(f"【字幕内容】\n{self.subtitle_text}")
        if self.asr_text:
            parts.append(f"【语音识别内容】\n{self.asr_text}")
        
        if parts:
            return "\n\n".join(parts)
        return None
    
    def cleanup(self):
        """清理临时文件（使用安全删除，5-6重验证）
        
        安全删除会验证：
        1. 路径非空
        2. 文件/目录存在
        3. 类型正确（文件/目录）
        4. 文件名前缀正确（bili_video_/bili_audio_/bili_frames_）
        5. 路径在插件临时目录中（data/temp/）
        6. 目录只包含图片文件（针对帧目录）
        """
        # 安全删除临时视频文件
        if self.video_path:
            success, reason = safe_delete_temp_file(self.video_path)
            if not success and reason != "文件不存在":
                logger.warning(f"[VideoService] 临时视频文件未删除: {reason}")
        
        # 安全删除临时帧目录
        if self.frames_dir:
            success, reason = safe_delete_temp_dir(self.frames_dir)
            if not success and reason != "目录不存在":
                logger.warning(f"[VideoService] 临时帧目录未删除: {reason}")


class VideoService:
    """视频处理服务
    
    支持两种视觉分析方式：
    1. VLM抽帧分析（默认）：下载视频 -> 抽帧 -> VLM分析每帧
    2. 豆包视频模型：下载视频 -> 上传豆包 -> 整体视频理解
    """
    
    def __init__(
        self,
        video_parser,  # VideoParser实例
        get_config: Callable[[str, Any], Any]  # 配置获取函数
    ):
        """初始化视频服务
        
        Args:
            video_parser: VideoParser实例
            get_config: 配置获取函数，签名为 get_config(key, default)
        """
        self.video_parser = video_parser
        self.get_config = get_config
        self._voice_model = None
        self._voice_model_checked = False
        self._doubao_analyzer = None
        self._doubao_checked = False
    
    def _get_doubao_analyzer(self):
        """懒加载获取豆包分析器
        
        采用动态参数传递策略：
        - 只传递用户在配置中实际定义的参数
        - 不同版本的豆包API可能支持不同的参数
        - 用户可以自由添加豆包特有的参数
        """
        if self._doubao_checked:
            return self._doubao_analyzer
        
        self._doubao_checked = True
        
        visual_method = self.get_config("analysis.visual_method", "default")
        if visual_method != "doubao":
            return None
        
        try:
            from ..doubao_analyzer import DoubaoAnalyzer
            
            # 必需参数（有默认值）
            doubao_config = {
                "api_key": self.get_config("analysis.doubao.api_key", ""),
                "model_id": self.get_config("analysis.doubao.model_id", "doubao-seed-1-6-251015"),
                "base_url": self.get_config("analysis.doubao.base_url", "https://ark.cn-beijing.volces.com/api/v3"),
                "timeout": self.get_config("analysis.doubao.timeout", 120),
                "max_retries": self.get_config("analysis.doubao.max_retries", 2),
                "retry_interval": self.get_config("analysis.doubao.retry_interval", 10),
                "video_prompt": self.get_config("analysis.doubao.video_prompt", ""),
                "summary_min_chars": self.get_config("analysis.doubao.summary_min_chars", 100),
                "summary_max_chars": self.get_config("analysis.doubao.summary_max_chars", 150),
            }
            
            # 动态参数：只有用户配置了才传递
            # 这些参数不同版本的豆包API可能不支持
            optional_params = ["fps", "temperature", "max_tokens", "top_p", "top_k"]
            for param in optional_params:
                value = self.get_config(f"analysis.doubao.{param}", None)
                if value is not None:
                    doubao_config[param] = value
            
            # 获取所有用户自定义的额外参数（豆包特有参数）
            # 通过遍历配置获取所有analysis.doubao.*的配置项
            doubao_section = self.get_config("analysis.doubao", {})
            if isinstance(doubao_section, dict):
                known_params = {
                    "visual_max_duration_min", "api_key", "model_id", "base_url",
                    "timeout", "max_retries", "retry_interval", "video_prompt",
                    "summary_min_chars", "summary_max_chars"
                } | set(optional_params)
                
                for key, value in doubao_section.items():
                    if key not in known_params and value is not None:
                        # 用户自定义的额外参数，直接传递
                        doubao_config[key] = value
            
            logger.debug(f"[VideoService] 豆包配置参数: {list(doubao_config.keys())}")
            
            self._doubao_analyzer = DoubaoAnalyzer(doubao_config)
            logger.info("[BilibiliVideoParser] 豆包视频分析器已初始化")
            
        except ImportError as e:
            logger.warning(f"[VideoService] 无法导入豆包分析器: {e}")
        except Exception as e:
            logger.warning(f"[VideoService] 初始化豆包分析器失败: {e}")
        
        return self._doubao_analyzer
    
    def _get_voice_model(self):
        """懒加载获取语音识别模型"""
        if self._voice_model_checked:
            return self._voice_model
        
        self._voice_model_checked = True
        try:
            models = llm_api.get_available_models()
            self._voice_model = models.get('voice')
            if not self._voice_model:
                logger.debug("[VideoService] 未配置语音识别模型，ASR功能不可用")
        except Exception as e:
            logger.warning(f"[VideoService] 获取语音识别模型失败: {e}")
        
        return self._voice_model
    
    async def process_video(
        self,
        video_id: str,
        bilibili_api,  # BilibiliAPI类
        page: int = 1,  # 分P号（从1开始）
    ) -> VideoProcessResult:
        """处理视频的完整流程
        
        根据配置选择不同的处理方式：
        1. 豆包模式：下载视频 -> 豆包视频理解
        2. VLM模式：下载视频 -> 抽帧 -> VLM分析
        3. 仅文本模式：只获取字幕/ASR（视频时长超过视觉分析限制时）
        
        Args:
            video_id: 视频ID (BV号或AV号)
            bilibili_api: BilibiliAPI类
            page: 分P号（从1开始），默认为1
            
        Returns:
            VideoProcessResult: 处理结果
        """
        result = VideoProcessResult(video_id=video_id)
        
        try:
            logger.debug(f"[VideoService] 开始处理视频: {video_id}, 分P: {page}")
            
            # 获取全局配置（从 video 节）
            sessdata = self.get_config("video.sessdata", "")
            enable_asr = self.get_config("video.enable_asr", False)
            max_duration_min = self.get_config("video.max_duration_min", 30.0)
            # 时长限制使用向下取整到整分钟：11分50秒算11分钟，只有12分0秒才算12分钟
            # 所以限制30分钟时，30分59秒的视频仍然可以处理
            max_duration_sec = int((max_duration_min + 1) * 60) - 1  # 30分钟 -> 30分59秒
            max_size_mb = self.get_config("video.max_size_mb", 200)
            
            # 视觉分析方式
            visual_method = self.get_config("analysis.visual_method", "default")
            
            # 硬编码最大抽帧数和最大分析帧数
            MAX_EXTRACT_FRAMES = 10  # 最大抽帧数
            MAX_ANALYZE_FRAMES = 5   # 最大VLM分析帧数（在handlers.py中使用）
            
            # 根据 visual_method 获取对应模式的配置
            if visual_method == "default":
                visual_max_duration_min = self.get_config("analysis.default.visual_max_duration_min", 10.0)
                frame_interval = self.get_config("analysis.default.frame_interval_sec", 6)
            elif visual_method == "builtin":
                visual_max_duration_min = self.get_config("analysis.builtin.visual_max_duration_min", 10.0)
                frame_interval = self.get_config("analysis.builtin.frame_interval_sec", 6)
            elif visual_method == "doubao":
                visual_max_duration_min = self.get_config("analysis.doubao.visual_max_duration_min", 10.0)
                frame_interval = 6  # 豆包模式不使用抽帧
            else:
                # none 模式或其他：不进行视觉分析
                visual_max_duration_min = 0
                frame_interval = 6
            
            # 同样使用向下取整：10分钟限制 -> 10分59秒的视频仍然进行视觉分析
            visual_max_duration_sec = int((visual_max_duration_min + 1) * 60) - 1
            
            # 获取重试配置
            retry_max_attempts = self.get_config("video.retry_max_attempts", 3)
            retry_interval_sec = self.get_config("video.retry_interval_sec", 2.0)
            
            # 步骤1: 获取视频基础信息（带重试机制）
            logger.debug(f"[VideoService] 步骤1: 获取视频信息...")
            video_info = await bilibili_api.get_video_info(
                video_id, sessdata, page,
                max_attempts=retry_max_attempts,
                retry_interval=retry_interval_sec
            )
            if not video_info:
                result.error = "获取视频信息失败"
                return result
            
            logger.debug(f"[VideoService] 视频信息获取成功: {video_info.get('title', '')[:30]}...")
            
            result.title = video_info.get('title', '')
            result.description = video_info.get('desc', '')  # 视频简介
            result.duration = video_info.get('duration')
            result.aid = video_info.get('aid')
            result.cid = video_info.get('cid')
            
            # 获取UP主信息
            owner_info = video_info.get('owner', {})
            result.author = owner_info.get('name', '')
            
            # 获取分P信息
            result.page = video_info.get('page', 1)
            result.page_title = video_info.get('page_title', '')
            result.total_pages = video_info.get('total_pages', 1)
            result.total_duration = video_info.get('total_duration')
            
            
            # 检查时长限制（向下取整到整分钟：11分50秒算11分钟）
            if result.duration:
                video_minutes = result.duration // 60  # 向下取整到整分钟
                if video_minutes >= max_duration_min + 1:  # 超过限制的整分钟数
                    result.error = f"视频时长超过限制（>{int(max_duration_min)}分钟）"
                    logger.warning(f"[VideoService] {result.error}: {result.duration}s ({video_minutes}分钟)")
                    return result
            
            # 步骤2: 获取字幕（如果配置了sessdata，带重试机制）
            if sessdata and result.aid and result.cid:
                logger.debug(f"[VideoService] 步骤2: 获取字幕...")
                result.subtitle_text = await bilibili_api.get_subtitle(
                    result.aid, result.cid, sessdata,
                    max_attempts=retry_max_attempts,
                    retry_interval=retry_interval_sec
                )
                if result.subtitle_text:
                    logger.debug(f"[VideoService] 字幕获取成功，长度: {len(result.subtitle_text)}")
                else:
                    logger.debug("[VideoService] 该视频没有可用字幕")
            else:
                logger.debug("[VideoService] 跳过字幕获取（未配置SESSDATA或缺少aid/cid）")
            
            # 判断是否需要进行视觉分析（向下取整到整分钟）
            if result.duration:
                video_minutes = result.duration // 60  # 向下取整到整分钟
                need_visual_analysis = (
                    visual_max_duration_min > 0 and
                    video_minutes < visual_max_duration_min + 1  # 10分钟限制 -> 10分59秒仍然分析
                )
            else:
                need_visual_analysis = visual_max_duration_min > 0
            
            if not need_visual_analysis:
                result.visual_method = "none"
            else:
                result.visual_method = visual_method
            
            # 步骤3: 下载视频（如果需要视觉分析或ASR，带重试机制）
            # 采用"尽力获取"策略：下载失败时继续处理，降级到字幕模式或基础信息模式
            if need_visual_analysis or enable_asr:
                logger.debug(f"[VideoService] 步骤3: 下载视频（视觉分析={need_visual_analysis}, ASR={enable_asr}）...")
                
                # 获取下载超时配置
                download_timeout_sec = self.get_config("video.download_timeout_sec", 300)
                
                try:
                    download_info = await bilibili_api.get_video_download_url(
                        video_id, sessdata, page,
                        max_attempts=retry_max_attempts,
                        retry_interval=retry_interval_sec
                    )
                    
                    if download_info:
                        video_url = download_info['url']
                        result.video_path = await bilibili_api.download_video(
                            video_url, max_size_mb, download_timeout_sec,
                            max_attempts=retry_max_attempts,
                            retry_interval=retry_interval_sec
                        )
                    else:
                        logger.warning("[VideoService] 获取视频下载地址失败，降级处理")
                        result.video_path = None
                        
                except NonRetryableError as e:
                    # 不可重试的错误（如文件过大），记录日志但继续处理
                    logger.warning(f"[VideoService] 视频下载失败（不可重试）: {e}")
                    result.video_path = None
                except Exception as e:
                    # 其他错误（超时、网络错误等），记录日志但继续处理
                    logger.warning(f"[VideoService] 视频下载失败: {e}")
                    result.video_path = None
                
                if not result.video_path:
                    # 下载失败，降级处理
                    has_subtitle = bool(result.subtitle_text)
                    if has_subtitle:
                        logger.info("[VideoService] 视频下载失败，降级到字幕模式（Level 2）")
                    else:
                        logger.info("[VideoService] 视频下载失败，降级到基础信息模式（Level 3）")
                    result.visual_method = "none"
                    # 不返回错误，继续处理
                else:
                    logger.debug(f"[VideoService] 视频下载完成: {result.video_path}")
            else:
                logger.debug("[VideoService] 跳过视频下载（不需要视觉分析和ASR）")
            
            # 步骤4: 视觉分析
            if need_visual_analysis and result.video_path:
                logger.debug(f"[VideoService] 步骤4: 视觉分析（方式: {visual_method}）...")
                if visual_method == "doubao":
                    # 使用豆包视频模型
                    logger.debug("[VideoService] 使用豆包视频模型分析...")
                    result.visual_analysis = await self._analyze_with_doubao(result.video_path)
                    if not result.visual_analysis:
                        logger.warning(f"[VideoService] 豆包分析失败，回退到VLM抽帧")
                        result.visual_method = "default"
                
                # default/builtin 都使用VLM抽帧分析
                if visual_method in ("default", "builtin") or (visual_method == "doubao" and not result.visual_analysis):
                    # 使用VLM抽帧分析
                    # 根据视频时长和抽帧间隔自动计算抽帧数量，最多 MAX_EXTRACT_FRAMES 帧
                    if result.duration:
                        n_frames = max(1, int(math.ceil(float(result.duration) / max(1, frame_interval))))
                        n_frames = min(n_frames, MAX_EXTRACT_FRAMES)
                        result.frame_paths = await self.video_parser.extract_frames_equidistant(
                            result.video_path, result.duration, n_frames
                        )
                    else:
                        result.frame_paths = await self.video_parser.extract_frames(
                            result.video_path, frame_interval, MAX_EXTRACT_FRAMES
                        )
                    
                    if result.frame_paths:
                        result.frames_dir = os.path.dirname(result.frame_paths[0])
                        # 保持原来的visual_method（default或builtin）
                        if result.visual_method not in ("default", "builtin"):
                            result.visual_method = "default"
                        logger.debug(f"[VideoService] 抽帧完成，共{len(result.frame_paths)}帧")
                    else:
                        logger.warning(f"[VideoService] 视频抽帧失败")
                        result.visual_method = "none"
            else:
                logger.debug("[VideoService] 跳过视觉分析（视频时长超过限制或不需要）")
            
            # 步骤5: 如果启用ASR，执行语音识别
            if enable_asr and result.video_path:
                logger.debug("[VideoService] 步骤5: ASR语音识别...")
                result.asr_text = await self._extract_audio_text(result.video_path)
                if result.asr_text:
                    logger.debug(f"[VideoService] ASR识别完成，长度: {len(result.asr_text)}")
                else:
                    logger.debug("[VideoService] ASR识别未返回结果")
            
            result.success = True
            logger.debug(f"[VideoService] 视频处理完成: {result.title}")
            return result
            
        except NonRetryableError:
            # 不可重试的错误，直接向上抛出
            raise
        except Exception as e:
            logger.error(f"[VideoService] 处理视频失败: {e}")
            result.error = str(e)
            return result
    
    async def _analyze_with_doubao(self, video_path: str) -> Optional[str]:
        """使用豆包视频模型分析视频
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            分析结果文本
        """
        doubao = self._get_doubao_analyzer()
        if not doubao:
            logger.warning("[VideoService] 豆包分析器不可用")
            return None
        
        try:
            result = await doubao.analyze_video(video_path)
            return result
        except Exception as e:
            logger.error(f"[VideoService] 豆包分析失败: {e}")
            return None
    
    async def _extract_audio_text(self, video_path: str) -> Optional[str]:
        """从视频中提取音频并转文字
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            识别的文本
        """
        voice_model = self._get_voice_model()
        if not voice_model:
            logger.warning("[VideoService] 语音识别模型未配置，无法使用ASR")
            return None
        
        audio_path = None
        try:
            # 使用ffmpeg提取音频
            audio_path = await self._extract_audio(video_path)
            if not audio_path:
                logger.warning("[VideoService] 音频提取失败")
                return None
            
            # 调用语音识别模型
            # 注意：这里需要根据MaiBot的voice模型API来实现
            # 目前MaiBot的voice模型可能使用SenseVoice等模型
            result = await self._call_voice_model(audio_path, voice_model)
            return result
            
        except Exception as e:
            logger.error(f"[VideoService] ASR处理失败: {e}")
            return None
        finally:
            # 根据配置决定是否即时删除临时音频文件
            # temp_file_max_age_min=0 表示即时删除，>0 表示由定时任务清理
            if audio_path:
                max_age_min = self.get_config("video.temp_file_max_age_min", 60)
                if max_age_min == 0:
                    success, reason = safe_delete_temp_file(audio_path)
                    if not success and reason != "文件不存在":
                        logger.warning(f"[VideoService] 临时音频文件未删除: {reason}")
                    else:
                        logger.debug("[VideoService] 临时音频文件已即时删除")
                # else: 由定时清理任务处理
    
    async def _extract_audio(self, video_path: str) -> Optional[str]:
        """从视频中提取音频
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            音频文件路径
        """
        if not self.video_parser.ffmpeg_path:
            logger.warning("[VideoService] ffmpeg不可用，无法提取音频")
            return None
        
        try:
            # 使用插件的临时目录创建音频文件
            audio_temp_dir = get_temp_subdir("audio")
            audio_filename = f"bili_audio_{uuid.uuid4().hex[:8]}.wav"
            audio_path = os.path.join(audio_temp_dir, audio_filename)
            
            # 使用ffmpeg提取音频
            cmd = [
                self.video_parser.ffmpeg_path,
                "-y",
                "-i", video_path,
                "-vn",  # 不处理视频
                "-acodec", "pcm_s16le",  # PCM格式
                "-ar", "16000",  # 16kHz采样率
                "-ac", "1",  # 单声道
                audio_path
            ]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120
            )
            
            if result.returncode == 0 and os.path.exists(audio_path):
                return audio_path
            else:
                logger.warning(f"[VideoService] 音频提取失败: {result.stderr.decode()[:200]}")
                return None
                
        except Exception as e:
            logger.error(f"[VideoService] 提取音频异常: {e}")
            return None
    
    async def _call_voice_model(self, audio_path: str, voice_model) -> Optional[str]:
        """调用语音识别模型
        
        使用MaiBot的LLMRequest.generate_response_for_voice方法
        
        Args:
            audio_path: 音频文件路径
            voice_model: 语音识别模型配置（TaskConfig）
            
        Returns:
            识别的文本
        """
        try:
            import base64
            
            # 读取音频文件并转为base64
            with open(audio_path, 'rb') as f:
                audio_data = base64.b64encode(f.read()).decode('utf-8')
            
            logger.debug(f"[VideoService] 音频文件大小: {len(audio_data) // 1024}KB (base64)")
            
            # 使用MaiBot的LLMRequest调用语音识别
            from src.llm_models.utils_model import LLMRequest
            
            llm_request = LLMRequest(model_set=voice_model, request_type="plugin.video_asr")
            
            # 调用generate_response_for_voice方法
            result = await llm_request.generate_response_for_voice(audio_data)
            return result if result else None
            
        except Exception as e:
            logger.error(f"[VideoService] 调用语音识别模型失败: {e}")
            return None