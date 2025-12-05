# -*- coding: utf-8 -*-
"""
重试工具模块 - 提供统一的重试机制和错误分类

本模块提供了网络请求的重试机制和错误分类功能，用于处理：
1. 网络超时和连接错误
2. B站API返回的错误码
3. HTTP状态码错误

错误分类：
- 可重试错误（RetryableError）：网络超时、服务器错误、请求频繁等
- 不可重试错误（NonRetryableError）：视频不存在、无权限、文件过大等

错误类型枚举（ErrorType）：
- VIDEO_NOT_FOUND: 视频不存在/已删除
- VIDEO_TOO_LONG: 视频时长超限
- VIDEO_TOO_LARGE: 视频文件过大
- NETWORK_ERROR: 网络错误
- NO_CONTENT: 无法获取内容
- PERMISSION_DENIED: 无权限访问
- RATE_LIMITED: 请求过于频繁
- UNKNOWN: 未知错误

主要函数：
- classify_bilibili_error: 根据B站API错误码分类
- classify_http_error: 根据HTTP状态码分类
- get_friendly_error_message: 获取友好的错误提示
- retry_async: 异步重试函数
- with_retry: 重试装饰器

使用示例：
    # 使用重试函数
    async def fetch_data():
        # ... 网络请求
        pass
    
    result = await retry_async(
        fetch_data,
        max_attempts=3,
        interval_sec=2.0,
        retryable_exceptions=(RetryableError,)
    )
    
    # 使用装饰器
    @with_retry(max_attempts=3, interval_sec=2.0)
    async def fetch_with_retry():
        # ... 网络请求
        pass

B站API错误码参考：
- -404: 视频不存在
- -403: 无权限
- -504: 服务调用超时
- -509: 请求过于频繁
- -503: 服务不可用
- 62002: 稿件不可见
- 62004: 稿件审核中

Author: 约瑟夫.k && 白泽
"""
import asyncio
import functools
from enum import Enum
from typing import Optional, Callable, Any, Type, Tuple, Set
from src.plugin_system import get_logger

logger = get_logger("retry_utils")


class ErrorType(Enum):
    """错误类型枚举"""
    VIDEO_NOT_FOUND = "video_not_found"  # 视频不存在/已删除
    VIDEO_TOO_LONG = "video_too_long"  # 视频时长超限
    VIDEO_TOO_LARGE = "video_too_large"  # 视频文件过大
    NETWORK_ERROR = "network_error"  # 网络错误
    NO_CONTENT = "no_content"  # 无法获取内容（无字幕、抽帧失败）
    PERMISSION_DENIED = "permission_denied"  # 无权限访问
    RATE_LIMITED = "rate_limited"  # 请求过于频繁
    UNKNOWN = "unknown"  # 未知错误


# 可重试的错误类型
RETRYABLE_ERRORS: Set[ErrorType] = {
    ErrorType.NETWORK_ERROR,
    ErrorType.RATE_LIMITED,
}

# 命令模式的友好错误提示
ERROR_MESSAGES = {
    ErrorType.VIDEO_NOT_FOUND: "视频不存在或已被删除",
    ErrorType.VIDEO_TOO_LONG: "视频时长超过限制（>{limit}分钟）",
    ErrorType.VIDEO_TOO_LARGE: "视频文件过大（>{limit}MB）",
    ErrorType.NETWORK_ERROR: "网络连接失败，请稍后重试",
    ErrorType.NO_CONTENT: "无法获取视频内容",
    ErrorType.PERMISSION_DENIED: "视频需要登录或会员才能观看",
    ErrorType.RATE_LIMITED: "请求过于频繁，请稍后重试",
    ErrorType.UNKNOWN: "视频解析失败",
}


class RetryableError(Exception):
    """可重试的错误"""
    
    def __init__(self, message: str, error_type: ErrorType = ErrorType.NETWORK_ERROR):
        super().__init__(message)
        self.error_type = error_type


class NonRetryableError(Exception):
    """不可重试的错误"""
    
    def __init__(self, message: str, error_type: ErrorType = ErrorType.UNKNOWN):
        super().__init__(message)
        self.error_type = error_type


def classify_bilibili_error(code: int, message: str = "") -> Tuple[ErrorType, bool]:
    """根据B站API返回的错误码分类错误
    
    Args:
        code: B站API返回的错误码
        message: 错误消息
        
    Returns:
        (错误类型, 是否可重试)
    """
    # B站API错误码映射
    # 参考: https://github.com/SocialSisterYi/bilibili-API-collect
    
    # 不可重试的错误
    if code == -404:
        return ErrorType.VIDEO_NOT_FOUND, False
    if code == -403:
        return ErrorType.PERMISSION_DENIED, False
    if code == 62002:  # 稿件不可见
        return ErrorType.VIDEO_NOT_FOUND, False
    if code == 62004:  # 稿件审核中
        return ErrorType.VIDEO_NOT_FOUND, False
    
    # 可重试的错误
    if code == -504:  # 服务调用超时
        return ErrorType.NETWORK_ERROR, True
    if code == -509:  # 请求过于频繁
        return ErrorType.RATE_LIMITED, True
    if code == -503:  # 服务不可用
        return ErrorType.NETWORK_ERROR, True
    
    # 其他错误默认不可重试
    return ErrorType.UNKNOWN, False


def classify_http_error(status_code: int) -> Tuple[ErrorType, bool]:
    """根据HTTP状态码分类错误
    
    Args:
        status_code: HTTP状态码
        
    Returns:
        (错误类型, 是否可重试)
    """
    # 5xx 服务器错误 - 可重试
    if 500 <= status_code < 600:
        return ErrorType.NETWORK_ERROR, True
    
    # 429 请求过于频繁 - 可重试
    if status_code == 429:
        return ErrorType.RATE_LIMITED, True
    
    # 404 不存在 - 不可重试
    if status_code == 404:
        return ErrorType.VIDEO_NOT_FOUND, False
    
    # 403 无权限 - 不可重试
    if status_code == 403:
        return ErrorType.PERMISSION_DENIED, False
    
    # 其他4xx错误 - 不可重试
    if 400 <= status_code < 500:
        return ErrorType.UNKNOWN, False
    
    # 其他错误 - 不可重试
    return ErrorType.UNKNOWN, False


def get_friendly_error_message(error_type: ErrorType, **kwargs) -> str:
    """获取友好的错误提示消息
    
    Args:
        error_type: 错误类型
        **kwargs: 格式化参数（如 limit）
        
    Returns:
        友好的错误提示消息
    """
    template = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES[ErrorType.UNKNOWN])
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


async def retry_async(
    func: Callable,
    max_attempts: int = 3,
    interval_sec: float = 2.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> Any:
    """异步重试函数
    
    Args:
        func: 要执行的异步函数（无参数）
        max_attempts: 最大尝试次数
        interval_sec: 重试间隔（秒）
        retryable_exceptions: 可重试的异常类型
        on_retry: 重试时的回调函数，参数为 (当前尝试次数, 异常)
        
    Returns:
        函数执行结果
        
    Raises:
        最后一次尝试的异常
    """
    last_exception = None
    
    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except NonRetryableError:
            # 不可重试的错误，直接抛出
            raise
        except retryable_exceptions as e:
            last_exception = e
            
            if attempt < max_attempts:
                if on_retry:
                    on_retry(attempt, e)
                logger.debug(f"[Retry] 第{attempt}次尝试失败: {e}，{interval_sec}秒后重试")
                await asyncio.sleep(interval_sec)
            else:
                logger.warning(f"[Retry] 达到最大重试次数({max_attempts})，最后错误: {e}")
    
    if last_exception:
        raise last_exception
    
    return None


def with_retry(
    max_attempts: int = 3,
    interval_sec: float = 2.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
):
    """重试装饰器
    
    Args:
        max_attempts: 最大尝试次数
        interval_sec: 重试间隔（秒）
        retryable_exceptions: 可重试的异常类型
        
    Returns:
        装饰器函数
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            async def call():
                return await func(*args, **kwargs)
            
            return await retry_async(
                call,
                max_attempts=max_attempts,
                interval_sec=interval_sec,
                retryable_exceptions=retryable_exceptions,
            )
        return wrapper
    return decorator