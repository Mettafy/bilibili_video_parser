"""文本格式化服务。"""

from __future__ import annotations

import re

from .models import BilibiliVideoMetadata


def format_duration(seconds: int) -> str:
    if not isinstance(seconds, int):
        try:
            seconds = int(seconds)
        except Exception:
            return "0秒"
    if seconds < 60:
        return f"{seconds}秒"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分钟")
    if not parts and secs > 0:
        parts.append(f"{secs}秒")
    return "".join(parts) if parts else "0秒"


def build_basic_info_text(
    title: str,
    author: str,
    description: str,
    duration: int | None = None,
    page: int = 1,
    page_title: str = "",
    total_pages: int = 1,
    total_duration: int | None = None,
    include_status_note: bool = True,
) -> str:
    if total_pages > 1:
        if page_title:
            title_text = f"关于这个B站视频《{title}》P{page}「{page_title}」："
        else:
            title_text = f"关于这个B站视频《{title}》P{page}："
    else:
        title_text = f"关于这个B站视频《{title}》："
    parts = [title_text]
    if author:
        parts.append(f"UP主：{author}")
    if total_pages > 1:
        if duration:
            parts.append(f"当前分P时长：{format_duration(duration)}")
        if total_duration:
            parts.append(f"合集总时长：{format_duration(total_duration)}（共{total_pages}P）")
    else:
        if duration:
            parts.append(f"时长：{format_duration(duration)}")
    if description:
        max_desc_len = 400
        if len(description) > max_desc_len:
            description = description[:max_desc_len] + "..."
        parts.append(f"简介：{description}")
    if include_status_note:
        parts.append("（视频内容暂时无法解析，以上为基础信息）")
    return "\n".join(parts)


def simplify_bilibili_links(text: str, video_id: str) -> str:
    bilibili_url_pattern = r'https?://(?:www\.|m\.)?bilibili\.com/video/(?:BV[a-zA-Z0-9]{10}|av\d+)[^\s]*'
    text = re.sub(bilibili_url_pattern, video_id, text)
    short_url_pattern = r'https?://b23\.tv/([a-zA-Z0-9]+)[^\s]*'
    text = re.sub(short_url_pattern, r'b23.tv/\1', text)
    text = re.sub(r'\[picid:[^\]]+\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[图片[：:][^\]]*\]', '', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class TextRenderService:
    def build_basic_info_text(self, metadata: BilibiliVideoMetadata, *, include_status_note: bool = True) -> str:
        return build_basic_info_text(
            title=metadata.title,
            author=metadata.author,
            description=metadata.description,
            duration=metadata.duration_sec,
            page=metadata.page,
            page_title=metadata.page_title,
            total_pages=metadata.total_pages,
            total_duration=metadata.total_duration_sec,
            include_status_note=include_status_note,
        )

    def build_video_info_text(self, metadata: BilibiliVideoMetadata, summary: str) -> str:
        if metadata.total_pages > 1:
            if metadata.page_title:
                title_text = f"关于这个B站视频《{metadata.title}》P{metadata.page}「{metadata.page_title}」："
            else:
                title_text = f"关于这个B站视频《{metadata.title}》P{metadata.page}："
        else:
            title_text = f"关于这个B站视频《{metadata.title}》："
        parts = [title_text]
        if metadata.author:
            parts.append(f"UP主：{metadata.author}")
        if metadata.total_pages > 1:
            if metadata.duration_sec:
                parts.append(f"当前分P时长：{format_duration(metadata.duration_sec)}")
            if metadata.total_duration_sec:
                parts.append(f"合集总时长：{format_duration(metadata.total_duration_sec)}（共{metadata.total_pages}P）")
        else:
            if metadata.duration_sec:
                parts.append(f"时长：{format_duration(metadata.duration_sec)}")
        if metadata.description:
            description = metadata.description
            if len(description) > 200:
                description = description[:200] + "..."
            parts.append(f"简介：{description}")
        parts.append(f"内容总结：{summary}")
        return "\n".join(parts)

    def build_raw_info_text(self, metadata: BilibiliVideoMetadata, *, subtitle_text: str = "", asr_text: str = "", frame_descriptions: list[str] | None = None) -> str:
        frame_descriptions = frame_descriptions or []
        parts: list[str] = []
        parts.append(f"视频标题: {metadata.title or '未知标题'}")
        if metadata.author:
            parts.append(f"UP主: {metadata.author}")
        if metadata.duration_sec:
            parts.append(f"时长: {format_duration(metadata.duration_sec)}")
        if frame_descriptions:
            parts.append(f"分析帧数: {len(frame_descriptions)}")
        parts.append("")
        parts.append("视频简介:")
        parts.append(metadata.description if metadata.description else "无法判断")
        parts.append("")
        parts.append("关键帧描述:")
        if frame_descriptions:
            parts.extend(frame_descriptions)
        else:
            parts.append("无法判断")
        parts.append("")
        parts.append("视频字幕/语音内容:")
        content_parts: list[str] = []
        if subtitle_text:
            content_parts.append(f"【字幕内容】\n{subtitle_text}")
        if asr_text:
            content_parts.append(f"【语音识别内容】\n{asr_text}")
        parts.append("\n\n".join(content_parts) if content_parts else "无法判断")
        return "\n".join(parts).strip()

    def build_raw_detail_text(self, *, subtitle_text: str = "", asr_text: str = "", frame_descriptions: list[str] | None = None, card_visual_text: str = "") -> str:
        frame_descriptions = frame_descriptions or []
        parts: list[str] = []
        normalized_card_visual_text = str(card_visual_text or "").strip()
        if normalized_card_visual_text:
            parts.append("卡片预览图描述:")
            parts.append(normalized_card_visual_text)
            parts.append("")
        parts.append("关键帧描述:")
        if frame_descriptions:
            parts.extend(frame_descriptions)
        else:
            parts.append("无法判断")
        parts.append("")
        parts.append("视频字幕/语音内容:")
        content_parts: list[str] = []
        if subtitle_text:
            content_parts.append(f"【字幕内容】\n{subtitle_text}")
        if asr_text:
            content_parts.append(f"【语音识别内容】\n{asr_text}")
        parts.append("\n\n".join(content_parts) if content_parts else "无法判断")
        return "\n".join(parts).strip()

    def simplify_bilibili_links(self, text: str, video_id: str) -> str:
        return simplify_bilibili_links(text, video_id)
