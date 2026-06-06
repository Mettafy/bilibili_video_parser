"""自动检测消息注入辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .message_formatter import MessageFormatter


@dataclass(slots=True)
class HookDecision:
    """Hook 阶段的决策结果。"""

    action: str
    modified_kwargs: dict[str, Any] | None = None
    custom_result: dict[str, Any] | None = None

    def to_hook_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"action": self.action}
        if self.modified_kwargs is not None:
            result["modified_kwargs"] = self.modified_kwargs
        if self.custom_result is not None:
            result["custom_result"] = self.custom_result
        return result


class HookInjector:
    """将解析结果注入 Hook 参数。"""

    def __init__(self, formatter: MessageFormatter) -> None:
        self._formatter = formatter

    def pass_through(self) -> HookDecision:
        return HookDecision(action="continue")

    def inject_text(self, *, kwargs: dict[str, Any], injected_text: str) -> HookDecision:
        message = kwargs.get("message")
        if message is None:
            return self.pass_through()

        original_text = self._get_original_text(message)
        merged_text = self._formatter.build_auto_detect_block(
            original_text=original_text,
            injected_text=injected_text,
        )
        modified_kwargs = dict(kwargs)
        modified_kwargs["message"] = self._rewrite_message_payload(message, merged_text)
        return HookDecision(
            action="continue",
            modified_kwargs=modified_kwargs,
            custom_result={"injected": True, "text_length": len(injected_text)},
        )

    def rewrite_text(self, *, kwargs: dict[str, Any], rewritten_text: str) -> HookDecision:
        """直接用最终文本重写当前消息。"""

        message = kwargs.get("message")
        if message is None:
            return self.pass_through()

        modified_kwargs = dict(kwargs)
        modified_kwargs["message"] = self._rewrite_message_payload(message, rewritten_text)
        return HookDecision(
            action="continue",
            modified_kwargs=modified_kwargs,
            custom_result={"rewritten": True, "text_length": len(rewritten_text)},
        )

    @staticmethod
    def _get_original_text(message: Any) -> str:
        if isinstance(message, dict):
            processed_plain_text = message.get("processed_plain_text")
            if isinstance(processed_plain_text, str) and processed_plain_text.strip():
                return processed_plain_text
            plain_text = message.get("plain_text")
            if isinstance(plain_text, str):
                return plain_text
            return ""

        processed_plain_text = message.processed_plain_text if hasattr(message, "processed_plain_text") else None
        if isinstance(processed_plain_text, str) and processed_plain_text.strip():
            return processed_plain_text

        plain_text = message.plain_text
        if isinstance(plain_text, str):
            return plain_text
        return ""

    @staticmethod
    def _rewrite_message_payload(message: Any, merged_text: str) -> Any:
        if isinstance(message, dict):
            rewritten_message = dict(message)
            rewritten_message["plain_text"] = merged_text
            rewritten_message["processed_plain_text"] = merged_text
            rewritten_message["raw_message"] = [{"type": "text", "data": {"text": merged_text}}]
            return rewritten_message

        if hasattr(message, "modify_plain_text"):
            message.modify_plain_text(merged_text)
        else:
            message.plain_text = merged_text

        if hasattr(message, "processed_plain_text"):
            message.processed_plain_text = merged_text
        return message
