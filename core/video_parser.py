# -*- coding: utf-8 -*-
"""
视频解析模块 - 使用ffmpeg进行视频处理

本模块封装了ffmpeg的视频处理功能，包括：
1. 视频帧提取（按间隔抽帧、等距抽帧）
2. 音频提取（用于ASR语音识别）
3. 视频时长获取

主要类：
- VideoParser: 视频解析器

抽帧方式：
1. 按间隔抽帧（extract_frames）：
   - 每隔指定秒数抽取一帧
   - 适合固定间隔的场景
   
2. 等距抽帧（extract_frames_equidistant）：
   - 根据视频时长均匀分布抽帧点
   - 公式：t_i = (i/(N+1))*duration
   - 确保帧均匀分布在视频中

图片格式处理：
- 默认输出JPEG格式
- 自动转换非JPEG格式（需要PIL）
- 处理透明通道（RGBA -> RGB）

临时文件管理：
- 帧图片保存在 data/temp/frames/bili_frames_{uuid}/ 目录
- 使用safe_delete模块的临时目录管理

使用示例：
    parser = VideoParser()
    
    # 检查ffmpeg
    if parser.check_ffmpeg():
        # 按间隔抽帧
        frames = await parser.extract_frames(
            video_path="/path/to/video.mp4",
            interval_sec=6,
            max_frames=10
        )
        
        # 等距抽帧
        frames = await parser.extract_frames_equidistant(
            video_path="/path/to/video.mp4",
            duration_sec=300,
            count=10
        )
        
        # 获取视频时长
        duration = parser.get_video_duration("/path/to/video.mp4")

依赖：
- ffmpeg: 视频处理工具（必需）
- ffprobe: 视频信息获取工具（可选，用于获取时长）
- PIL: 图片处理（可选，用于格式转换）
- safe_delete: 临时目录管理

注意：
- ffmpeg必须安装并在系统PATH中
- 如果ffprobe不可用，get_video_duration将返回None

Author: 约瑟夫.k && 白泽
"""
import os
import shutil
import subprocess
from typing import Optional, Dict, Any, List
from pathlib import Path
from src.plugin_system import get_logger
from .safe_delete import get_temp_subdir

logger = get_logger("video_parser")

# 尝试导入PIL用于图片格式转换
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("[VideoParser] PIL未安装，图片格式转换功能将受限")


class VideoParser:
    """视频解析器"""

    def __init__(self, ffmpeg_path: Optional[str] = None, **kwargs):
        """初始化视频解析器
        
        Args:
            ffmpeg_path: ffmpeg可执行文件路径，为None时自动检测
            **kwargs: 其他参数（保留兼容性）
        """
        self.ffmpeg_path = self._detect_ffmpeg(ffmpeg_path)
        self.ffprobe_path = self._detect_ffprobe()

    def _detect_ffmpeg(self, custom_path: Optional[str] = None) -> Optional[str]:
        """自动检测ffmpeg路径
        
        Args:
            custom_path: 用户指定的路径
            
        Returns:
            ffmpeg可执行文件路径
        """
        # 1. 如果用户指定了路径，优先使用
        if custom_path:
            if shutil.which(custom_path):
                return shutil.which(custom_path)
            logger.warning(f"[VideoParser] 指定的ffmpeg路径无效: {custom_path}")
        
        # 2. 尝试从系统PATH查找
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
        
        logger.error("[VideoParser] 未找到ffmpeg，请安装后重试")
        return None

    def _detect_ffprobe(self) -> Optional[str]:
        """自动检测ffprobe路径"""
        # 1. 尝试从系统PATH查找
        system_ffprobe = shutil.which("ffprobe")
        if system_ffprobe:
            return system_ffprobe
        
        # 2. 尝试从ffmpeg同目录查找
        if self.ffmpeg_path:
            ffmpeg_dir = os.path.dirname(self.ffmpeg_path)
            ffprobe_path = os.path.join(ffmpeg_dir, "ffprobe")
            if os.path.exists(ffprobe_path):
                return ffprobe_path
        
        return None

    def check_ffmpeg(self) -> bool:
        """检查ffmpeg是否可用"""
        if not self.ffmpeg_path:
            return False
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"[VideoParser] ffmpeg检查失败: {e}")
            return False

    def _create_frames_temp_dir(self) -> str:
        """创建帧临时目录
        
        Returns:
            临时目录路径
        """
        import uuid
        
        # 使用插件的临时目录
        frames_base_dir = get_temp_subdir("frames")
        temp_dir_name = f"bili_frames_{uuid.uuid4().hex[:8]}"
        temp_dir = os.path.join(frames_base_dir, temp_dir_name)
        os.makedirs(temp_dir, exist_ok=True)
        return temp_dir
    
    async def extract_frames(
        self,
        video_path: str,
        interval_sec: int = 6,
        max_frames: int = 10
    ) -> List[str]:
        """从视频中抽取关键帧（按间隔抽帧）
        
        Args:
            video_path: 视频文件路径
            interval_sec: 抽帧间隔(秒)
            max_frames: 最大帧数
            
        Returns:
            关键帧图片路径列表
        """
        frames = []
        temp_dir = self._create_frames_temp_dir()
        logger.debug(f"[VideoParser] 开始按间隔抽帧: video={video_path}, interval={interval_sec}s, max_frames={max_frames}")
        
        try:
            # 使用ffmpeg抽帧
            output_pattern = os.path.join(temp_dir, "frame_%03d.jpg")
            cmd = [
                self.ffmpeg_path,
                "-i", video_path,
                "-vf", f"fps=1/{interval_sec}",
                "-frames:v", str(max_frames),
                "-qscale:v", "2",
                output_pattern
            ]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300
            )
            
            if result.returncode != 0:
                logger.error(f"[VideoParser] 抽帧失败: {result.stderr.decode()}")
                return []
            
            # 收集生成的帧
            for i in range(1, max_frames + 1):
                frame_path = os.path.join(temp_dir, f"frame_{i:03d}.jpg")
                if os.path.exists(frame_path):
                    frames.append(frame_path)
            
            logger.debug(f"[VideoParser] 按间隔抽帧完成: 共抽取 {len(frames)} 帧")
            return frames
            
        except Exception as e:
            logger.error(f"[VideoParser] 抽帧异常: {e}")
            return []
    
    async def extract_frames_equidistant(
        self,
        video_path: str,
        duration_sec: float,
        count: int = 10,
        output_format: str = "jpeg"
    ) -> List[str]:
        """从视频中等距抽取关键帧
        
        Args:
            video_path: 视频文件路径
            duration_sec: 视频时长(秒)
            count: 抽取帧数
            output_format: 输出图片格式，默认为jpeg
            
        Returns:
            关键帧图片路径列表
        """
        frames = []
        temp_dir = self._create_frames_temp_dir()
        logger.debug(f"[VideoParser] 开始等距抽帧: video={video_path}, duration={duration_sec}s, count={count}")
        
        try:
            N = max(1, int(count))
            total = max(0.0, float(duration_sec))
            
            # 计算等距时间点：t_i = (i/(N+1))*duration
            times = []
            for i in range(1, N + 1):
                t = (i / (N + 1.0)) * total
                times.append(t)
            
            # 为每个时间点抽取一帧
            for idx, t in enumerate(times, start=1):
                frame_path = os.path.join(temp_dir, f"frame_{idx:03d}.jpg")
                cmd = [
                    self.ffmpeg_path,
                    "-y",
                    "-ss", f"{max(0.0, t):.3f}",
                    "-i", video_path,
                    "-frames:v", "1",
                    "-qscale:v", "2",
                    "-f", "image2",  # 强制输出为图片格式
                    "-c:v", "mjpeg",  # 使用MJPEG编码器确保输出JPEG
                    frame_path
                ]
                
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30
                )
                
                if result.returncode == 0 and os.path.exists(frame_path):
                    # 验证并确保是JPEG格式
                    converted_path = self._ensure_jpeg_format(frame_path)
                    if converted_path:
                        frames.append(converted_path)
                    else:
                        frames.append(frame_path)
                else:
                    logger.warning(f"[VideoParser] 在 {t:.3f}s 处抽帧失败")
            
            logger.debug(f"[VideoParser] 等距抽帧完成: 共抽取 {len(frames)} 帧")
            return frames
            
        except Exception as e:
            logger.error(f"[VideoParser] 等距抽帧异常: {e}")
            return []
    
    def _ensure_jpeg_format(self, image_path: str) -> Optional[str]:
        """确保图片是JPEG格式
        
        Args:
            image_path: 图片路径
            
        Returns:
            转换后的图片路径，如果转换失败返回None
        """
        if not PIL_AVAILABLE:
            # PIL不可用，假设ffmpeg已正确输出JPEG
            return image_path
        
        try:
            with Image.open(image_path) as img:
                # 检查是否已经是JPEG格式
                if img.format == 'JPEG':
                    return image_path
                
                # 转换为JPEG格式
                # 如果有透明通道，需要先转换为RGB
                if img.mode in ('RGBA', 'LA', 'P'):
                    # 创建白色背景
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # 保存为JPEG
                jpeg_path = image_path.rsplit('.', 1)[0] + '.jpg'
                img.save(jpeg_path, 'JPEG', quality=85)
                
                # 如果原文件不是JPEG，删除原文件
                if jpeg_path != image_path and os.path.exists(image_path):
                    os.remove(image_path)
                
                return jpeg_path
                
        except Exception as e:
            logger.warning(f"[VideoParser] 图片格式转换失败: {e}")
            return image_path

    async def extract_audio_text(self, video_path: str) -> Optional[str]:
        """从视频中提取音频并转文字
        
        注意：此方法已废弃，ASR功能已移至 services/video_service.py 中实现。
        请使用 VideoService._extract_audio_text() 方法。
        
        ASR实现位置：
        - 音频提取：VideoService._extract_audio() (video_service.py:553)
        - 语音识别：VideoService._call_voice_model() (video_service.py:601)
        
        保留此方法是为了向后兼容，但始终返回None。
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            始终返回None（请使用VideoService中的ASR功能）
        """
        # ASR功能已移至 services/video_service.py 中实现
        # 请参考 VideoService._extract_audio_text() 方法
        return None

    def get_video_duration(self, video_path: str) -> Optional[float]:
        """获取视频时长
        
        Args:
            video_path: 视频文件路径
            
        Returns:
            时长(秒)
        """
        if not self.ffprobe_path:
            logger.warning("[VideoParser] ffprobe不可用，无法获取视频时长")
            return None
        
        try:
            cmd = [
                self.ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10
            )
            
            if result.returncode == 0:
                duration = float(result.stdout.decode().strip())
                return duration
            
        except Exception as e:
            logger.error(f"[VideoParser] 获取视频时长失败: {e}")
        
        return None

    async def parse_video(
        self,
        video_path: str,
        frame_interval: int = 6,
        max_frames: int = 10,
        enable_asr: bool = False
    ) -> Dict[str, Any]:
        """解析视频内容
        
        Args:
            video_path: 视频路径
            frame_interval: 抽帧间隔
            max_frames: 最大帧数
            enable_asr: 是否启用ASR
            
        Returns:
            解析结果字典
        """
        result = {
            "frames": [],
            "audio_text": None,
            "duration": None,
            "error": None
        }
        
        logger.debug(f"[VideoParser] 开始解析视频: {video_path}")
        
        try:
            # 获取视频时长
            duration = self.get_video_duration(video_path)
            result["duration"] = duration
            logger.debug(f"[VideoParser] 视频时长: {duration}s")
            
            # 抽取关键帧
            frames = await self.extract_frames(
                video_path,
                interval_sec=frame_interval,
                max_frames=max_frames
            )
            result["frames"] = frames
            
            logger.debug(f"[VideoParser] 抽帧完成: {len(frames)} 帧")
            
            # 提取音频文本(如果启用)
            if enable_asr:
                logger.debug("[VideoParser] 开始ASR音频识别...")
                audio_text = await self.extract_audio_text(video_path)
                result["audio_text"] = audio_text
                logger.debug(f"[VideoParser] ASR完成: {len(audio_text) if audio_text else 0} 字符")
            
        except Exception as e:
            logger.error(f"[VideoParser] 视频解析失败: {e}")
            result["error"] = str(e)
        
        return result