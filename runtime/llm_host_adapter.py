"""宿主 LLM 能力适配器。"""

from __future__ import annotations

import math
import logging

logger = logging.getLogger(__name__)


from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class HostTaskValidationResult:
    """宿主任务名校验结果。"""

    available_tasks: set[str]
    visual_task_ready: bool
    summary_task_ready: bool


@dataclass(slots=True)
class HostTimeoutPolicy:
    """宿主能力调用超时策略。"""

    enabled: bool
    request_timeout_min: float | None

    def for_generation(self) -> float | None:
        if not self.enabled:
            return None
        return self.request_timeout_min

    def for_discovery(self) -> float | None:
        if not self.enabled:
            return None
        return self.request_timeout_min


class LLMHostAdapter:
    """封装宿主 LLM 能力调用与任务校验。"""

    def __init__(
        self,
        ctx: Any,
        *,
        timeout_policy: HostTimeoutPolicy | None = None,
    ) -> None:
        self._ctx = ctx
        self._task_cache: set[str] = set()
        self._timeout_policy = timeout_policy or HostTimeoutPolicy(enabled=False, request_timeout_min=None)

    async def refresh_available_tasks(self) -> set[str]:
        payload: dict[str, Any] = {}
        discovery_timeout_min = self._timeout_policy.for_discovery()
        if discovery_timeout_min is not None:
            payload["rpc_timeout_ms"] = self.build_timeout_ms(discovery_timeout_min)
        logger.info(
            "宿主任务发现请求: task_name=%s, timeout_policy_enabled=%s, effective_rpc_timeout_ms=%s",
            "llm.get_available_models",
            self._timeout_policy.enabled,
            payload.get("rpc_timeout_ms"),
        )
        raw_models = await self._ctx.call_capability("llm.get_available_models", **payload)
        tasks = self._extract_task_names(raw_models)
        self._task_cache = tasks
        return tasks

    async def generate_text(
        self,
        *,
        prompt: Any,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        timeout_min: float | None = None,
        configured_summary_max_chars: int | None = None,
    ) -> dict[str, Any]:
        effective_timeout_min = self._timeout_policy.for_generation() if timeout_min is None else timeout_min
        payload: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
        }
        if effective_timeout_min is not None:
            payload["rpc_timeout_ms"] = self.build_timeout_ms(effective_timeout_min)
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        logger.info(
            "宿主文本生成请求: task_name=%s, timeout_policy_enabled=%s, effective_rpc_timeout_ms=%s, configured_max_tokens=%s, effective_request_max_tokens=%s, configured_summary_max_chars=%s",
            model,
            self._timeout_policy.enabled,
            payload.get("rpc_timeout_ms"),
            max_tokens,
            payload.get("max_tokens"),
            configured_summary_max_chars,
        )
        result = await self._ctx.call_capability("llm.generate", **payload)
        return result if isinstance(result, dict) else {"success": False, "response": "", "error": "宿主返回格式无效"}

    @staticmethod
    def build_timeout_ms(timeout_min: float) -> int:
        normalized_timeout_min = float(timeout_min)
        if not math.isfinite(normalized_timeout_min) or normalized_timeout_min <= 0:
            raise ValueError(f"宿主超时预算无效: {timeout_min}")
        timeout_ms = int(math.ceil(normalized_timeout_min * 60 * 1000))
        return max(1000, timeout_ms)

    @staticmethod
    def build_timeout_sec(timeout_min: float) -> int:
        timeout_ms = LLMHostAdapter.build_timeout_ms(timeout_min)
        return max(1, int(math.ceil(timeout_ms / 1000)))

    async def validate_tasks(self, *, visual_task_name: str, summary_task_name: str) -> HostTaskValidationResult:
        available_tasks = await self.refresh_available_tasks()
        return HostTaskValidationResult(
            available_tasks=available_tasks,
            visual_task_ready=visual_task_name in available_tasks,
            summary_task_ready=summary_task_name in available_tasks,
        )

    def resolve_preferred_task(self, preferred_task_name: str, fallback_candidates: list[str], *, allow_fallback: bool = True) -> str:
        normalized_preferred = str(preferred_task_name or "").strip()
        if normalized_preferred and normalized_preferred in self._task_cache:
            return normalized_preferred
        if not allow_fallback:
            return normalized_preferred
        for candidate in fallback_candidates:
            normalized_candidate = str(candidate or "").strip()
            if normalized_candidate in self._task_cache:
                return normalized_candidate
        if self._task_cache:
            return next(iter(sorted(self._task_cache)))
        return normalized_preferred

    def get_cached_tasks(self) -> set[str]:
        return set(self._task_cache)

    @property
    def ctx(self) -> Any:
        return self._ctx

    @property
    def timeout_policy(self) -> HostTimeoutPolicy:
        return self._timeout_policy

    @staticmethod
    def _extract_task_names(raw_models: Any) -> set[str]:
        task_names: set[str] = set()
        if isinstance(raw_models, dict):
            for key in raw_models.keys():
                normalized_key = str(key or "").strip()
                if normalized_key:
                    task_names.add(normalized_key)
        elif isinstance(raw_models, list):
            for item in raw_models:
                if isinstance(item, dict):
                    task_name = str(item.get("task_name", "") or item.get("name", "")).strip()
                    if task_name:
                        task_names.add(task_name)
                else:
                    normalized_item = str(item or "").strip()
                    if normalized_item:
                        task_names.add(normalized_item)
        return task_names
