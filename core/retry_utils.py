"""重试与错误分类。"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Tuple, Type


class ErrorType(Enum):
    VIDEO_NOT_FOUND = "video_not_found"
    VIDEO_TOO_LONG = "video_too_long"
    VIDEO_TOO_LARGE = "video_too_large"
    NETWORK_ERROR = "network_error"
    NO_CONTENT = "no_content"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


class RetryableError(Exception):
    def __init__(self, message: str, error_type: ErrorType = ErrorType.NETWORK_ERROR) -> None:
        super().__init__(message)
        self.error_type = error_type


class NonRetryableError(Exception):
    def __init__(self, message: str, error_type: ErrorType = ErrorType.UNKNOWN) -> None:
        super().__init__(message)
        self.error_type = error_type


def classify_http_error(status_code: int) -> Tuple[ErrorType, bool]:
    if 500 <= status_code < 600:
        return ErrorType.NETWORK_ERROR, True
    if status_code == 429:
        return ErrorType.RATE_LIMITED, True
    if status_code == 404:
        return ErrorType.VIDEO_NOT_FOUND, False
    if status_code == 403:
        return ErrorType.PERMISSION_DENIED, False
    if 400 <= status_code < 500:
        return ErrorType.UNKNOWN, False
    return ErrorType.UNKNOWN, False


def classify_bilibili_error(code: int, message: str = "") -> Tuple[ErrorType, bool]:
    del message
    if code == -404 or code == 62002 or code == 62004:
        return ErrorType.VIDEO_NOT_FOUND, False
    if code == -403:
        return ErrorType.PERMISSION_DENIED, False
    if code in (-504, -509, -503):
        return ErrorType.NETWORK_ERROR if code != -509 else ErrorType.RATE_LIMITED, True
    return ErrorType.UNKNOWN, False


async def retry_async(
    func: Callable[[], Awaitable[Any]],
    max_attempts: int = 3,
    interval_sec: float = 2.0,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except NonRetryableError:
            raise
        except retryable_exceptions as exc:
            last_error = exc
            if attempt < max_attempts:
                await asyncio.sleep(interval_sec)
    if last_error is not None:
        raise last_error
    return None
