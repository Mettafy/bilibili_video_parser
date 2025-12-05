# -*- coding: utf-8 -*-
"""
总结服务模块 - 封装总结生成和个性化回复的完整流程

本模块提供视频内容总结和个性化回复生成的高层服务接口，包括：
1. 视频内容总结生成（80-120字客观描述）
2. 个性化回复生成（结合麦麦人设）
3. 原生信息文本构建

主要类：
- SummaryService: 总结服务
- SummaryResult: 总结结果数据类

总结生成模式：
1. VLM帧分析模式（default/builtin）：
   - 分析每帧图片获取描述
   - 结合字幕/ASR生成总结
   
2. 豆包模式（doubao）：
   - 使用豆包视频理解结果
   - 结合字幕/ASR生成总结
   
3. 纯文本模式（none）：
   - 仅使用字幕/ASR和视频信息
   - 适用于超长视频

个性化回复生成：
- 读取麦麦的人设配置（昵称、性格、兴趣、说话风格）
- 结合视频内容生成符合人设的回复
- 支持两种方式：
  - generate_personalized_reply: 基于总结生成
  - generate_personalized_reply_from_raw_info: 基于原生信息生成

原生信息文本构建：
- build_raw_info_text: 构建格式化的视频信息文本
- 包含标题、UP主、时长、简介、字幕、帧描述等
- 支持多P视频信息显示

SummaryResult 字段：
- success: 是否成功
- error: 错误信息
- raw_summary: 原始总结（80-120字）
- personalized_reply: 个性化回复

使用示例：
    service = SummaryService(video_analyzer, get_config)
    
    # 生成总结
    result = await service.generate_summary(
        frame_paths=[...],
        video_info={"title": "...", "duration": 300},
        text_content="字幕内容...",
        visual_method="default"
    )
    
    # 生成个性化回复（基于总结）
    reply = await service.generate_personalized_reply(
        raw_summary=result.raw_summary,
        video_info={...}
    )
    
    # 生成个性化回复（基于原生信息）
    reply = await service.generate_personalized_reply_from_raw_info(
        video_info={...},
        raw_info={...}
    )
    
    # 构建原生信息文本
    text = service.build_raw_info_text(video_info, raw_info)

依赖：
- video_analyzer: 视频分析器（用于帧分析）
- src.plugin_system.llm_api: MaiBot LLM API
- src.config.config: MaiBot配置（用于获取人设）

Author: 约瑟夫.k && 白泽
"""
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass
from src.plugin_system import llm_api, get_logger

logger = get_logger("summary_service")


@dataclass
class SummaryResult:
    """总结结果"""
    success: bool = False
    error: Optional[str] = None
    raw_summary: Optional[str] = None  # 原始总结
    personalized_reply: Optional[str] = None  # 个性化回复（命令模式使用）


class SummaryService:
    """总结服务"""
    
    def __init__(
        self,
        video_analyzer,  # VideoAnalyzer实例
        get_config: Callable[[str, Any], Any]  # 配置获取函数
    ):
        """初始化总结服务
        
        Args:
            video_analyzer: VideoAnalyzer实例
            get_config: 配置获取函数
        """
        self.video_analyzer = video_analyzer
        self.get_config = get_config
        self._replyer_model = None
        self._replyer_model_checked = False
    
    def _get_replyer_model(self):
        """懒加载获取回复模型"""
        if self._replyer_model_checked:
            return self._replyer_model
        
        self._replyer_model_checked = True
        try:
            models = llm_api.get_available_models()
            self._replyer_model = models.get('replyer')
            if not self._replyer_model:
                logger.warning("[SummaryService] 未找到回复模型配置")
        except Exception as e:
            logger.warning(f"[SummaryService] 获取回复模型失败: {e}")
        
        return self._replyer_model
    
    async def generate_summary(
        self,
        frame_paths: List[str],
        video_info: Dict[str, Any],
        text_content: Optional[str] = None,
        visual_analysis: Optional[str] = None,
        visual_method: str = "default"
    ) -> SummaryResult:
        """生成视频总结
        
        支持多种模式：
        1. default/builtin模式：使用VLM帧分析结果生成总结
        2. 豆包模式：使用豆包视频理解结果生成总结
        3. 纯文本模式：仅使用字幕/ASR生成总结
        
        Args:
            frame_paths: 帧图片路径列表（VLM模式使用）
            video_info: 视频信息字典，包含title、description、duration、author等
            text_content: 文本内容（字幕或ASR结果）
            visual_analysis: 视觉分析结果（豆包模式使用）
            visual_method: 视觉分析方式：default、builtin、doubao、none
            
        Returns:
            SummaryResult: 总结结果
        """
        result = SummaryResult()
        logger.debug(f"[SummaryService] 开始生成总结: visual_method={visual_method}")
        
        try:
            # 构建视频信息字典
            title = video_info.get('title', '未知标题')
            description = video_info.get('description', '')
            author = video_info.get('author', '')
            duration = video_info.get('duration')
            video_id = video_info.get('video_id', '')
            
            # 根据视觉分析方式选择不同的总结生成策略
            if visual_method == "doubao" and visual_analysis:
                # 豆包模式：使用豆包视频理解结果
                logger.debug("[SummaryService] 使用豆包模式生成总结")
                summary = await self._generate_summary_from_doubao(
                    visual_analysis=visual_analysis,
                    title=title,
                    description=description,
                    author=author,
                    duration=duration,
                    text_content=text_content
                )
            elif visual_method in ("default", "builtin") and frame_paths:
                # VLM模式（default使用MaiBot VLM，builtin使用插件内置VLM）：使用帧分析
                logger.debug(f"[SummaryService] 使用VLM模式生成总结: {visual_method}")
                if not self.video_analyzer or not self.video_analyzer.is_initialized():
                    result.error = "视频分析器未初始化"
                    return result
                
                summary = await self._analyze_video_with_description(
                    frame_paths=frame_paths,
                    title=title,
                    description=description,
                    author=author,
                    duration=duration,
                    video_id=video_id,
                    text_content=text_content
                )
            else:
                # 纯文本模式：仅使用字幕/ASR和视频信息
                logger.debug("[SummaryService] 使用纯文本模式生成总结")
                summary = await self._generate_summary_text_only(
                    title=title,
                    description=description,
                    author=author,
                    duration=duration,
                    text_content=text_content
                )
            
            if summary:
                result.success = True
                result.raw_summary = summary
                logger.debug(f"[SummaryService] 总结生成成功: {len(summary)} 字符")
            else:
                result.error = "生成总结失败"
                logger.debug("[SummaryService] 总结生成失败")
            
            return result
            
        except Exception as e:
            logger.error(f"[SummaryService] 生成总结异常: {e}")
            result.error = str(e)
            return result
    
    async def _generate_summary_from_doubao(
        self,
        visual_analysis: str,
        title: str,
        description: str,
        author: str,
        duration: Optional[int],
        text_content: Optional[str]
    ) -> Optional[str]:
        """使用豆包视频理解结果生成总结
        
        Args:
            visual_analysis: 豆包视频理解结果
            title: 视频标题
            description: 视频简介
            author: UP主名称
            duration: 视频时长
            text_content: 文本内容（字幕或ASR）
            
        Returns:
            总结文本
        """
        replyer_model = self._get_replyer_model()
        if not replyer_model:
            logger.error("[SummaryService] 回复模型未配置")
            return None
        
        try:
            # 构建元信息
            meta_parts = [f"视频标题: {title}"]
            if author:
                meta_parts.append(f"UP主: {author}")
            if duration:
                minutes = int(duration) // 60
                seconds = int(duration) % 60
                if minutes > 0:
                    meta_parts.append(f"时长: {minutes}分{seconds}秒")
                else:
                    meta_parts.append(f"时长: {seconds}秒")
            meta_block = "\n".join(meta_parts)
            
            # 构建视频简介块
            description_block = ""
            if description:
                max_desc_len = 500
                if len(description) > max_desc_len:
                    description = description[:max_desc_len] + "..."
                description_block = f"\n\n视频简介:\n{description}"
            
            # 构建字幕/ASR块
            text_block = ""
            if text_content:
                max_text_len = 800
                if len(text_content) > max_text_len:
                    text_content = text_content[:max_text_len] + "..."
                text_block = f"\n\n视频字幕/语音内容:\n{text_content}"
            
            # 构建提示词
            final_prompt = (
                f"根据以下B站视频信息，以客观第三方视角输出一段简洁的视频内容总结（80-120字）。\n"
                f"要求：\n"
                f"1. 只描述视频的客观内容，不要加入主观评价或感受\n"
                f"2. 不要使用'你'、'我'等人称代词\n"
                f"3. 不要说'这是一段XX制作的视频'，直接描述视频内容\n"
                f"4. 只输出总结内容本身，不要输出任何标题、说明、解释、格式标记\n\n"
                f"{meta_block}"
                f"{description_block}\n\n"
                f"视频内容分析（AI视觉理解）:\n{visual_analysis}"
                f"{text_block}"
            )
            
            success, summary, reasoning, model_name = await llm_api.generate_with_model(
                prompt=final_prompt,
                model_config=replyer_model,
                request_type="plugin.video_summary"
            )
            
            if success and summary:
                summary = summary.strip()
                if summary.startswith('"') and summary.endswith('"'):
                    summary = summary[1:-1]
                if summary.startswith("'") and summary.endswith("'"):
                    summary = summary[1:-1]
                
                return summary
            else:
                logger.error(f"[SummaryService] 生成总结失败: {summary}")
                return None
                
        except Exception as e:
            logger.error(f"[SummaryService] 豆包模式总结异常: {e}")
            return None
    
    async def _generate_summary_text_only(
        self,
        title: str,
        description: str,
        author: str,
        duration: Optional[int],
        text_content: Optional[str]
    ) -> Optional[str]:
        """仅使用文本信息生成总结（无视觉分析）
        
        Args:
            title: 视频标题
            description: 视频简介
            author: UP主名称
            duration: 视频时长
            text_content: 文本内容（字幕或ASR）
            
        Returns:
            总结文本
        """
        replyer_model = self._get_replyer_model()
        if not replyer_model:
            logger.error("[SummaryService] 回复模型未配置")
            return None
        
        try:
            # 构建元信息
            meta_parts = [f"视频标题: {title}"]
            if author:
                meta_parts.append(f"UP主: {author}")
            if duration:
                minutes = int(duration) // 60
                seconds = int(duration) % 60
                if minutes > 0:
                    meta_parts.append(f"时长: {minutes}分{seconds}秒")
                else:
                    meta_parts.append(f"时长: {seconds}秒")
            meta_block = "\n".join(meta_parts)
            
            # 构建视频简介块
            description_block = ""
            if description:
                max_desc_len = 500
                if len(description) > max_desc_len:
                    description = description[:max_desc_len] + "..."
                description_block = f"\n\n视频简介:\n{description}"
            
            # 构建字幕/ASR块
            text_block = ""
            if text_content:
                max_text_len = 1200  # 纯文本模式允许更长的字幕
                if len(text_content) > max_text_len:
                    text_content = text_content[:max_text_len] + "..."
                text_block = f"\n\n视频字幕/语音内容:\n{text_content}"
            
            # 如果没有任何文本内容，只能基于标题和简介
            if not text_content and not description:
                logger.warning("[SummaryService] 无字幕和简介，仅基于标题生成总结")
            
            # 构建提示词
            final_prompt = (
                f"根据以下B站视频信息，以客观第三方视角输出一段简洁的视频内容总结（80-120字）。\n"
                f"注意：由于视频时长较长，未进行视觉分析，请主要基于字幕/语音内容和视频简介进行总结。\n"
                f"要求：\n"
                f"1. 只描述视频的客观内容，不要加入主观评价或感受\n"
                f"2. 不要使用'你'、'我'等人称代词\n"
                f"3. 不要说'这是一段XX制作的视频'，直接描述视频内容\n"
                f"4. 只输出总结内容本身，不要输出任何标题、说明、解释、格式标记\n\n"
                f"{meta_block}"
                f"{description_block}"
                f"{text_block}"
            )
            
            success, summary, reasoning, model_name = await llm_api.generate_with_model(
                prompt=final_prompt,
                model_config=replyer_model,
                request_type="plugin.video_summary"
            )
            
            if success and summary:
                summary = summary.strip()
                if summary.startswith('"') and summary.endswith('"'):
                    summary = summary[1:-1]
                if summary.startswith("'") and summary.endswith("'"):
                    summary = summary[1:-1]
                
                return summary
            else:
                logger.error(f"[SummaryService] 生成总结失败: {summary}")
                return None
                
        except Exception as e:
            logger.error(f"[SummaryService] 纯文本模式总结异常: {e}")
            return None
    
    async def _analyze_video_with_description(
        self,
        frame_paths: List[str],
        title: str,
        description: str,
        author: str,
        duration: Optional[int],
        video_id: str,
        text_content: Optional[str]
    ) -> Optional[str]:
        """分析视频并生成总结（支持视频简介和作者信息）
        
        Args:
            frame_paths: 帧图片路径列表
            title: 视频标题
            description: 视频简介
            author: UP主名称
            duration: 视频时长
            video_id: 视频ID
            text_content: 文本内容（字幕或ASR）
            
        Returns:
            总结文本
        """
        if not self.video_analyzer or not self.video_analyzer.is_initialized():
            logger.error("[SummaryService] 视频分析器未初始化")
            return None
        
        if not self.video_analyzer.replyer_model:
            logger.error("[SummaryService] Replyer模型未初始化")
            return None
        
        if not frame_paths:
            logger.warning("[SummaryService] 没有可分析的帧")
            return None
        
        logger.debug(f"[SummaryService] 开始VLM帧分析: {len(frame_paths)} 帧")
        
        try:
            # 第一步：分析关键帧，获取每帧的描述
            # 硬编码限制：最多分析5帧，避免过多API调用
            frame_descriptions = []
            MAX_ANALYZE_FRAMES = 5  # 硬编码：最大VLM分析帧数
            max_analyze_frames = min(len(frame_paths), MAX_ANALYZE_FRAMES)
            logger.debug(f"[SummaryService] 将分析 {max_analyze_frames} 帧")
            
            for idx, frame_path in enumerate(frame_paths[:max_analyze_frames], start=1):
                desc = await self.video_analyzer.analyze_frame(frame_path)
                if desc and desc != "未识别":
                    frame_descriptions.append(f"帧{idx}: {desc}")
                else:
                    frame_descriptions.append(f"帧{idx}: 画面内容未识别")
            
            # 第二步：构建最终总结提示词
            # 构建元信息
            meta_parts = [f"视频标题: {title}"]
            if author:
                meta_parts.append(f"UP主: {author}")
            if duration:
                minutes = int(duration) // 60
                seconds = int(duration) % 60
                if minutes > 0:
                    meta_parts.append(f"时长: {minutes}分{seconds}秒")
                else:
                    meta_parts.append(f"时长: {seconds}秒")
            meta_parts.append(f"分析帧数: {len(frame_descriptions)}")
            meta_block = "\n".join(meta_parts)
            
            # 构建视频简介块
            description_block = ""
            if description:
                # 限制简介长度
                max_desc_len = 500
                if len(description) > max_desc_len:
                    description = description[:max_desc_len] + "..."
                description_block = f"\n\n视频简介:\n{description}"
            
            # 构建帧描述
            frames_block = "\n".join(frame_descriptions) if frame_descriptions else "无帧描述"
            
            # 构建字幕/ASR块
            text_block = ""
            if text_content:
                max_text_len = 800
                if len(text_content) > max_text_len:
                    text_content = text_content[:max_text_len] + "..."
                text_block = f"\n\n视频字幕/语音内容:\n{text_content}"
            
            # 构建最终提示词（客观视角）
            final_prompt = (
                f"根据以下B站视频信息，以客观第三方视角输出一段简洁的视频内容总结（80-120字）。\n"
                f"要求：\n"
                f"1. 只描述视频的客观内容，不要加入主观评价或感受\n"
                f"2. 不要使用'你'、'我'等人称代词\n"
                f"3. 不要说'这是一段XX制作的视频'，直接描述视频内容\n"
                f"4. 只输出总结内容本身，不要输出任何标题、说明、解释、格式标记\n\n"
                f"{meta_block}"
                f"{description_block}\n\n"
                f"关键帧描述:\n{frames_block}"
                f"{text_block}"
            )
            
            # 调用replyer模型生成最终总结
            success, summary, reasoning, model_name = await llm_api.generate_with_model(
                prompt=final_prompt,
                model_config=self.video_analyzer.replyer_model,
                request_type="plugin.video_summary"
            )
            
            if success and summary:
                # 清理总结文本
                summary = summary.strip()
                if summary.startswith('"') and summary.endswith('"'):
                    summary = summary[1:-1]
                if summary.startswith("'") and summary.endswith("'"):
                    summary = summary[1:-1]
                summary = self.video_analyzer._clean_summary(summary)
                return summary
            else:
                logger.error(f"[SummaryService] 生成总结失败: {summary}")
                return None
                
        except Exception as e:
            logger.error(f"[SummaryService] 分析视频异常: {e}")
            return None
    
    async def generate_personalized_reply(
        self,
        raw_summary: str,
        video_info: Dict[str, Any]
    ) -> Optional[str]:
        """生成个性化回复（命令模式使用，基于总结）
        
        结合麦麦的人设、说话风格、兴趣生成回复
        
        Args:
            raw_summary: 原始视频总结
            video_info: 视频信息，包含title、author、description、duration等
            
        Returns:
            个性化回复文本
        """
        logger.debug("[SummaryService] 开始生成个性化回复（基于总结）")
        
        try:
            # 获取麦麦的人设信息
            from src.config.config import global_config
            
            bot_name = global_config.bot.nickname
            bot_alias = global_config.bot.alias_names
            personality = global_config.personality.personality
            reply_style = global_config.personality.reply_style
            interest = global_config.personality.interest
            
            # 构建昵称部分
            nickname_part = ""
            if bot_alias:
                nickname_part = f"，也有人叫你{'、'.join(bot_alias)}"
            
            title = video_info.get('title', '未知标题')
            author = video_info.get('author', '')
            description = video_info.get('description', '')
            duration = video_info.get('duration')
            
            # 构建时长描述
            duration_desc = ""
            if duration:
                minutes = int(duration) // 60
                seconds = int(duration) % 60
                if minutes > 0:
                    duration_desc = f"时长{minutes}分{seconds}秒"
                else:
                    duration_desc = f"时长{seconds}秒"
            
            # 构建视频信息块
            video_info_parts = [f"标题：《{title}》"]
            if author:
                video_info_parts.append(f"UP主：{author}")
            if duration_desc:
                video_info_parts.append(duration_desc)
            if description:
                # 限制简介长度
                max_desc_len = 150
                if len(description) > max_desc_len:
                    description = description[:max_desc_len] + "..."
                video_info_parts.append(f"简介：{description}")
            
            video_info_block = "\n".join(video_info_parts)
            
            # 构建提示词
            prompt = f"""你是{bot_name}{nickname_part}，{personality}
你的兴趣是：{interest}

用户发送了一个B站视频链接，想让你看看这个视频。

视频信息：
{video_info_block}

视频内容总结：
{raw_summary}

请根据你的人设和兴趣，用你的说话风格给出日常且口语化的回复，平淡一些，分享你对这个视频的看法或感受。
你的说话风格是：{reply_style}
要求：
- 尽量简短，像日常聊天一样
- 不要太有条理，可以有个性
- 不要用"这个视频讲的是..."这种总结式开头
- 直接输出回复内容，不要输出多余内容（前后缀、冒号、引号、括号、表情包、at/@等）"""
            
            replyer_model = self._get_replyer_model()
            if not replyer_model:
                logger.error("[SummaryService] 回复模型未配置")
                return None
            
            success, reply, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=replyer_model,
                request_type="plugin.video_personalized_reply"
            )
            
            if success and reply:
                reply = reply.strip()
                # 清理可能的引号包裹
                if reply.startswith('"') and reply.endswith('"'):
                    reply = reply[1:-1]
                if reply.startswith("'") and reply.endswith("'"):
                    reply = reply[1:-1]
                
                return reply
            else:
                logger.error(f"[SummaryService] 生成个性化回复失败: {reply}")
                return None
                
        except Exception as e:
            logger.error(f"[SummaryService] 生成个性化回复异常: {e}")
            return None
    
    async def generate_personalized_reply_from_raw_info(
        self,
        video_info: Dict[str, Any],
        raw_info: Dict[str, Any]
    ) -> Optional[str]:
        """使用原生视频信息生成个性化回复（命令模式使用，无需总结）
        
        直接使用原生视频信息（标题、简介、字幕、帧描述等）生成个性化回复，
        跳过生成最终总结的环节，降低模型压力。
        
        Args:
            video_info: 视频基本信息，包含title、author、description、duration等
            raw_info: 原生视频信息，包含subtitle_text、asr_text、frame_descriptions、visual_analysis等
            
        Returns:
            个性化回复文本
        """
        logger.debug("[SummaryService] 开始生成个性化回复（基于原生信息）")
        
        try:
            # 获取麦麦的人设信息
            from src.config.config import global_config
            
            bot_name = global_config.bot.nickname
            bot_alias = global_config.bot.alias_names
            personality = global_config.personality.personality
            reply_style = global_config.personality.reply_style
            interest = global_config.personality.interest
            
            # 构建昵称部分
            nickname_part = ""
            if bot_alias:
                nickname_part = f"，也有人叫你{'、'.join(bot_alias)}"
            
            title = video_info.get('title', '未知标题')
            author = video_info.get('author', '')
            description = video_info.get('description', '')
            duration = video_info.get('duration')
            total_duration = video_info.get('total_duration')
            page = video_info.get('page', 1)
            page_title = video_info.get('page_title', '')
            total_pages = video_info.get('total_pages', 1)
            
            # 构建标题（包含分P信息）
            if total_pages > 1:
                if page_title:
                    title_text = f"标题：《{title}》P{page}「{page_title}」"
                else:
                    title_text = f"标题：《{title}》P{page}"
            else:
                title_text = f"标题：《{title}》"
            
            # 构建视频基本信息块
            video_info_parts = [title_text]
            if author:
                video_info_parts.append(f"UP主：{author}")
            
            # 时长显示逻辑
            if total_pages > 1:
                # 多P视频：显示当前分P时长和合集总时长
                if duration:
                    video_info_parts.append(f"当前分P时长：{self._format_duration(duration)}")
                if total_duration:
                    video_info_parts.append(f"合集总时长：{self._format_duration(total_duration)}（共{total_pages}P）")
            else:
                # 单P视频：只显示时长
                if duration:
                    video_info_parts.append(f"时长：{self._format_duration(duration)}")
            
            if description:
                video_info_parts.append(f"简介：{description}")
            
            video_info_block = "\n".join(video_info_parts)
            
            # 构建原生内容块
            raw_content_parts = []
            
            # 字幕内容
            subtitle_text = raw_info.get('subtitle_text', '')
            if subtitle_text:
                raw_content_parts.append(f"【字幕内容】\n{subtitle_text}")
            
            # ASR识别内容
            asr_text = raw_info.get('asr_text', '')
            if asr_text:
                raw_content_parts.append(f"【语音识别内容】\n{asr_text}")
            
            # 帧描述
            frame_descriptions = raw_info.get('frame_descriptions', [])
            if frame_descriptions:
                frames_text = "\n".join(frame_descriptions)
                raw_content_parts.append(f"【画面描述】\n{frames_text}")
            
            # 豆包视觉分析结果
            visual_analysis = raw_info.get('visual_analysis', '')
            if visual_analysis:
                raw_content_parts.append(f"【视频内容分析】\n{visual_analysis}")
            
            raw_content_block = "\n\n".join(raw_content_parts) if raw_content_parts else "（无详细内容）"
            
            # 构建提示词
            prompt = f"""你是{bot_name}{nickname_part}，{personality}
你的兴趣是：{interest}

用户发送了一个B站视频链接，想让你看看这个视频。

视频信息：
{video_info_block}

视频详细内容：
{raw_content_block}

请根据你的人设和兴趣，用你的说话风格给出日常且口语化的回复，平淡一些，分享你对这个视频的看法或感受。
你的说话风格是：{reply_style}
要求：
- 尽量简短，像日常聊天一样
- 不要太有条理，可以有个性
- 不要用"这个视频讲的是..."这种总结式开头
- 直接输出回复内容，不要输出多余内容（前后缀、冒号、引号、括号、表情包、at/@等）"""
            
            replyer_model = self._get_replyer_model()
            if not replyer_model:
                logger.error("[SummaryService] 回复模型未配置")
                return None
            
            success, reply, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=replyer_model,
                request_type="plugin.video_personalized_reply"
            )
            
            if success and reply:
                reply = reply.strip()
                # 清理可能的引号包裹
                if reply.startswith('"') and reply.endswith('"'):
                    reply = reply[1:-1]
                if reply.startswith("'") and reply.endswith("'"):
                    reply = reply[1:-1]
                
                return reply
            else:
                logger.error(f"[SummaryService] 生成个性化回复失败: {reply}")
                return None
                
        except Exception as e:
            logger.error(f"[SummaryService] 生成个性化回复异常: {e}")
            return None
    
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
    
    def build_raw_info_text(
        self,
        video_info: Dict[str, Any],
        raw_info: Dict[str, Any]
    ) -> str:
        """构建原生视频信息文本（用于自动检测模式，enable_summary=false时）
        
        Args:
            video_info: 视频基本信息
            raw_info: 原生视频信息
            
        Returns:
            格式化的原生信息文本
        """
        title = video_info.get('title', '未知标题')
        author = video_info.get('author', '')
        description = video_info.get('description', '')
        duration = video_info.get('duration')
        total_duration = video_info.get('total_duration')
        page = video_info.get('page', 1)
        page_title = video_info.get('page_title', '')
        total_pages = video_info.get('total_pages', 1)
        
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
            parts.append(f"简介：{description}")
        
        # 添加原生内容
        subtitle_text = raw_info.get('subtitle_text', '')
        if subtitle_text:
            parts.append(f"字幕内容：{subtitle_text}")
        
        asr_text = raw_info.get('asr_text', '')
        if asr_text:
            parts.append(f"语音识别内容：{asr_text}")
        
        frame_descriptions = raw_info.get('frame_descriptions', [])
        if frame_descriptions:
            frames_text = "；".join(frame_descriptions)
            parts.append(f"画面描述：{frames_text}")
        
        visual_analysis = raw_info.get('visual_analysis', '')
        if visual_analysis:
            parts.append(f"视频内容分析：{visual_analysis}")
        
        return "\n".join(parts)