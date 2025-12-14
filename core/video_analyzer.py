# -*- coding: utf-8 -*-
"""
视频分析模块 - 使用VLM模型分析视频帧

本模块提供视频帧的视觉分析功能，支持两种VLM模式：
1. MaiBot VLM模式（默认）：使用MaiBot主程序配置的VLM模型
2. 插件内置VLM模式：使用插件独立配置的VLM服务

主要类：
- VideoAnalyzer: 视频分析器

功能：
- 单帧分析：分析单张图片，返回内容描述
- 视频分析：分析多帧图片，结合字幕生成视频总结

VLM模式选择：
- default: 使用MaiBot主程序的VLM模型
- builtin: 使用插件内置的VLM客户端（需配置API）

懒加载策略：
- 首次调用 is_initialized() 或 analyze_frame() 时才初始化模型
- 避免插件加载时的性能开销
- 初始化失败后不会重复尝试

帧分析提示词：
- 默认提示词：请用一句中文描述这张图片的主要内容，少于25字
- 可通过配置自定义提示词

使用示例：
    # 使用MaiBot VLM
    analyzer = VideoAnalyzer()
    
    # 使用插件内置VLM
    config = {
        "use_builtin": True,
        "client_type": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "your-api-key",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
    }
    analyzer = VideoAnalyzer(vlm_config=config)
    
    # 检查初始化状态
    if analyzer.is_initialized():
        # 分析单帧
        description = await analyzer.analyze_frame("/path/to/frame.jpg")
        
        # 分析整个视频
        summary = await analyzer.analyze_video(
            frame_paths=[...],
            video_info={"title": "...", "duration": 300},
            subtitle_text="..."
        )

依赖：
- src.plugin_system.llm_api: MaiBot LLM API
- src.llm_models.payload_content.message: 消息构建器
- builtin_vlm: 内置VLM客户端（可选）

Author: 约瑟夫.k && 白泽
"""
import base64
import os
from typing import List, Optional, Dict, Any
from src.plugin_system import llm_api, get_logger
from src.llm_models.payload_content.message import Message, MessageBuilder, RoleType

logger = get_logger("video_analyzer")


class VideoAnalyzer:
    """视频分析器 - 使用VLM模型分析视频内容
    
    支持两种模式：
    1. 使用MaiBot主程序的VLM模型（默认）
    2. 使用插件内置的VLM客户端（需配置）
    
    采用懒加载策略：在首次使用时自动初始化模型配置
    """
    
    def __init__(self, vlm_config: Optional[Dict[str, Any]] = None):
        """初始化视频分析器（不进行模型初始化，采用懒加载）
        
        Args:
            vlm_config: VLM配置字典（可选），用于内置VLM模式
        """
        self.vlm_config = vlm_config or {}
        self.vlm_model = None
        self.replyer_model = None
        self._builtin_vlm = None
        self._initialized = False
        self._init_attempted = False  # 标记是否已尝试初始化
        self._use_builtin = self.vlm_config.get("use_builtin", False)
    
    def set_config(self, vlm_config: Dict[str, Any]):
        """设置VLM配置
        
        Args:
            vlm_config: VLM配置字典
        """
        self.vlm_config = vlm_config
        self._use_builtin = vlm_config.get("use_builtin", False)
        # 重置初始化状态，以便使用新配置
        self._initialized = False
        self._init_attempted = False
        self._builtin_vlm = None
        
    def _ensure_initialized(self) -> bool:
        """确保模型已初始化（懒加载）
        
        Returns:
            bool: 是否初始化成功
        """
        if self._initialized:
            return True
            
        if self._init_attempted:
            # 已经尝试过初始化但失败了，不再重试
            return False
            
        self._init_attempted = True
        
        try:
            # 检查是否使用内置VLM
            if self._use_builtin:
                return self._init_builtin_vlm()
            else:
                return self._init_maibot_vlm()
                
        except Exception as e:
            logger.error(f"[VideoAnalyzer] 懒加载初始化失败: {e}")
            return False
    
    def _init_maibot_vlm(self) -> bool:
        """初始化MaiBot主程序的VLM模型"""
        try:
            models = llm_api.get_available_models()
            
            self.vlm_model = models.get('vlm')
            self.replyer_model = models.get('replyer')
            
            if not self.vlm_model:
                logger.warning("[VideoAnalyzer] 未找到VLM模型配置，将使用replyer模型替代")
                self.vlm_model = self.replyer_model
            if not self.replyer_model:
                logger.error("[VideoAnalyzer] 未找到replyer模型配置")
                return False
            else:
                self._initialized = True
                logger.info("[BilibiliVideoParser] VLM模型初始化成功")
                return True
        except Exception as e:
            logger.error(f"[VideoAnalyzer] MaiBot VLM初始化失败: {e}")
            return False
    
    def _init_builtin_vlm(self) -> bool:
        """初始化插件内置VLM客户端"""
        try:
            from .builtin_vlm import BuiltinVLMClient
            
            self._builtin_vlm = BuiltinVLMClient(self.vlm_config)
            
            # 同时初始化replyer模型（用于生成总结）
            models = llm_api.get_available_models()
            self.replyer_model = models.get('replyer')
            
            if not self.replyer_model:
                logger.warning("[VideoAnalyzer] 未找到replyer模型配置，总结功能可能受限")
            
            self._initialized = True
            logger.info("[BilibiliVideoParser] 内置VLM客户端初始化成功")
            return True
        except ImportError as e:
            logger.error(f"[VideoAnalyzer] 无法导入内置VLM模块: {e}")
            return False
        except Exception as e:
            logger.error(f"[VideoAnalyzer] 内置VLM初始化失败: {e}")
            return False
    
    async def initialize(self):
        """初始化模型配置（兼容旧接口，实际使用懒加载）"""
        # 调用同步的懒加载方法
        self._ensure_initialized()
    
    def is_initialized(self) -> bool:
        """检查是否已初始化
        
        注意：此方法会触发懒加载初始化
        """
        return self._ensure_initialized()
    
    async def analyze_frame(self, frame_path: str, custom_prompt: str = "") -> Optional[str]:
        """分析单帧图片
        
        Args:
            frame_path: 图片文件路径
            custom_prompt: 自定义提示词（可选）
            
        Returns:
            帧的描述文本
        """
        if not self._ensure_initialized():
            logger.error("[VideoAnalyzer] 模型未初始化")
            return "未识别"
        
        logger.debug(f"[VideoAnalyzer] 开始分析帧: {frame_path}")
        
        # 使用内置VLM
        if self._use_builtin and self._builtin_vlm:
            result = await self._analyze_frame_builtin(frame_path, custom_prompt)
            logger.debug(f"[VideoAnalyzer] 内置VLM分析结果: {result}")
            return result
        
        # 使用MaiBot VLM
        result = await self._analyze_frame_maibot(frame_path, custom_prompt)
        logger.debug(f"[VideoAnalyzer] MaiBot VLM分析结果: {result}")
        return result
    
    async def _analyze_frame_builtin(self, frame_path: str, custom_prompt: str = "") -> Optional[str]:
        """使用内置VLM分析帧"""
        if not self._builtin_vlm:
            return "未识别"
        
        # 约束部分（始终追加到提示词末尾）
        constraint_suffix = "仅描述画面中实际出现的内容，不要推测或编造。若无法判断，请回答'未识别'。"
        
        # 获取用户自定义提示词或配置中的提示词
        user_prompt = custom_prompt or self.vlm_config.get("frame_prompt", "")
        
        if user_prompt:
            # 用户设置了自定义提示词，追加约束部分
            prompt = f"{user_prompt} {constraint_suffix}"
        else:
            # 使用默认提示词
            prompt = f"请用一句中文描述这张视频截图的画面要点，少于25字。{constraint_suffix}"
        
        try:
            result = await self._builtin_vlm.analyze_frame(frame_path, prompt)
            if result:
                # 清理响应文本
                description = result.strip()
                return description
            return "未识别"
        except Exception as e:
            logger.error(f"[VideoAnalyzer] 内置VLM分析帧异常: {e}")
            return "未识别"
    
    async def _analyze_frame_maibot(self, frame_path: str, custom_prompt: str = "") -> Optional[str]:
        """使用MaiBot VLM分析帧"""
        if not self.vlm_model:
            logger.error("[VideoAnalyzer] VLM模型未初始化")
            return "未识别"
        
        # 约束部分（始终追加到提示词末尾）
        constraint_suffix = "仅描述画面中实际出现的内容，不要推测或编造。若无法判断，请回答'未识别'。"
        
        # 获取用户自定义提示词或配置中的提示词
        user_prompt = custom_prompt or self.vlm_config.get("frame_prompt", "")
        
        if user_prompt:
            # 用户设置了自定义提示词，追加约束部分
            prompt = f"{user_prompt} {constraint_suffix}"
        else:
            # 使用默认提示词
            prompt = f"请用一句中文描述这张视频截图的画面要点，少于25字。{constraint_suffix}"
        
        try:
            # 读取图片文件并转为base64
            with open(frame_path, 'rb') as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            
            # 获取图片格式（从文件扩展名）
            _, ext = os.path.splitext(frame_path)
            image_format = ext.lower().lstrip('.')
            if image_format == 'jpg':
                image_format = 'jpeg'
            
            # 使用MessageBuilder构建带图片的消息
            def message_factory(client):
                """构建带图片的消息"""
                try:
                    message = (
                        MessageBuilder()
                        .set_role(RoleType.User)
                        .add_text_content(prompt)
                        .add_image_content(image_format, image_data)
                        .build()
                    )
                    return [message]
                except Exception as e:
                    logger.warning(f"[VideoAnalyzer] 构建图片消息失败: {e}，回退到纯文本")
                    # 回退到纯文本方式
                    message = (
                        MessageBuilder()
                        .set_role(RoleType.User)
                        .add_text_content(prompt)
                        .build()
                    )
                    return [message]
            
            # 调用VLM模型（使用message_factory方式）
            result = await llm_api.generate_with_model_with_tools_by_message_factory(
                message_factory=message_factory,
                model_config=self.vlm_model,
                tool_options=None,
                request_type="plugin.video_frame_analysis"
            )
            
            success, response, reasoning, model_name, tool_calls = result
            
            if success and response:
                # 清理响应文本
                description = response.strip()
                return description
            else:
                logger.warning(f"[VideoAnalyzer] VLM分析失败: {response}")
                return "未识别"
                
        except Exception as e:
            logger.error(f"[VideoAnalyzer] MaiBot VLM分析帧异常: {e}")
            return "未识别"
    
    async def analyze_video(
        self,
        frame_paths: List[str],
        video_info: Dict[str, Any],
        subtitle_text: Optional[str] = None
    ) -> Optional[str]:
        """分析整个视频
        
        Args:
            frame_paths: 所有帧的路径列表
            video_info: 视频基本信息
            subtitle_text: 字幕文本
            
        Returns:
            视频总结文本
        """
        if not self._ensure_initialized():
            logger.error("[VideoAnalyzer] 模型未初始化")
            return None
        
        if not self.replyer_model:
            logger.error("[VideoAnalyzer] Replyer模型未初始化")
            return None
        
        if not frame_paths:
            logger.warning("[VideoAnalyzer] 没有可分析的帧")
            return None
        
        logger.debug(f"[VideoAnalyzer] 开始分析视频: {video_info.get('title', '未知')}, 帧数: {len(frame_paths)}")
        
        try:
            title = video_info.get('title', '未知标题')
            duration = video_info.get('duration')
            
            # 第一步：分析关键帧，获取每帧的描述
            # 硬编码限制：最多分析5帧，避免过多API调用
            frame_descriptions = []
            MAX_ANALYZE_FRAMES = 5  # 硬编码：最大VLM分析帧数
            max_analyze_frames = min(len(frame_paths), MAX_ANALYZE_FRAMES)
            logger.debug(f"[VideoAnalyzer] 将分析 {max_analyze_frames} 帧")
            
            for idx, frame_path in enumerate(frame_paths[:max_analyze_frames], start=1):
                description = await self.analyze_frame(frame_path)
                if description and description != "未识别":
                    frame_descriptions.append(f"帧{idx}: {description}")
                else:
                    frame_descriptions.append(f"帧{idx}: 画面内容未识别")
            
            # 第二步：构建最终总结提示词
            # 构建元信息
            meta_parts = [f"视频标题: {title}"]
            if duration:
                minutes = int(duration) // 60
                seconds = int(duration) % 60
                if minutes > 0:
                    meta_parts.append(f"时长: {minutes}分{seconds}秒")
                else:
                    meta_parts.append(f"时长: {seconds}秒")
            meta_parts.append(f"分析帧数: {len(frame_descriptions)}")
            meta_block = "\n".join(meta_parts)
            
            # 构建帧描述
            frames_block = "\n".join(frame_descriptions) if frame_descriptions else "无帧描述"
            
            # 构建字幕块（不截断，保留完整内容）
            subtitle_block = ""
            if subtitle_text:
                subtitle_block = f"\n\n视频字幕内容:\n{subtitle_text}"
            
            # 构建最终提示词 - 直接输出总结内容，不要任何说明或格式标记
            final_prompt = (
                f"根据以下B站视频信息，直接输出一段简洁的视频内容总结（80-120字）。\n"
                f"只输出总结内容本身，不要输出任何标题、说明、解释、格式标记或改写说明。\n\n"
                f"视频标题: {title}\n"
                f"{meta_block}\n\n"
                f"关键帧描述:\n{frames_block}"
                f"{subtitle_block}"
            )
            
            # 调用replyer模型生成最终总结
            logger.debug("[VideoAnalyzer] 调用replyer模型生成总结...")
            success, summary, reasoning, model_name = await llm_api.generate_with_model(
                prompt=final_prompt,
                model_config=self.replyer_model,
                request_type="plugin.video_summary"
            )
            
            if success and summary:
                logger.debug(f"[VideoAnalyzer] 总结生成成功: {len(summary)} 字符")
                # 清理总结文本
                summary = summary.strip()
                # 移除可能的引号包裹
                if summary.startswith('"') and summary.endswith('"'):
                    summary = summary[1:-1]
                if summary.startswith("'") and summary.endswith("'"):
                    summary = summary[1:-1]
                # 移除可能的"改写说明"等标记
                summary = self._clean_summary(summary)
                    
                return summary
            else:
                logger.error(f"[VideoAnalyzer] 生成总结失败: {summary}")
                return None
                
        except Exception as e:
            logger.error(f"[VideoAnalyzer] 分析视频异常: {e}")
            return None
    
    def _clean_summary(self, summary: str) -> str:
        """清理总结文本，移除不需要的标记和说明
        
        Args:
            summary: 原始总结文本
            
        Returns:
            清理后的总结文本
        """
        import re
        
        # 移除常见的不需要的标记
        patterns_to_remove = [
            r'【?改写说明】?[：:].+',  # 移除"改写说明"及其后的内容
            r'【?说明】?[：:].+',
            r'【?注】?[：:].+',
            r'\*\*.+\*\*',  # 移除markdown加粗
            r'^\s*[-•]\s*',  # 移除列表标记
        ]
        
        lines = summary.split('\n')
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 检查是否包含需要移除的模式
            should_skip = False
            for pattern in patterns_to_remove:
                if re.search(pattern, line, re.IGNORECASE):
                    should_skip = True
                    break
            
            if not should_skip:
                cleaned_lines.append(line)
        
        # 合并清理后的行
        result = ' '.join(cleaned_lines)
        
        # 如果结果为空，返回原始内容的第一行
        if not result.strip() and summary.strip():
            first_line = summary.strip().split('\n')[0]
            return first_line
        
        return result