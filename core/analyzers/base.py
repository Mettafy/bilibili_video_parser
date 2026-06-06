"""视觉分析器抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseVisualAnalyzer(ABC):
    @abstractmethod
    async def analyze_frame(self, frame_path: str, prompt: str = "") -> str:
        raise NotImplementedError
