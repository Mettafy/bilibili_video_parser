"""消息格式化工具。"""

from __future__ import annotations

class MessageFormatter:
    """统一生成自动检测注入文本与命令回复文本。"""

    def build_auto_detect_block(
        self,
        *,
        original_text: str,
        injected_text: str,
    ) -> str:
        normalized_original = original_text.strip()
        normalized_injected = injected_text.strip()
        if not normalized_injected:
            return normalized_original
        if normalized_original:
            return f"{normalized_original}\n\n{normalized_injected}"
        return normalized_injected

    def build_processing_text(self) -> str:
        """构造命令模式处理中提示。"""
        return "正在解析 B 站视频，请稍候。"

    def build_auto_detect_fallback_text(self, video_id: str, page: int, title: str = "", author: str = "") -> str:
        """构造自动检测链在完整分析失败时的最小可用文本。"""

        normalized_title = title.strip()
        normalized_author = author.strip()
        video_label = f"{video_id} P{page}" if page > 1 else video_id
        if normalized_title and normalized_author:
            return f"检测到 B 站视频 {video_label}。标题：{normalized_title}。作者：{normalized_author}。"
        if normalized_title:
            return f"检测到 B 站视频 {video_label}。标题：{normalized_title}。"
        return f"检测到 B 站视频 {video_label}。"

    def build_auto_detect_final_text(self, *, simplified_text: str, resolved_text: str) -> str:
        """构造自动检测链最终交给 Maisaka 的增强文本。"""

        normalized_resolved_text = resolved_text.strip()
        if not normalized_resolved_text:
            return simplified_text.strip()
        normalized_simplified_text = simplified_text.strip()
        if normalized_simplified_text:
            return f"{normalized_simplified_text}\n\n{normalized_resolved_text}"
        return normalized_resolved_text

    def build_command_error_text(self, message: str) -> str:
        """构造命令模式错误提示。"""
        normalized_message = str(message or "").strip() or "视频解析失败"
        return f"B站视频解析失败：{normalized_message}"
