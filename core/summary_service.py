"""视频总结服务。"""

from __future__ import annotations

import asyncio
import logging
import re

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import BilibiliVideoMetadata
from .text_render_service import TextRenderService, format_duration


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SummaryResult:
    success: bool = False
    error: Optional[str] = None
    raw_summary: Optional[str] = None
    frame_descriptions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SummaryPromptSnapshot:
    mode: str
    meta_block: str
    description_block: str
    card_visual_block: str
    frames_block: str
    text_block: str
    custom_prompt_raw: str
    custom_prompt_body: str
    prompt_final: str


class SummaryService:
    """负责视频总结生成与文本构造。"""

    def __init__(self, text_renderer: TextRenderService, host_llm_adapter: Any) -> None:
        self._text_renderer = text_renderer
        self._host_llm_adapter = host_llm_adapter

    @staticmethod
    def normalize_summary_text(text: str) -> str:
        normalized = str(text or "").strip()
        if normalized.startswith('"') and normalized.endswith('"'):
            normalized = normalized[1:-1]
        if normalized.startswith("'") and normalized.endswith("'"):
            normalized = normalized[1:-1]
        return normalized.strip()

    def is_bad_summary(self, text: str) -> bool:
        normalized = self.normalize_summary_text(text)
        if not normalized:
            return False
        compact = re.sub(r"\s+", " ", normalized).strip().lower()
        exact_bad_cases = {
            "未识别",
            "无法识别",
            "无法判断",
            "none",
            "null",
            "n/a",
            "error",
        }
        if compact in exact_bad_cases:
            return True
        bad_patterns = [
            r"抱歉.*无法",
            r"作为.?ai",
            r"无法访问",
            r"请求失败",
            r"系统繁忙",
            r"服务异常",
            r"模型错误",
            r"生成失败",
            r"视频内容总结[:：]?\s*$",
        ]
        return any(re.search(pattern, compact, re.IGNORECASE) for pattern in bad_patterns)

    def sanitize_cached_summary(self, payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
        if not isinstance(payload, dict):
            return payload, False
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return payload, False
        if not self.is_bad_summary(summary):
            payload["summary"] = self.normalize_summary_text(summary)
            return payload, False
        payload["summary"] = None
        return payload, True

    @staticmethod
    def _sanitize_summary_max_chars(summary_max_chars: int, fallback: int = 200) -> int:
        value = int(summary_max_chars) if isinstance(summary_max_chars, (int, float)) else fallback
        return value if value > 0 else fallback

    def _format_custom_prompt(self, *, metadata: BilibiliVideoMetadata, body: str, summary_max_chars: int, custom_prompt: str) -> str:
        return custom_prompt.strip().format(
            title=metadata.title,
            author=metadata.author,
            duration=format_duration(metadata.duration_sec or 0) if metadata.duration_sec else "",
            description=metadata.description,
            content=body,
            summary_max_chars=summary_max_chars,
        )

    @staticmethod
    def _build_summary_meta_block(metadata: BilibiliVideoMetadata) -> str:
        meta_parts = [f"视频标题: {metadata.title}"]
        if metadata.author:
            meta_parts.append(f"UP主: {metadata.author}")
        if metadata.duration_sec:
            minutes = int(metadata.duration_sec) // 60
            seconds = int(metadata.duration_sec) % 60
            if minutes > 0:
                meta_parts.append(f"时长: {minutes}分{seconds}秒")
            else:
                meta_parts.append(f"时长: {seconds}秒")
        return "\n".join(meta_parts)

    @staticmethod
    def _build_description_block(description: str, limit: int = 500) -> str:
        if not description:
            return ""
        clipped = description[:limit] + ("..." if len(description) > limit else "")
        return f"\n\n视频简介:\n{clipped}"

    async def build_frame_descriptions(
        self,
        frame_paths: list[str],
        analyzer: Any,
        visual_method: str,
        enable_parallel: bool = False,
        parallel_limit: int = 1,
        prompt: str = "",
    ) -> list[str]:
        del visual_method
        if not frame_paths or analyzer is None:
            return []
        max_analyze_frames = min(len(frame_paths), 5)
        frame_descriptions: list[str] = []

        async def _do_call(idx: int, frame_path: str):
            try:
                desc = await analyzer.analyze_frame(frame_path, prompt)
                if desc and desc != "未识别":
                    return idx, str(desc).strip()
            except Exception:
                return None
            return None

        if enable_parallel:
            sem = asyncio.Semaphore(max(1, min(parallel_limit, max_analyze_frames)))

            async def _analyze_one(idx: int, frame_path: str):
                async with sem:
                    return await _do_call(idx, frame_path)

            tasks = [asyncio.create_task(_analyze_one(idx, p)) for idx, p in enumerate(frame_paths[:max_analyze_frames], start=1)]
            results = await asyncio.gather(*tasks)
            by_idx: dict[int, str] = {}
            for item in results:
                if not item:
                    continue
                i, d = item
                by_idx[i] = d
            for idx in range(1, max_analyze_frames + 1):
                if idx in by_idx:
                    frame_descriptions.append(f"帧{idx}: {by_idx[idx]}")
        else:
            for idx, frame_path in enumerate(frame_paths[:max_analyze_frames], start=1):
                item = await _do_call(idx, frame_path)
                if item:
                    i, d = item
                    frame_descriptions.append(f"帧{i}: {d}")
        return frame_descriptions

    async def _generate_with_host(
        self,
        *,
        prompt: str,
        summary_task_name: str,
        summary_temperature: float,
        summary_max_tokens: int,
        summary_max_chars: int,
    ) -> Optional[str]:
        result = await self._host_llm_adapter.generate_text(
            prompt=prompt,
            model=summary_task_name,
            temperature=summary_temperature,
            max_tokens=summary_max_tokens,
            configured_summary_max_chars=summary_max_chars,
        )
        if not isinstance(result, dict) or not result.get("success"):
            return None
        raw_summary = self.normalize_summary_text(str(result.get("response", "") or ""))
        if not raw_summary:
            return None
        if self.is_bad_summary(raw_summary):
            return None
        return raw_summary or None

    def _build_card_visual_block(self, card_visual_text: str) -> str:
        normalized = str(card_visual_text or "").strip()
        if not normalized:
            return ""
        return f"\n\n视频卡片预览图描述:\n{normalized}"

    def _build_summary_prompt(
        self,
        *,
        summary_mode: str,
        metadata: BilibiliVideoMetadata,
        description_block: str,
        card_visual_block: str,
        frames_block: str,
        text_block: str,
        summary_max_chars: int,
    ) -> str:
        if summary_mode == "text_only":
            return (
                f"根据以下B站视频信息，以客观第三方视角输出一段内容充分、信息密度高的视频内容总结。\n"
                f"长度目标：尽量接近{summary_max_chars}字；当信息充分时，优先覆盖到目标长度的70%-100%；当信息明显不足时，可以缩短，但必须明确写出'无法判断'，不要编造。\n"
                f"注意：由于视频时长较长，未进行视觉分析，请主要基于字幕/语音内容和视频简介进行总结。\n"
                f"要求：\n"
                f"1. 仅依据已给信息进行总结，信息不足请说明'无法判断'，不要编造未出现的内容\n"
                f"2. 只描述视频的客观内容，不要加入主观评价或感受\n"
                f"3. 不要使用'你'、'我'等人称代词\n"
                f"4. 不要说'这是一段XX制作的视频'，直接描述视频内容\n"
                f"5. 优先覆盖：主要讨论主题、具体提到的事件或观点、话题变化、无法判断项\n"
                f"6. 不要为了凑字数重复同一句话，不要使用空泛套话\n"
                f"7. 只输出总结内容本身，不要输出任何标题、说明、解释、格式标记\n\n"
                f"{self._build_summary_meta_block(metadata)}"
                f"{description_block}"
                f"{card_visual_block}"
                f"{text_block}"
            )

        return (
            f"根据以下B站视频信息，以客观第三方视角输出一段内容充分、信息密度高的视频内容总结。\n"
            f"长度目标：尽量接近{summary_max_chars}字；当信息充分时，优先覆盖到目标长度的70%-100%；当信息明显不足时，可以缩短，但必须明确写出'无法判断'，不要编造。\n"
            f"要求：\n"
            f"1. 仅依据已给信息进行总结，信息不足请说明'无法判断'，不要编造未出现的内容\n"
            f"2. 只描述视频的客观内容，不要加入主观评价或感受\n"
            f"3. 不要使用'你'、'我'等人称代词\n"
            f"4. 不要说'这是一段XX制作的视频'，直接描述视频内容\n"
            f"5. 优先覆盖：视频画面/界面元素、主要讨论主题、具体提到的事件或观点、话题变化、无法判断项\n"
            f"6. 不要为了凑字数重复同一句话，不要使用空泛套话\n"
            f"7. 只输出总结内容本身，不要输出任何标题、说明、解释、格式标记\n\n"
            f"{self._build_summary_meta_block(metadata)}"
            f"{description_block}"
            f"{card_visual_block}\n\n"
            f"关键帧描述:\n{frames_block}"
            f"{text_block}"
        )

    def _build_summary_prompt_snapshot(
        self,
        *,
        summary_mode: str,
        metadata: BilibiliVideoMetadata,
        description_block: str,
        card_visual_text: str,
        frames_block: str,
        text_content: str,
        summary_max_chars: int,
        custom_prompt: str,
    ) -> SummaryPromptSnapshot:
        meta_block = self._build_summary_meta_block(metadata)
        card_visual_block = self._build_card_visual_block(card_visual_text)
        text_block = f"\n\n视频字幕/语音内容:\n{text_content}" if text_content else ""

        if custom_prompt.strip():
            if summary_mode == "text_only":
                body = f"{meta_block}{description_block}\n\n注意：由于视频时长较长，未进行视觉分析，请主要基于字幕/语音内容和视频简介进行总结。{card_visual_block}{text_block}"
            else:
                body = f"{meta_block}{description_block}{card_visual_block}\n\n关键帧描述:\n{frames_block}{text_block}"
            prompt_final = self._format_custom_prompt(
                metadata=metadata,
                body=body,
                summary_max_chars=summary_max_chars,
                custom_prompt=custom_prompt,
            )
            return SummaryPromptSnapshot(
                mode=summary_mode,
                meta_block=meta_block,
                description_block=description_block,
                card_visual_block=card_visual_block,
                frames_block=frames_block,
                text_block=text_block,
                custom_prompt_raw=custom_prompt,
                custom_prompt_body=body,
                prompt_final=prompt_final,
            )

        prompt_final = self._build_summary_prompt(
            summary_mode=summary_mode,
            metadata=metadata,
            description_block=description_block,
            card_visual_block=card_visual_block,
            frames_block=frames_block,
            text_block=text_block,
            summary_max_chars=summary_max_chars,
        )
        return SummaryPromptSnapshot(
            mode=summary_mode,
            meta_block=meta_block,
            description_block=description_block,
            card_visual_block=card_visual_block,
            frames_block=frames_block,
            text_block=text_block,
            custom_prompt_raw=custom_prompt,
            custom_prompt_body="",
            prompt_final=prompt_final,
        )

    def _log_summary_prompt_snapshot(
        self,
        *,
        metadata: BilibiliVideoMetadata,
        snapshot: SummaryPromptSnapshot,
        summary_task_name: str,
        summary_temperature: float,
        summary_max_tokens: int,
        summary_max_chars: int,
        subtitle_text: str,
        asr_text: str,
    ) -> None:
        del subtitle_text
        del asr_text
        if not logger.isEnabledFor(logging.DEBUG):
            return
        logger.debug(
            "总结输入最终提示词[prompt_final]: video_id=%s, page=%s, mode=%s, task=%s, temperature=%s, configured_max_tokens=%s, configured_summary_max_chars=%s, effective_request_max_tokens=%s\n%s",
            metadata.video_id,
            metadata.page,
            snapshot.mode,
            summary_task_name,
            summary_temperature,
            summary_max_tokens,
            summary_max_chars,
            summary_max_tokens,
            snapshot.prompt_final,
        )

    async def generate_summary(
        self,
        *,
        metadata: BilibiliVideoMetadata,
        frame_paths: list[str],
        frame_descriptions: list[str] | None = None,
        card_visual_text: str = "",
        subtitle_text: str = "",
        asr_text: str = "",
        text_content: str = "",
        visual_method: str = "host",
        analyzer: Any = None,
        enable_parallel: bool = False,
        parallel_limit: int = 1,
        frame_prompt: str = "",
        summary_task_name: str = "replyer",
        summary_temperature: float = 0.4,
        summary_max_tokens: int = 1024,
        summary_max_chars: int = 200,
        custom_prompt: str = "",
    ) -> SummaryResult:
        result = SummaryResult()
        summary_max_chars = self._sanitize_summary_max_chars(summary_max_chars)
        try:
            summary: Optional[str] = None
            if visual_method == "host" and (frame_paths or frame_descriptions):
                summary, frame_descriptions = await self._analyze_video_with_description(
                    metadata=metadata,
                    frame_paths=frame_paths,
                    frame_descriptions=frame_descriptions or [],
                    card_visual_text=card_visual_text,
                    subtitle_text=subtitle_text,
                    asr_text=asr_text,
                    text_content=text_content,
                    analyzer=analyzer,
                    enable_parallel=enable_parallel,
                    parallel_limit=parallel_limit,
                    frame_prompt=frame_prompt,
                    summary_task_name=summary_task_name,
                    summary_temperature=summary_temperature,
                    summary_max_tokens=summary_max_tokens,
                    summary_max_chars=summary_max_chars,
                    custom_prompt=custom_prompt,
                )
                result.frame_descriptions = frame_descriptions
            else:
                summary = await self._generate_summary_text_only(
                    metadata=metadata,
                    card_visual_text=card_visual_text,
                    subtitle_text=subtitle_text,
                    asr_text=asr_text,
                    text_content=text_content,
                    summary_task_name=summary_task_name,
                    summary_temperature=summary_temperature,
                    summary_max_tokens=summary_max_tokens,
                    summary_max_chars=summary_max_chars,
                    custom_prompt=custom_prompt,
                )
            if summary:
                result.success = True
                result.raw_summary = summary
            else:
                result.error = "生成总结失败"
            return result
        except Exception as exc:
            result.error = str(exc)
            return result

    async def _generate_summary_text_only(
        self,
        *,
        metadata: BilibiliVideoMetadata,
        card_visual_text: str,
        subtitle_text: str,
        asr_text: str,
        text_content: str,
        summary_task_name: str,
        summary_temperature: float,
        summary_max_tokens: int,
        summary_max_chars: int,
        custom_prompt: str,
    ) -> Optional[str]:
        description_block = self._build_description_block(metadata.description)
        snapshot = self._build_summary_prompt_snapshot(
            summary_mode="text_only",
            metadata=metadata,
            description_block=description_block,
            card_visual_text=card_visual_text,
            frames_block="",
            text_content=text_content,
            summary_max_chars=summary_max_chars,
            custom_prompt=custom_prompt,
        )
        self._log_summary_prompt_snapshot(
            metadata=metadata,
            snapshot=snapshot,
            summary_task_name=summary_task_name,
            summary_temperature=summary_temperature,
            summary_max_tokens=summary_max_tokens,
            summary_max_chars=summary_max_chars,
            subtitle_text=subtitle_text,
            asr_text=asr_text,
        )
        return await self._generate_with_host(
            prompt=snapshot.prompt_final,
            summary_task_name=summary_task_name,
            summary_temperature=summary_temperature,
            summary_max_tokens=summary_max_tokens,
            summary_max_chars=summary_max_chars,
        )

    async def _analyze_video_with_description(
        self,
        *,
        metadata: BilibiliVideoMetadata,
        frame_paths: list[str],
        frame_descriptions: list[str],
        card_visual_text: str,
        subtitle_text: str,
        asr_text: str,
        text_content: str,
        analyzer: Any,
        enable_parallel: bool,
        parallel_limit: int,
        frame_prompt: str,
        summary_task_name: str,
        summary_temperature: float,
        summary_max_tokens: int,
        summary_max_chars: int,
        custom_prompt: str,
    ) -> tuple[Optional[str], list[str]]:
        if not frame_descriptions:
            frame_descriptions = await self.build_frame_descriptions(
                frame_paths,
                analyzer,
                "host",
                enable_parallel=enable_parallel,
                parallel_limit=parallel_limit,
                prompt=frame_prompt,
            )
        if not frame_descriptions:
            summary = await self._generate_summary_text_only(
                metadata=metadata,
                card_visual_text=card_visual_text,
                subtitle_text=subtitle_text,
                asr_text=asr_text,
                text_content=text_content,
                summary_task_name=summary_task_name,
                summary_temperature=summary_temperature,
                summary_max_tokens=summary_max_tokens,
                summary_max_chars=summary_max_chars,
                custom_prompt=custom_prompt,
            )
            return summary, []

        meta_block = self._build_summary_meta_block(metadata)
        if frame_descriptions:
            meta_block = f"{meta_block}\n分析帧数: {len(frame_descriptions)}"
        description_block = self._build_description_block(metadata.description)
        frames_block = "\n".join(frame_descriptions)
        snapshot = self._build_summary_prompt_snapshot(
            summary_mode="frames",
            metadata=metadata,
            description_block=description_block,
            card_visual_text=card_visual_text,
            frames_block=frames_block,
            text_content=text_content,
            summary_max_chars=summary_max_chars,
            custom_prompt=custom_prompt,
        )
        if custom_prompt.strip():
            body = f"{meta_block}{description_block}{snapshot.card_visual_block}\n\n关键帧描述:\n{frames_block}{snapshot.text_block}"
            prompt_final = self._format_custom_prompt(
                metadata=metadata,
                body=body,
                summary_max_chars=summary_max_chars,
                custom_prompt=custom_prompt,
            )
        else:
            prompt_final = (
                f"根据以下B站视频信息，以客观第三方视角输出一段内容充分、信息密度高的视频内容总结。\n"
                f"长度目标：尽量接近{summary_max_chars}字；当信息充分时，优先覆盖到目标长度的70%-100%；当信息明显不足时，可以缩短，但必须明确写出'无法判断'，不要编造。\n"
                f"要求：\n"
                f"1. 仅依据已给信息进行总结，信息不足请说明'无法判断'，不要编造未出现的内容\n"
                f"2. 只描述视频的客观内容，不要加入主观评价或感受\n"
                f"3. 不要使用'你'、'我'等人称代词\n"
                f"4. 不要说'这是一段XX制作的视频'，直接描述视频内容\n"
                f"5. 优先覆盖：视频画面/界面元素、主要讨论主题、具体提到的事件或观点、话题变化、无法判断项\n"
                f"6. 不要为了凑字数重复同一句话，不要使用空泛套话\n"
                f"7. 只输出总结内容本身，不要输出任何标题、说明、解释、格式标记\n\n"
                f"{meta_block}"
                f"{description_block}"
                f"{snapshot.card_visual_block}\n\n"
                f"关键帧描述:\n{frames_block}"
                f"{snapshot.text_block}"
            )
        snapshot.meta_block = meta_block
        snapshot.custom_prompt_body = body if custom_prompt.strip() else ""
        snapshot.prompt_final = prompt_final
        self._log_summary_prompt_snapshot(
            metadata=metadata,
            snapshot=snapshot,
            summary_task_name=summary_task_name,
            summary_temperature=summary_temperature,
            summary_max_tokens=summary_max_tokens,
            summary_max_chars=summary_max_chars,
            subtitle_text=subtitle_text,
            asr_text=asr_text,
        )
        summary = await self._generate_with_host(
            prompt=snapshot.prompt_final,
            summary_task_name=summary_task_name,
            summary_temperature=summary_temperature,
            summary_max_tokens=summary_max_tokens,
            summary_max_chars=summary_max_chars,
        )
        return summary, frame_descriptions

    def build_raw_info_text(self, metadata: BilibiliVideoMetadata, raw_info: dict[str, Any]) -> str:
        base_text = self._text_renderer.build_raw_info_text(
            metadata,
            subtitle_text=str(raw_info.get("subtitle_text", "") or ""),
            asr_text=str(raw_info.get("asr_text", "") or ""),
            frame_descriptions=list(raw_info.get("frame_descriptions", []) or []),
        )
        card_visual_text = str(raw_info.get("card_visual_text", "") or "").strip()
        if not card_visual_text:
            return base_text
        return f"{base_text}\n\n卡片预览图描述：\n{card_visual_text}".strip()

    def build_raw_detail_text(self, raw_info: dict[str, Any]) -> str:
        return self._text_renderer.build_raw_detail_text(
            subtitle_text=str(raw_info.get("subtitle_text", "") or ""),
            asr_text=str(raw_info.get("asr_text", "") or ""),
            frame_descriptions=list(raw_info.get("frame_descriptions", []) or []),
            card_visual_text=str(raw_info.get("card_visual_text", "") or ""),
        )

    def build_video_info_text(self, metadata: BilibiliVideoMetadata, summary: str) -> str:
        return self._text_renderer.build_video_info_text(metadata, summary)

    def build_basic_info_text(self, metadata: BilibiliVideoMetadata, *, include_status_note: bool = True) -> str:
        return self._text_renderer.build_basic_info_text(metadata, include_status_note=include_status_note)
