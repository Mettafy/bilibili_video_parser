# -*- coding: utf-8 -*-
"""安全删除临时文件模块

此模块提供安全的临时文件删除功能，通过多重验证确保不会误删用户文件。
所有删除操作都必须通过严格的安全检查才能执行。

安全检查包括：
1. 路径非空检查
2. 文件/目录存在性检查
3. 文件/目录类型检查
4. 文件名前缀检查（必须以bili_开头）
5. 路径位置检查（必须在插件的data/temp/目录中）
6. 目录内容检查（仅允许图片文件）

定时清理功能：
- 支持配置临时文件最大保留时间（分钟）
- 设为0表示处理完成后立即删除
- 设为>0表示定时清理超过指定时间的文件

Author: 约瑟夫.k && 白泽
"""
import os
import shutil
import time
from pathlib import Path
from typing import Tuple, Optional, Dict
from src.plugin_system import get_logger

logger = get_logger("safe_delete")

# 允许的临时文件前缀
ALLOWED_FILE_PREFIXES = ("bili_video_", "bili_audio_")
# 允许的临时目录前缀
ALLOWED_DIR_PREFIXES = ("bili_frames_",)
# 允许的图片扩展名
ALLOWED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# 插件临时目录路径（在模块加载时初始化）
_plugin_temp_dir: Optional[str] = None


def init_temp_dir(data_dir: str) -> str:
    """初始化插件临时目录
    
    Args:
        data_dir: 插件数据目录路径
        
    Returns:
        临时目录的绝对路径
    """
    global _plugin_temp_dir
    temp_dir = Path(data_dir) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    _plugin_temp_dir = os.path.normcase(os.path.normpath(os.path.abspath(str(temp_dir))))
    return _plugin_temp_dir


def get_temp_dir() -> Optional[str]:
    """获取插件临时目录路径
    
    Returns:
        临时目录的绝对路径，如果未初始化返回None
    """
    return _plugin_temp_dir


def get_temp_subdir(subdir: str) -> str:
    """获取临时目录下的子目录路径
    
    Args:
        subdir: 子目录名称（如 "videos", "frames", "audio"）
        
    Returns:
        子目录的绝对路径
        
    Raises:
        RuntimeError: 如果临时目录未初始化
    """
    if not _plugin_temp_dir:
        raise RuntimeError("临时目录未初始化，请先调用 init_temp_dir()")
    
    subdir_path = Path(_plugin_temp_dir) / subdir
    subdir_path.mkdir(parents=True, exist_ok=True)
    return str(subdir_path)


def _is_path_in_plugin_temp_dir(path: str) -> bool:
    """检查路径是否在插件临时目录中
    
    Args:
        path: 要检查的路径
        
    Returns:
        是否在插件临时目录中
    """
    if not _plugin_temp_dir:
        logger.error("[SafeDelete] 临时目录未初始化")
        return False
    
    abs_path = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    
    # 确保路径以插件临时目录开头（使用os.path.commonpath更安全）
    try:
        common = os.path.commonpath([_plugin_temp_dir, abs_path])
        return os.path.normcase(os.path.normpath(common)) == _plugin_temp_dir
    except ValueError:
        # 在Windows上，如果路径在不同驱动器上会抛出ValueError
        return False


def safe_delete_temp_file(file_path: str) -> Tuple[bool, str]:
    """安全删除临时文件（5重验证）
    
    验证步骤：
    1. 路径非空检查
    2. 文件存在性检查
    3. 文件类型检查（必须是文件，不是目录）
    4. 文件名前缀检查（必须以bili_video_或bili_audio_开头）
    5. 路径位置检查（必须在插件的data/temp/目录中）
    
    Args:
        file_path: 要删除的文件路径
        
    Returns:
        (是否成功, 原因说明)
    """
    # 检查1：路径不能为空
    if not file_path:
        return False, "路径为空"
    
    # 检查2：文件必须存在
    if not os.path.exists(file_path):
        return False, "文件不存在"
    
    # 检查3：必须是文件（不是目录）
    if not os.path.isfile(file_path):
        return False, "路径不是文件"
    
    # 检查4：文件名必须以允许的前缀开头
    basename = os.path.basename(file_path)
    if not any(basename.startswith(prefix) for prefix in ALLOWED_FILE_PREFIXES):
        logger.error(f"[SafeDelete] 安全检查失败：文件名前缀不在允许列表中: {basename}")
        return False, f"文件名前缀不在允许列表中: {basename}"
    
    # 检查5：文件必须在插件临时目录中
    if not _is_path_in_plugin_temp_dir(file_path):
        logger.error(f"[SafeDelete] 安全检查失败：文件不在插件临时目录中: {file_path}")
        return False, f"文件不在插件临时目录中"
    
    # 所有检查通过，安全删除
    try:
        os.remove(file_path)
        return True, "删除成功"
    except PermissionError:
        logger.warning(f"[SafeDelete] 删除文件权限不足: {file_path}")
        return False, "权限不足"
    except Exception as e:
        logger.warning(f"[SafeDelete] 删除文件失败: {e}")
        return False, str(e)


def safe_delete_temp_dir(dir_path: str) -> Tuple[bool, str]:
    """安全删除临时目录（6重验证）
    
    验证步骤：
    1. 路径非空检查
    2. 目录存在性检查
    3. 目录类型检查（必须是目录，不是文件）
    4. 目录名前缀检查（必须以bili_frames_开头）
    5. 路径位置检查（必须在插件的data/temp/目录中）
    6. 目录内容检查（只能包含图片文件，不能有子目录）
    
    Args:
        dir_path: 要删除的目录路径
        
    Returns:
        (是否成功, 原因说明)
    """
    # 检查1：路径不能为空
    if not dir_path:
        return False, "路径为空"
    
    # 检查2：目录必须存在
    if not os.path.exists(dir_path):
        return False, "目录不存在"
    
    # 检查3：必须是目录
    if not os.path.isdir(dir_path):
        return False, "路径不是目录"
    
    # 检查4：目录名必须以允许的前缀开头
    basename = os.path.basename(dir_path)
    if not any(basename.startswith(prefix) for prefix in ALLOWED_DIR_PREFIXES):
        logger.error(f"[SafeDelete] 安全检查失败：目录名前缀不在允许列表中: {basename}")
        return False, f"目录名前缀不在允许列表中: {basename}"
    
    # 检查5：目录必须在插件临时目录中
    if not _is_path_in_plugin_temp_dir(dir_path):
        logger.error(f"[SafeDelete] 安全检查失败：目录不在插件临时目录中: {dir_path}")
        return False, f"目录不在插件临时目录中"
    
    # 检查6：目录中只能包含图片文件（不能有子目录或其他文件）
    try:
        for item in os.listdir(dir_path):
            item_path = os.path.join(dir_path, item)
            
            # 不允许子目录
            if os.path.isdir(item_path):
                logger.error(f"[SafeDelete] 安全检查失败：目录中包含子目录: {item}")
                return False, f"目录中包含子目录: {item}"
            
            # 只允许图片文件
            if not item.lower().endswith(ALLOWED_IMAGE_EXTENSIONS):
                logger.error(f"[SafeDelete] 安全检查失败：目录中包含非图片文件: {item}")
                return False, f"目录中包含非图片文件: {item}"
    except PermissionError:
        logger.warning(f"[SafeDelete] 读取目录权限不足: {dir_path}")
        return False, "读取目录权限不足"
    except Exception as e:
        logger.warning(f"[SafeDelete] 检查目录内容失败: {e}")
        return False, str(e)
    
    # 所有检查通过，安全删除
    try:
        shutil.rmtree(dir_path)
        return True, "删除成功"
    except PermissionError:
        logger.warning(f"[SafeDelete] 删除目录权限不足: {dir_path}")
        return False, "权限不足"
    except Exception as e:
        logger.warning(f"[SafeDelete] 删除目录失败: {e}")
        return False, str(e)


def cleanup_temp_files(video_path: str = None, frames_dir: str = None, audio_path: str = None) -> dict:
    """清理临时文件的便捷函数
    
    Args:
        video_path: 临时视频文件路径
        frames_dir: 临时帧目录路径
        audio_path: 临时音频文件路径
        
    Returns:
        清理结果字典，包含每个文件的清理状态
    """
    results = {}
    
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


def cleanup_old_temp_files(max_age_min: float) -> Dict[str, int]:
    """清理超过指定时间的临时文件
    
    遍历临时目录下的所有子目录（videos, frames, audio），
    删除修改时间超过 max_age_min 分钟的文件和目录。
    
    Args:
        max_age_min: 文件最大保留时间（分钟）
        
    Returns:
        清理统计字典：{"files_deleted": 数量, "dirs_deleted": 数量, "errors": 数量}
    """
    if not _plugin_temp_dir:
        logger.warning("[SafeDelete] 临时目录未初始化，无法执行定时清理")
        return {"files_deleted": 0, "dirs_deleted": 0, "errors": 0}
    
    if max_age_min <= 0:
        logger.debug("[SafeDelete] max_age_min <= 0，跳过定时清理")
        return {"files_deleted": 0, "dirs_deleted": 0, "errors": 0}
    
    stats = {"files_deleted": 0, "dirs_deleted": 0, "errors": 0}
    current_time = time.time()
    max_age_sec = max_age_min * 60
    
    # 遍历临时目录下的子目录
    temp_path = Path(_plugin_temp_dir)
    for subdir_name in ["videos", "frames", "audio"]:
        subdir_path = temp_path / subdir_name
        if not subdir_path.exists():
            continue
        
        if subdir_name == "frames":
            # frames目录下是帧目录（bili_frames_xxx），需要检查目录
            for item in subdir_path.iterdir():
                if item.is_dir():
                    try:
                        # 获取目录的修改时间
                        mtime = item.stat().st_mtime
                        age_sec = current_time - mtime
                        
                        if age_sec > max_age_sec:
                            # 使用安全删除
                            success, reason = safe_delete_temp_dir(str(item))
                            if success:
                                stats["dirs_deleted"] += 1
                            else:
                                stats["errors"] += 1
                                logger.warning(f"[SafeDelete] 清理目录失败: {item.name}, 原因: {reason}")
                    except Exception as e:
                        stats["errors"] += 1
                        logger.warning(f"[SafeDelete] 检查目录时出错: {item.name}, 错误: {e}")
        else:
            # videos和audio目录下是文件
            for item in subdir_path.iterdir():
                if item.is_file():
                    try:
                        # 获取文件的修改时间
                        mtime = item.stat().st_mtime
                        age_sec = current_time - mtime
                        
                        if age_sec > max_age_sec:
                            # 使用安全删除
                            success, reason = safe_delete_temp_file(str(item))
                            if success:
                                stats["files_deleted"] += 1
                            else:
                                stats["errors"] += 1
                                logger.warning(f"[SafeDelete] 清理文件失败: {item.name}, 原因: {reason}")
                    except Exception as e:
                        stats["errors"] += 1
                        logger.warning(f"[SafeDelete] 检查文件时出错: {item.name}, 错误: {e}")
    
    total_deleted = stats["files_deleted"] + stats["dirs_deleted"]
    if total_deleted > 0:
        logger.info(f"[BilibiliVideoParser] 临时文件清理完成: 删除{stats['files_deleted']}个文件, {stats['dirs_deleted']}个目录")
    
    return stats