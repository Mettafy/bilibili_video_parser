# -*- coding: utf-8 -*-
"""
文本格式化服务模块

提供跨处理器复用的轻量文本构建能力，避免重复实现导致行为分叉。

功能：
1. 时长格式化
2. Level 3 基础信息文本构建
3. B站链接与图片标记清洗

设计原则：
- 只做纯文本转换，不依赖外部状态
- 保持与原有输出语义一致
- 不改变业务流程与安全策略
"""

import re


def format_duration(seconds: int) -> str:
    """格式化时长为用户友好的字符串。"""
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

    parts = []
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分钟")
    # 只有在没有小时和分钟时才显示秒
    if not parts and secs > 0:
        parts.append(f"{secs}秒")

    return "".join(parts) if parts else "0秒"


def build_basic_info_text(
    title: str,
    author: str,
    description: str,
    duration: int = None,
    page: int = 1,
    page_title: str = "",
    total_pages: int = 1,
    total_duration: int = None,
) -> str:
    """构建基础信息文本（Level 3 降级模式使用）。"""
    # 构建标题（包含分P信息）
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

    # 时长显示逻辑
    if total_pages > 1:
        # 多P视频：显示当前分P时长和合集总时长
        if duration:
            parts.append(f"当前分P时长：{format_duration(duration)}")
        if total_duration:
            parts.append(f"合集总时长：{format_duration(total_duration)}（共{total_pages}P）")
    else:
        # 单P视频：只显示时长
        if duration:
            parts.append(f"时长：{format_duration(duration)}")

    if description:
        # Level 3 可以显示更长的简介，因为没有总结
        max_desc_len = 400
        if len(description) > max_desc_len:
            description = description[:max_desc_len] + "..."
        parts.append(f"简介：{description}")

    # 添加降级说明
    parts.append("（视频内容暂时无法解析，以上为基础信息）")

    return "\n".join(parts)


def simplify_bilibili_links(text: str, video_id: str) -> str:
    """简化消息中的B站链接并清理图片标记，减少消息长度。"""
    # 替换完整B站链接（包含各种参数）为视频ID
    # 匹配: https://www.bilibili.com/video/BVxxx?各种参数
    # 匹配: https://m.bilibili.com/video/BVxxx?各种参数
    bilibili_url_pattern = r'https?://(?:www\.|m\.)?bilibili\.com/video/(?:BV[a-zA-Z0-9]{10}|av\d+)[^\s]*'
    text = re.sub(bilibili_url_pattern, video_id, text)

    # 替换b23.tv短链接（包含各种参数）为简化形式
    # 匹配: https://b23.tv/xxx?各种参数
    short_url_pattern = r'https?://b23\.tv/([a-zA-Z0-9]+)[^\s]*'
    text = re.sub(short_url_pattern, r'b23.tv/\1', text)

    # 清理图片标记，避免主回复系统将当前消息识别为“发送了图片”
    # 仅清理内部图片标记，不影响普通文本
    text = re.sub(r'\[picid:[^\]]+\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[图片[：:][^\]]*\]', '', text)

    # 清理清洗后可能残留的多余空白
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()
