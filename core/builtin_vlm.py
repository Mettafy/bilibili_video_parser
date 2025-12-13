# -*- coding: utf-8 -*-
"""
插件内置VLM客户端模块

本模块提供独立于MaiBot主程序的VLM（视觉语言模型）客户端，
允许插件使用自定义的VLM服务进行视频帧分析。

支持的API格式：
1. OpenAI兼容格式：支持所有兼容OpenAI API的服务
   - OpenAI官方API
   - Azure OpenAI
   - 硅基流动（SiliconFlow）
   - 其他兼容服务
   
2. Google Gemini格式：支持Google Gemini系列模型
   - gemini-1.5-flash
   - gemini-1.5-pro
   - 等

主要类：
- BuiltinVLMClient: 内置VLM客户端

配置参数：
- client_type: API类型（"openai" 或 "gemini"）
- base_url: API基础URL
- api_key: API密钥
- model: 模型标识符
- temperature: 生成温度（0-1）
- max_tokens: 最大输出token数
- timeout: 请求超时时间（秒）
- max_retries: 最大重试次数
- retry_interval: 重试间隔（秒）
- frame_prompt: 自定义帧分析提示词

使用示例：
    config = {
        "client_type": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "your-api-key",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
    }
    client = BuiltinVLMClient(config)
    
    # 分析单帧
    description = await client.analyze_frame("/path/to/frame.jpg")
    
    # 批量分析
    results = await client.analyze_frames_batch(["/path/to/frame1.jpg", "/path/to/frame2.jpg"])

依赖：
- openai: OpenAI Python SDK（可选，用于OpenAI格式）
- google-generativeai: Google Gemini SDK（可选，用于Gemini格式）
- PIL: 图片处理（Gemini格式需要）

注意：
- 采用懒加载策略，首次调用时才初始化客户端
- 如果未安装对应SDK，会在初始化时报错

Author: 约瑟夫.k && 白泽
"""
import base64
import asyncio
from typing import Optional, Dict, Any, List
from pathlib import Path
from src.plugin_system import get_logger

logger = get_logger("builtin_vlm")


class BuiltinVLMClient:
    """插件内置VLM客户端
    
    支持OpenAI和Gemini两种API格式
    """
    
    DEFAULT_FRAME_PROMPT = """请用中文描述这张视频截图的内容：
1. 场景环境（时间、地点、背景）
2. 人物/角色（外观、服装、动作）- 如是已知作品角色请指出名称和作品
3. 画面中的文字（字幕、对话框、界面文本等）
4. 正在发生的事件或动作

仅描述画面中实际出现的内容，不要推测或编造未出现的信息。
输出为一段连贯的描述文本。"""
    
    def __init__(self, config: Dict[str, Any]):
        """初始化VLM客户端
        
        Args:
            config: VLM配置字典
        """
        self.config = config
        self._openai_client = None
        self._gemini_client = None
        self._initialized = False
        
    async def _ensure_initialized(self) -> bool:
        """确保客户端已初始化（懒加载）"""
        if self._initialized:
            return self._openai_client is not None or self._gemini_client is not None
            
        self._initialized = True
        
        client_type = self.config.get("client_type", "openai").lower()
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "")
        
        if not api_key:
            logger.error("[BuiltinVLM] 未配置API密钥")
            return False
            
        try:
            if client_type == "gemini":
                return await self._init_gemini_client(api_key, base_url)
            else:
                return await self._init_openai_client(api_key, base_url)
        except Exception as e:
            logger.error(f"[BuiltinVLM] 初始化失败: {e}")
            return False
    
    async def _init_openai_client(self, api_key: str, base_url: str) -> bool:
        """初始化OpenAI兼容客户端"""
        try:
            from openai import AsyncOpenAI
            
            self._openai_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url if base_url else None
            )
            logger.debug(f"[BuiltinVLM] OpenAI客户端初始化成功: {base_url}")
            return True
        except ImportError:
            logger.error("[BuiltinVLM] 未安装openai库，请运行: pip install openai")
            return False
        except Exception as e:
            logger.error(f"[BuiltinVLM] OpenAI客户端初始化失败: {e}")
            return False
    
    async def _init_gemini_client(self, api_key: str, base_url: str) -> bool:
        """初始化Gemini客户端"""
        try:
            import google.generativeai as genai
            
            genai.configure(api_key=api_key)
            self._gemini_client = genai
            logger.debug("[BuiltinVLM] Gemini客户端初始化成功")
            return True
        except ImportError:
            logger.error("[BuiltinVLM] 未安装google-generativeai库，请运行: pip install google-generativeai")
            return False
        except Exception as e:
            logger.error(f"[BuiltinVLM] Gemini客户端初始化失败: {e}")
            return False
    
    def _encode_image_to_base64(self, image_path: str) -> Optional[str]:
        """将图片编码为base64"""
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error(f"[BuiltinVLM] 图片编码失败: {e}")
            return None
    
    def _get_image_mime_type(self, image_path: str) -> str:
        """获取图片MIME类型"""
        suffix = Path(image_path).suffix.lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        return mime_types.get(suffix, "image/jpeg")
    
    async def analyze_frame(self, image_path: str, custom_prompt: str = "") -> Optional[str]:
        """分析单帧图片
        
        Args:
            image_path: 图片文件路径
            custom_prompt: 自定义提示词（可选）
            
        Returns:
            分析结果文本，失败返回None
        """
        if not await self._ensure_initialized():
            return None
            
        prompt = custom_prompt or self.config.get("frame_prompt", "") or self.DEFAULT_FRAME_PROMPT
        
        client_type = self.config.get("client_type", "openai").lower()
        
        if client_type == "gemini":
            return await self._analyze_with_gemini(image_path, prompt)
        else:
            return await self._analyze_with_openai(image_path, prompt)
    
    async def _analyze_with_openai(self, image_path: str, prompt: str) -> Optional[str]:
        """使用OpenAI兼容API分析图片
        
        采用动态参数传递策略：
        - 只传递用户在配置中实际定义的参数
        - 不同API服务商可能支持不同的参数
        - 用户可以自由添加服务商特有的参数
        """
        if not self._openai_client:
            return None
            
        # 必需参数
        model = self.config.get("model", "gpt-4-vision-preview")
        timeout = self.config.get("timeout", 60)
        max_retries = self.config.get("max_retries", 2)
        retry_interval = self.config.get("retry_interval", 5)
        
        # 编码图片
        image_base64 = self._encode_image_to_base64(image_path)
        if not image_base64:
            return None
            
        mime_type = self._get_image_mime_type(image_path)
        
        # 构建动态API参数
        # 只传递用户配置的参数，不传递未配置的参数
        api_params = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
        }
        
        # 动态添加可选参数（只有用户配置了才传递）
        # 这些参数不同服务商可能不支持
        optional_params = [
            "temperature", "max_tokens", "top_p", "top_k",
            "presence_penalty", "frequency_penalty", "stop",
            "seed", "logprobs", "top_logprobs", "n"
        ]
        
        # 已知的非API参数（不应传递给API）
        non_api_params = {
            "client_type", "base_url", "api_key", "model", "timeout",
            "max_retries", "retry_interval", "frame_prompt", "use_builtin",
            "visual_method", "visual_max_duration_min", "frame_interval_sec"
        }
        
        for param in optional_params:
            if param in self.config and self.config[param] is not None:
                api_params[param] = self.config[param]
        
        # 添加用户自定义的额外参数（服务商特有参数）
        for key, value in self.config.items():
            if key not in non_api_params and key not in optional_params and key not in api_params:
                if value is not None:
                    api_params[key] = value
        
        logger.debug(f"[BuiltinVLM] API参数: {list(api_params.keys())}")
        
        for attempt in range(max_retries + 1):
            try:
                response = await asyncio.wait_for(
                    self._openai_client.chat.completions.create(**api_params),
                    timeout=timeout
                )
                
                if response.choices and response.choices[0].message:
                    return response.choices[0].message.content
                    
                return None
                
            except asyncio.TimeoutError:
                logger.warning(f"[BuiltinVLM] OpenAI请求超时 (尝试 {attempt + 1}/{max_retries + 1})")
                if attempt < max_retries:
                    await asyncio.sleep(retry_interval)
            except Exception as e:
                logger.error(f"[BuiltinVLM] OpenAI请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(retry_interval)
        
        return None
    
    async def _analyze_with_gemini(self, image_path: str, prompt: str) -> Optional[str]:
        """使用Gemini API分析图片
        
        采用动态参数传递策略：
        - 只传递用户在配置中实际定义的参数
        - Gemini API有自己特有的参数格式
        """
        if not self._gemini_client:
            return None
            
        # 必需参数
        model = self.config.get("model", "gemini-1.5-flash")
        timeout = self.config.get("timeout", 60)
        max_retries = self.config.get("max_retries", 2)
        retry_interval = self.config.get("retry_interval", 5)
        
        for attempt in range(max_retries + 1):
            try:
                # 加载图片
                import PIL.Image
                image = PIL.Image.open(image_path)
                
                # 创建模型
                gemini_model = self._gemini_client.GenerativeModel(model)
                
                # 构建动态生成配置
                # 只传递用户配置的参数
                generation_config_params = {}
                
                # Gemini支持的生成参数映射
                gemini_param_mapping = {
                    "temperature": "temperature",
                    "max_tokens": "max_output_tokens",  # Gemini使用不同的参数名
                    "top_p": "top_p",
                    "top_k": "top_k",
                    "stop": "stop_sequences",  # Gemini使用不同的参数名
                }
                
                for config_key, gemini_key in gemini_param_mapping.items():
                    if config_key in self.config and self.config[config_key] is not None:
                        generation_config_params[gemini_key] = self.config[config_key]
                
                # 添加用户自定义的Gemini特有参数
                # 已知的非生成配置参数
                non_generation_params = {
                    "client_type", "base_url", "api_key", "model", "timeout",
                    "max_retries", "retry_interval", "frame_prompt", "use_builtin",
                    "visual_method", "visual_max_duration_min", "frame_interval_sec",
                    "temperature", "max_tokens", "top_p", "top_k", "stop"
                }
                
                for key, value in self.config.items():
                    if key not in non_generation_params and value is not None:
                        # 用户自定义的额外参数，直接传递给generation_config
                        generation_config_params[key] = value
                
                logger.debug(f"[BuiltinVLM] Gemini生成配置参数: {list(generation_config_params.keys())}")
                
                # 创建生成配置（如果有参数的话）
                generation_config = None
                if generation_config_params:
                    generation_config = self._gemini_client.types.GenerationConfig(
                        **generation_config_params
                    )
                
                # 异步调用
                if generation_config:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            gemini_model.generate_content,
                            [prompt, image],
                            generation_config=generation_config
                        ),
                        timeout=timeout
                    )
                else:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            gemini_model.generate_content,
                            [prompt, image]
                        ),
                        timeout=timeout
                    )
                
                if response and response.text:
                    return response.text
                    
                return None
                
            except asyncio.TimeoutError:
                logger.warning(f"[BuiltinVLM] Gemini请求超时 (尝试 {attempt + 1}/{max_retries + 1})")
                if attempt < max_retries:
                    await asyncio.sleep(retry_interval)
            except Exception as e:
                logger.error(f"[BuiltinVLM] Gemini请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(retry_interval)
        
        return None
    
    async def analyze_frames_batch(self, image_paths: List[str], custom_prompt: str = "") -> List[Optional[str]]:
        """批量分析多帧图片
        
        Args:
            image_paths: 图片文件路径列表
            custom_prompt: 自定义提示词（可选）
            
        Returns:
            分析结果列表
        """
        results = []
        for path in image_paths:
            result = await self.analyze_frame(path, custom_prompt)
            results.append(result)
        return results
    
    async def is_available(self) -> bool:
        """检查VLM服务是否可用"""
        return await self._ensure_initialized()