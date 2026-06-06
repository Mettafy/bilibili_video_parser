"""视频缓存管理模块。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

from pathlib import Path
from typing import Any, Optional


class CacheManager:
    """视频缓存管理器。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "cache"
        self.index_file = self.data_dir / "index.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self.index_file.exists():
            try:
                return json.loads(self.index_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_index(self) -> None:
        temp_file: Path | None = None
        try:
            temp_file = self.index_file.parent / f"{self.index_file.name}.tmp.{uuid.uuid4().hex[:8]}"
            temp_file.write_text(json.dumps(self.index, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(temp_file), str(self.index_file))
        finally:
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    @staticmethod
    def _calculate_video_hash(video_id: str) -> str:
        return hashlib.md5(video_id.encode("utf-8")).hexdigest()

    def build_cache_key(self, video_id: str, page: int, config_fingerprint: str = "") -> str:
        base_key = f"{video_id}_p{page}" if page > 1 else video_id
        normalized_fingerprint = str(config_fingerprint or "").strip()
        if not normalized_fingerprint:
            return base_key
        return f"{base_key}__cfg_{normalized_fingerprint}"

    def get_cache(self, video_id: str) -> Optional[dict[str, Any]]:
        video_hash = self._calculate_video_hash(video_id)
        if video_hash not in self.index:
            return None
        cache_file = self.cache_dir / f"{video_hash}.json"
        if not cache_file.exists():
            self.index.pop(video_hash, None)
            self._save_index()
            return None
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    def load(self, cache_key: str) -> Optional[dict[str, Any]]:
        return self.get_cache(cache_key)

    def save_cache(self, video_id: str, data: dict[str, Any]) -> bool:
        video_hash = self._calculate_video_hash(video_id)
        cache_file = self.cache_dir / f"{video_hash}.json"
        temp_file: Path | None = None
        try:
            temp_file = self.cache_dir / f"{video_hash}.json.tmp.{uuid.uuid4().hex[:8]}"
            temp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(temp_file), str(cache_file))
            self.index[video_hash] = {"video_id": video_id, "file": f"{video_hash}.json"}
            self._save_index()
            return True
        finally:
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    def save(self, cache_key: str, payload: dict[str, Any]) -> None:
        self.save_cache(cache_key, payload)

    def clear_cache(self, video_id: Optional[str] = None) -> bool:
        try:
            if video_id:
                video_hash = self._calculate_video_hash(video_id)
                cache_file = self.cache_dir / f"{video_hash}.json"
                if cache_file.exists():
                    cache_file.unlink()
                self.index.pop(video_hash, None)
                self._save_index()
            else:
                for cache_file in self.cache_dir.glob("*.json"):
                    cache_file.unlink()
                self.index = {}
                self._save_index()
            return True
        except Exception:
            return False
