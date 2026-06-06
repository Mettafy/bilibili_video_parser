"""安全删除与临时文件清理。"""

from __future__ import annotations

import os
import shutil
import time

from pathlib import Path
from typing import Optional


ALLOWED_FILE_PREFIXES = ("bili_video_", "bili_audio_")
ALLOWED_DIR_PREFIXES = ("bili_frames_",)
ALLOWED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

_plugin_temp_dir: Optional[str] = None


def init_temp_dir(data_dir: str) -> str:
    global _plugin_temp_dir
    temp_dir = Path(data_dir) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    _plugin_temp_dir = os.path.normcase(os.path.normpath(os.path.abspath(str(temp_dir))))
    return _plugin_temp_dir


def get_temp_dir() -> Optional[str]:
    return _plugin_temp_dir


def get_temp_subdir(subdir: str) -> str:
    if not _plugin_temp_dir:
        raise RuntimeError("临时目录未初始化，请先调用 init_temp_dir()")
    subdir_path = Path(_plugin_temp_dir) / subdir
    subdir_path.mkdir(parents=True, exist_ok=True)
    return str(subdir_path)


def _is_path_in_plugin_temp_dir(path: str) -> bool:
    if not _plugin_temp_dir:
        return False
    abs_path = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    try:
        common = os.path.commonpath([_plugin_temp_dir, abs_path])
        return os.path.normcase(os.path.normpath(common)) == _plugin_temp_dir
    except ValueError:
        return False


def safe_delete_temp_file(file_path: str) -> tuple[bool, str]:
    if not file_path:
        return False, "路径为空"
    if not os.path.exists(file_path):
        return False, "文件不存在"
    if not os.path.isfile(file_path):
        return False, "路径不是文件"
    basename = os.path.basename(file_path)
    if not any(basename.startswith(prefix) for prefix in ALLOWED_FILE_PREFIXES):
        return False, f"文件名前缀不在允许列表中: {basename}"
    if not _is_path_in_plugin_temp_dir(file_path):
        return False, "文件不在插件临时目录中"
    try:
        os.remove(file_path)
        return True, "删除成功"
    except PermissionError:
        return False, "权限不足"
    except Exception as e:
        return False, str(e)


def safe_delete_temp_dir(dir_path: str) -> tuple[bool, str]:
    if not dir_path:
        return False, "路径为空"
    if not os.path.exists(dir_path):
        return False, "目录不存在"
    if not os.path.isdir(dir_path):
        return False, "路径不是目录"
    basename = os.path.basename(dir_path)
    if not any(basename.startswith(prefix) for prefix in ALLOWED_DIR_PREFIXES):
        return False, f"目录名前缀不在允许列表中: {basename}"
    if not _is_path_in_plugin_temp_dir(dir_path):
        return False, "目录不在插件临时目录中"
    try:
        for item in os.listdir(dir_path):
            item_path = os.path.join(dir_path, item)
            if os.path.isdir(item_path):
                return False, f"目录中包含子目录: {item}"
            if not item.lower().endswith(ALLOWED_IMAGE_EXTENSIONS):
                return False, f"目录中包含非图片文件: {item}"
    except PermissionError:
        return False, "读取目录权限不足"
    except Exception as e:
        return False, str(e)
    try:
        shutil.rmtree(dir_path)
        return True, "删除成功"
    except PermissionError:
        return False, "权限不足"
    except Exception as e:
        return False, str(e)


def cleanup_temp_files(video_path: str = None, frames_dir: str = None, audio_path: str = None) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    if video_path:
        success, reason = safe_delete_temp_file(video_path)
        results["video"] = {"success": success, "reason": reason, "path": video_path}
    if frames_dir:
        success, reason = safe_delete_temp_dir(frames_dir)
        results["frames"] = {"success": success, "reason": reason, "path": frames_dir}
    if audio_path:
        success, reason = safe_delete_temp_file(audio_path)
        results["audio"] = {"success": success, "reason": reason, "path": audio_path}
    return results


def cleanup_old_temp_files(max_age_min: float) -> dict[str, int]:
    if not _plugin_temp_dir or max_age_min <= 0:
        return {"files_deleted": 0, "dirs_deleted": 0, "errors": 0}
    stats = {"files_deleted": 0, "dirs_deleted": 0, "errors": 0}
    current_time = time.time()
    max_age_sec = max_age_min * 60
    temp_path = Path(_plugin_temp_dir)
    for subdir_name in ["videos", "frames", "audio"]:
        subdir_path = temp_path / subdir_name
        if not subdir_path.exists():
            continue
        if subdir_name == "frames":
            for item in subdir_path.iterdir():
                if item.is_dir():
                    try:
                        if current_time - item.stat().st_mtime >= max_age_sec:
                            success, _ = safe_delete_temp_dir(str(item))
                            if success:
                                stats["dirs_deleted"] += 1
                            else:
                                stats["errors"] += 1
                    except Exception:
                        stats["errors"] += 1
        else:
            for item in subdir_path.iterdir():
                if item.is_file():
                    try:
                        if current_time - item.stat().st_mtime >= max_age_sec:
                            success, _ = safe_delete_temp_file(str(item))
                            if success:
                                stats["files_deleted"] += 1
                            else:
                                stats["errors"] += 1
                    except Exception:
                        stats["errors"] += 1
    return stats
