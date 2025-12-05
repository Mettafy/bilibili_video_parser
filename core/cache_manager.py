# -*- coding: utf-8 -*-
"""
视频缓存管理模块

本模块提供视频解析结果的缓存管理功能，避免重复解析同一视频。

缓存结构：
data/
├── index.json          # 缓存索引文件
└── cache/              # 缓存数据目录
    └── {hash}.json     # 每个视频的缓存数据

缓存数据格式：
{
    "video_id": "BV1xx411c7mD",
    "page": 1,                    # 分P号
    "page_title": "第一集",        # 分P标题
    "total_pages": 10,            # 总分P数
    "title": "视频标题",
    "author": "UP主名称",
    "description": "视频简介",
    "duration": 300,              # 当前分P时长（秒）
    "total_duration": 3000,       # 合集总时长（秒）
    "raw_info": {                 # 原生视频信息
        "subtitle_text": "...",
        "asr_text": "...",
        "frame_descriptions": [...],
        "visual_analysis": "...",
        "visual_method": "default"
    },
    "summary": "视频总结",          # 可能为null
    "has_subtitle": true,
    "has_asr": false
}

主要类：
- CacheManager: 缓存管理器

特性：
- 原子写入：使用临时文件+os.replace()确保写入安全
- 并发安全：多个进程/协程同时写入不会损坏文件
- 自动清理：索引与缓存文件不一致时自动修复

缓存Key规则：
- 单P视频：video_id（如 "BV1xx411c7mD"）
- 多P视频：video_id_p{page}（如 "BV1xx411c7mD_p2"）

使用示例：
    manager = CacheManager("/path/to/data")
    
    # 获取缓存
    cached = manager.get_cache("BV1xx411c7mD")
    
    # 保存缓存
    manager.save_cache("BV1xx411c7mD", data)
    
    # 清除缓存
    manager.clear_cache("BV1xx411c7mD")  # 清除单个
    manager.clear_cache()  # 清除所有

Author: 约瑟夫.k && 白泽
"""
import os
import json
import hashlib
import uuid
from typing import Optional, Dict, Any
from pathlib import Path
from src.plugin_system import get_logger

logger = get_logger("bilibili_cache_manager")


class CacheManager:
    """视频缓存管理器"""

    def __init__(self, data_dir: str):
        """初始化缓存管理器
        
        Args:
            data_dir: 数据目录路径
        """
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "cache"  # 缓存目录（存储视频解析结果）
        self.index_file = self.data_dir / "index.json"
        
        # 确保目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 加载或初始化索引
        self.index = self._load_index()

    def _load_index(self) -> Dict[str, Any]:
        """加载缓存索引"""
        if self.index_file.exists():
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[CacheManager] 加载索引失败: {e}")
                return {}
        return {}

    def _save_index(self):
        """保存缓存索引（原子写入）
        
        使用临时文件 + os.replace() 实现原子写入：
        - 先写入临时文件
        - 然后原子重命名为目标文件
        - 即使多个进程同时写入，文件也不会损坏
        """
        temp_file = None
        try:
            # 生成唯一的临时文件名
            temp_file = self.index_file.parent / f"{self.index_file.name}.tmp.{uuid.uuid4().hex[:8]}"
            
            # 写入临时文件
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.index, f, ensure_ascii=False, indent=2)
            
            # 原子重命名（在同一文件系统上是原子操作）
            os.replace(str(temp_file), str(self.index_file))
            
        except Exception as e:
            logger.error(f"[CacheManager] 保存索引失败: {e}")
        finally:
            # 清理可能残留的临时文件
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    def _calculate_video_hash(self, video_id: str) -> str:
        """计算视频ID的hash值
        
        Args:
            video_id: 视频ID (BV号或AV号)
            
        Returns:
            hash值
        """
        return hashlib.md5(video_id.encode()).hexdigest()

    def get_cache(self, video_id: str) -> Optional[Dict[str, Any]]:
        """获取视频缓存
        
        Args:
            video_id: 视频ID
            
        Returns:
            缓存数据，不存在返回None
        """
        video_hash = self._calculate_video_hash(video_id)
        logger.debug(f"[CacheManager] 查询缓存: video_id={video_id}, hash={video_hash}")
        
        # 检查索引
        if video_hash not in self.index:
            logger.debug(f"[CacheManager] 缓存未命中: {video_id}")
            return None
        
        # 读取缓存文件
        cache_file = self.cache_dir / f"{video_hash}.json"
        if not cache_file.exists():
            logger.warning(f"[CacheManager] 索引存在但缓存文件不存在: {video_id}")
            del self.index[video_hash]
            self._save_index()
            return None
        
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            logger.debug(f"[CacheManager] 缓存命中: {video_id}")
            return cache_data
        except Exception as e:
            logger.error(f"[CacheManager] 读取缓存失败: {e}")
            return None

    def save_cache(self, video_id: str, data: Dict[str, Any]) -> bool:
        """保存视频缓存（原子写入）
        
        使用临时文件 + os.replace() 实现原子写入：
        - 先写入临时文件
        - 然后原子重命名为目标文件
        - 即使多个进程/协程同时写入同一视频的缓存，文件也不会损坏
        - 最后完成的写入会覆盖之前的，但所有写入的内容都是有效的
        
        Args:
            video_id: 视频ID
            data: 要缓存的数据
            
        Returns:
            是否保存成功
        """
        video_hash = self._calculate_video_hash(video_id)
        cache_file = self.cache_dir / f"{video_hash}.json"
        temp_file = None
        logger.debug(f"[CacheManager] 保存缓存: video_id={video_id}, hash={video_hash}")
        
        try:
            # 生成唯一的临时文件名（避免多个写入操作使用同一临时文件）
            temp_file = self.cache_dir / f"{video_hash}.json.tmp.{uuid.uuid4().hex[:8]}"
            
            # 写入临时文件
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 原子重命名（在同一文件系统上是原子操作）
            os.replace(str(temp_file), str(cache_file))
            
            # 更新索引
            self.index[video_hash] = {
                "video_id": video_id,
                "file": f"{video_hash}.json"
            }
            self._save_index()
            logger.debug(f"[CacheManager] 缓存保存成功: {video_id}")
            return True
        except Exception as e:
            logger.error(f"[CacheManager] 保存缓存失败: {e}")
            return False
        finally:
            # 清理可能残留的临时文件
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    def clear_cache(self, video_id: Optional[str] = None) -> bool:
        """清除缓存
        
        Args:
            video_id: 视频ID，为None则清除所有缓存
            
        Returns:
            是否清除成功
        """
        try:
            if video_id:
                # 清除单个视频缓存
                video_hash = self._calculate_video_hash(video_id)
                cache_file = self.cache_dir / f"{video_hash}.json"
                
                if cache_file.exists():
                    cache_file.unlink()
                
                if video_hash in self.index:
                    del self.index[video_hash]
                    self._save_index()
            else:
                # 清除所有缓存
                for cache_file in self.cache_dir.glob("*.json"):
                    cache_file.unlink()
                
                self.index = {}
                self._save_index()
            
            return True
        except Exception as e:
            logger.error(f"[CacheManager] 清除缓存失败: {e}")
            return False