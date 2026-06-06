"""临时文件管理。"""

from __future__ import annotations

import shutil
from pathlib import Path


class TempManager:
    """插件临时目录管理。"""

    def __init__(self, temp_dir: Path) -> None:
        self._temp_dir = temp_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    def new_work_dir(self, prefix: str) -> Path:
        work_dir = self._temp_dir / prefix
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def cleanup_path(self, path: str | Path) -> None:
        target = Path(path)
        if target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
