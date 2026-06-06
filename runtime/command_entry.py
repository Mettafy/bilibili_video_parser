"""/bili 命令参数解析。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CommandOptions:
    """命令附加选项。"""

    force_visual: bool = False
    force_asr: bool = False
    extra_arguments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CommandInvocation:
    """命令解析结果。"""

    url_or_id: str
    options: CommandOptions = field(default_factory=CommandOptions)


class CommandRequestParser:
    """解析 `/bili` 命令文本。"""

    def parse(
        self,
        raw_text: str,
        *,
        allow_extra_arguments: bool,
        allow_force_visual: bool,
        allow_force_asr: bool,
    ) -> CommandInvocation:
        stripped_text = str(raw_text or "").strip()
        parts = [part for part in stripped_text.split() if part]
        if len(parts) < 2:
            raise ValueError("命令缺少视频链接或视频 ID")

        url_or_id = parts[1].strip()
        option_tokens = parts[2:]
        options = CommandOptions()

        for token in option_tokens:
            normalized_token = token.strip().lower()
            if normalized_token in {"--force-visual", "-v"}:
                if not allow_force_visual:
                    raise ValueError("当前配置不允许强制视觉分析")
                options.force_visual = True
                continue
            if normalized_token in {"--force-asr", "-a"}:
                if not allow_force_asr:
                    raise ValueError("当前配置不允许强制 ASR")
                options.force_asr = True
                continue
            if allow_extra_arguments:
                options.extra_arguments.append(token)
                continue
            raise ValueError(f"不支持的命令参数：{token}")

        return CommandInvocation(url_or_id=url_or_id, options=options)
