"""ASR 服务。"""

from __future__ import annotations

import logging

from .asr.base import BaseAsrProvider


logger = logging.getLogger(__name__)


class AsrService:
    """ASR 统一入口。"""

    def __init__(self, provider: BaseAsrProvider | None) -> None:
        self._provider = provider

    async def transcribe(self, audio_path: str) -> str:
        if self._provider is None:
            logger.info("ASR 已跳过: provider 未配置, audio_path=%s", audio_path)
            return ""
        return await self._provider.transcribe(audio_path)
