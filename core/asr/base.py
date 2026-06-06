"""ASR 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAsrProvider(ABC):
    """ASR 供应商接口。"""

    @abstractmethod
    async def transcribe(self, audio_path: str) -> str:
        raise NotImplementedError
