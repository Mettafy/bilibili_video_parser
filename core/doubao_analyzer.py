# -*- coding: utf-8 -*-
"""
豆包视频理解模型分析器模块

本模块封装了火山引擎豆包（Doubao）视频理解模型的调用，
提供整体视频内容分析功能，无需抽帧即可理解视频内容。

豆包视频理解模型特点：
- 直接上传视频文件进行分析
- 支持自定义抽帧频率（fps）
- 适合较长视频的整体理解
- 相比VLM抽帧分析，能更好地理解视频的连续性和上下文

主要类：
- DoubaoAnalyzer: 豆包视频分析器

配置参数：
- api_key: 豆包API密钥（也可通过环境变量 ARK_API_KEY 设置）
- model_id: 模型ID（默认 "doubao-seed-1-6-251015"）
- fps: 抽帧频率（0.2-5），值越高理解越精细但token消耗越大
- base_url: API基础URL
- timeout: 请求超时时间（秒）
- max_retries: 最大重试次数
- retry_interval: 重试间隔（秒）
- video_prompt: 自定义视频分析提示词

工作流程：
1. 上传视频文件到豆包服务器
2. 等待视频预处理完成
3. 调用视频理解API获取分析结果

使用示例：
    config = {
        "api_key": "your-api-key",
        "model_id": "doubao-seed-1-6-251015",
        "fps": 1.0,
    }
    analyzer = DoubaoAnalyzer(config)
    
    # 分析视频
    result = await analyzer.analyze_video("/path/to/video.mp4")
    
    # 检查服务可用性
    available = await analyzer.is_available()

依赖：
- volcenginesdkarkruntime: 火山引擎SDK

注意：
- 采用懒加载策略，首次调用时才初始化客户端
- 如果未安装SDK，会在初始化时报错
- 视频上传和处理可能需要较长时间

Author: 约瑟夫.k && 白泽
"""
import os
import asyncio
from typing import Optional, Dict, Any
from src.plugin_system import get_logger

logger = get_logger("doubao_analyzer")


class DoubaoAnalyzer:
    """豆包视频理解模型分析器
    
    使用火山引擎豆包视频理解模型分析视频内容
    """
    
    DEFAULT_VIDEO_PROMPT = """请详细描述这个视频的内容（{summary_min_chars}-{summary_max_chars}字以内），包括：
1. 视频的主要场景和环境
2. 出现的人物、物体及其特征（如是已知作品角色请指出名称和作品）
3. 发生的主要事件和动作（按时间顺序）
4. 视频的整体氛围和风格
5. 任何出现的文字、标志或重要信息（如字幕、对话等）

仅描述视频中实际出现的内容，不要推测或编造未出现的信息。
若某些信息无法从视频中判断，请明确说明'无法判断'。
请用简洁清晰的语言描述，突出关键信息。"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化豆包分析器
        
        Args:
            config: 豆包配置字典，包含api_key, model_id, fps等
        """
        self.config = config
        self._client = None
        self._initialized = False
        
    async def _ensure_initialized(self) -> bool:
        """确保客户端已初始化（懒加载）"""
        if self._initialized:
            return self._client is not None
            
        self._initialized = True
        
        try:
            # 尝试导入豆包SDK
            from volcenginesdkarkruntime import AsyncArk
            
            # 获取API密钥
            api_key = self.config.get("api_key", "") or os.getenv("ARK_API_KEY", "")
            if not api_key:
                logger.error("[DoubaoAnalyzer] 未配置豆包API密钥，请在配置文件中设置doubao.api_key或设置环境变量ARK_API_KEY")
                return False
            
            base_url = self.config.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
            
            self._client = AsyncArk(
                base_url=base_url,
                api_key=api_key
            )
            
            logger.debug("[DoubaoAnalyzer] 豆包客户端初始化成功")
            return True
            
        except ImportError:
            logger.error("[DoubaoAnalyzer] 未安装豆包SDK，请运行: pip install volcengine-python-sdk[ark]")
            return False
        except Exception as e:
            logger.error(f"[DoubaoAnalyzer] 初始化失败: {e}")
            return False
    
    async def analyze_video(self, video_path: str, custom_prompt: str = "") -> Optional[str]:
        """分析视频内容
        
        采用动态参数传递策略：
        - 只传递用户在配置中实际定义的参数
        - 不同版本的豆包API可能支持不同的参数
        - 用户可以自由添加豆包特有的参数
        
        Args:
            video_path: 本地视频文件路径
            custom_prompt: 自定义提示词（可选）
            
        Returns:
            视频分析结果文本，失败返回None
        """
        if not await self._ensure_initialized():
            return None
            
        if not os.path.exists(video_path):
            logger.error(f"[DoubaoAnalyzer] 视频文件不存在: {video_path}")
            return None
        
        # 必需参数
        model_id = self.config.get("model_id", "doubao-seed-1-6-251015")
        timeout = self.config.get("timeout", 120)
        max_retries = self.config.get("max_retries", 2)
        retry_interval = self.config.get("retry_interval", 10)
        
        # 获取字数配置
        summary_min_chars = self.config.get("summary_min_chars", 100)
        summary_max_chars = self.config.get("summary_max_chars", 150)
        
        # 获取提示词并格式化字数范围
        prompt = custom_prompt or self.config.get("video_prompt", "") or self.DEFAULT_VIDEO_PROMPT
        try:
            prompt = prompt.format(
                summary_min_chars=summary_min_chars,
                summary_max_chars=summary_max_chars
            )
        except KeyError:
            # 如果用户自定义提示词没有使用占位符，忽略格式化错误
            pass
        
        # 构建动态视频预处理配置
        # 只传递用户配置的参数
        video_preprocess_config = {}
        if "fps" in self.config and self.config["fps"] is not None:
            video_preprocess_config["fps"] = self.config["fps"]
        
        # 已知的非API参数（不应传递给API）
        # 这些参数用于本地处理，不应传递给豆包API
        non_api_params = {
            "api_key", "model_id", "base_url", "timeout", "max_retries",
            "retry_interval", "video_prompt", "visual_max_duration_min",
            "summary_min_chars", "summary_max_chars"  # 用于格式化提示词，不传递给API
        }
        
        # 构建动态API参数
        api_params = {
            "model": model_id,
        }
        
        # 动态添加可选参数（只有用户配置了才传递）
        optional_api_params = ["temperature", "max_tokens", "top_p", "top_k"]
        for param in optional_api_params:
            if param in self.config and self.config[param] is not None:
                api_params[param] = self.config[param]
        
        # 添加用户自定义的额外参数（豆包特有参数）
        for key, value in self.config.items():
            if key not in non_api_params and key not in optional_api_params and key not in api_params and key != "fps":
                if value is not None:
                    api_params[key] = value
        
        logger.debug(f"[DoubaoAnalyzer] API参数: {list(api_params.keys())}")
        logger.debug(f"[DoubaoAnalyzer] 视频预处理配置: {video_preprocess_config}")
        
        for attempt in range(max_retries + 1):
            try:
                logger.debug(f"[DoubaoAnalyzer] 开始上传视频文件 (尝试 {attempt + 1}/{max_retries + 1})")
                
                # 上传视频文件（动态构建预处理配置）
                upload_kwargs = {
                    "file": open(video_path, "rb"),
                    "purpose": "user_data",
                }
                if video_preprocess_config:
                    upload_kwargs["preprocess_configs"] = {"video": video_preprocess_config}
                
                file = await self._client.files.create(**upload_kwargs)
                
                logger.debug(f"[DoubaoAnalyzer] 视频上传成功: {file.id}，等待处理...")
                
                # 等待视频处理完成
                await self._client.files.wait_for_processing(file.id)
                logger.debug(f"[DoubaoAnalyzer] 视频处理完成: {file.id}")
                
                # 构建请求参数
                request_params = {
                    **api_params,
                    "input": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "input_video",
                                "file_id": file.id
                            },
                            {
                                "type": "input_text",
                                "text": prompt
                            }
                        ]
                    }]
                }
                
                # 调用视频理解API
                logger.debug("[DoubaoAnalyzer] 开始视频理解分析...")
                response = await asyncio.wait_for(
                    self._client.responses.create(**request_params),
                    timeout=timeout
                )
                
                # 提取响应文本
                if response and hasattr(response, 'output'):
                    # 从output中提取文本
                    for item in response.output:
                        if hasattr(item, 'content'):
                            for content in item.content:
                                if hasattr(content, 'text'):
                                    result = content.text
                                    logger.debug(f"[DoubaoAnalyzer] 视频分析完成，结果长度: {len(result)}")
                                    return result
                
                # 尝试其他响应格式
                if hasattr(response, 'choices') and response.choices:
                    result = response.choices[0].message.content
                    logger.debug(f"[DoubaoAnalyzer] 视频分析完成，结果长度: {len(result)}")
                    return result
                    
                logger.warning(f"[DoubaoAnalyzer] 无法解析响应: {response}")
                return None
                
            except asyncio.TimeoutError:
                logger.warning(f"[DoubaoAnalyzer] 请求超时 (尝试 {attempt + 1}/{max_retries + 1})")
                if attempt < max_retries:
                    await asyncio.sleep(retry_interval)
                    
            except Exception as e:
                logger.error(f"[DoubaoAnalyzer] 分析失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(retry_interval)
        
        logger.error("[DoubaoAnalyzer] 视频分析失败，已达最大重试次数")
        return None
    
    async def is_available(self) -> bool:
        """检查豆包服务是否可用"""
        return await self._ensure_initialized()