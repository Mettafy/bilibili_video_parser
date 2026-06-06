"""ffmpeg 视频处理。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from PIL import Image


class VideoParser:
    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        *,
        ffmpeg_probe_timeout_sec: int = 10,
        ffmpeg_extract_audio_timeout_sec: int = 120,
        ffmpeg_extract_frames_timeout_sec: int = 30,
    ) -> None:
        self.ffmpeg_path = self._detect_ffmpeg(ffmpeg_path)
        self.ffprobe_path = self._detect_ffprobe(ffmpeg_path)
        self._ffmpeg_probe_timeout_sec = max(1, int(ffmpeg_probe_timeout_sec))
        self._ffmpeg_extract_audio_timeout_sec = max(1, int(ffmpeg_extract_audio_timeout_sec))
        self._ffmpeg_extract_frames_timeout_sec = max(1, int(ffmpeg_extract_frames_timeout_sec))

    def _detect_ffmpeg(self, custom_path: Optional[str] = None) -> Optional[str]:
        if custom_path:
            if os.path.isdir(custom_path):
                candidate = os.path.join(custom_path, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
                if os.path.isfile(candidate):
                    return candidate
            if os.path.isfile(custom_path):
                return custom_path
            found = shutil.which(custom_path)
            if found:
                return found
        return shutil.which("ffmpeg")

    def _detect_ffprobe(self, custom_ffmpeg_path: Optional[str] = None) -> Optional[str]:
        if custom_ffmpeg_path and os.path.isfile(custom_ffmpeg_path):
            parent_dir = os.path.dirname(custom_ffmpeg_path)
            for name in ("ffprobe.exe", "ffprobe"):
                candidate = os.path.join(parent_dir, name)
                if os.path.isfile(candidate):
                    return candidate
        return shutil.which("ffprobe")

    def get_video_duration(self, video_path: str) -> Optional[float]:
        if not self.ffprobe_path:
            return None
        try:
            result = subprocess.run([
                self.ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._ffmpeg_probe_timeout_sec)
            if result.returncode == 0:
                return float(result.stdout.decode().strip())
        except Exception:
            return None
        return None

    def _create_frames_temp_dir(self) -> str:
        temp_dir = Path(__file__).resolve().parent.parent / "data" / "temp" / "frames" / f"bili_frames_{os.urandom(4).hex()}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return str(temp_dir)

    def _ensure_jpeg_format(self, image_path: str) -> str:
        try:
            with Image.open(image_path) as img:
                if img.format == "JPEG":
                    return image_path
                if img.mode in ("RGBA", "LA", "P"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                jpeg_path = image_path.rsplit(".", 1)[0] + ".jpg"
                img.save(jpeg_path, "JPEG", quality=85)
                if jpeg_path != image_path and os.path.exists(image_path):
                    os.remove(image_path)
                return jpeg_path
        except Exception:
            return image_path

    async def extract_frames(self, video_path: str, interval_sec: int = 6, max_frames: int = 10) -> list[str]:
        frames: list[str] = []
        temp_dir = self._create_frames_temp_dir()
        output_pattern = os.path.join(temp_dir, "frame_%03d.jpg")
        cmd = [self.ffmpeg_path, "-i", video_path, "-vf", f"fps=1/{interval_sec}", "-frames:v", str(max_frames), "-qscale:v", "2", output_pattern]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self._ffmpeg_extract_frames_timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return []
        if process.returncode != 0:
            return []
        for i in range(1, max_frames + 1):
            frame_path = os.path.join(temp_dir, f"frame_{i:03d}.jpg")
            if os.path.exists(frame_path):
                frames.append(self._ensure_jpeg_format(frame_path))
        return frames

    async def extract_frames_equidistant(self, video_path: str, duration_sec: float, count: int = 10, output_format: str = "jpeg") -> list[str]:
        del output_format
        frames: list[str] = []
        temp_dir = self._create_frames_temp_dir()
        total = max(0.0, float(duration_sec))
        times = [(i / (count + 1.0)) * total for i in range(1, max(1, int(count)) + 1)]
        for idx, t in enumerate(times, start=1):
            frame_path = os.path.join(temp_dir, f"frame_{idx:03d}.jpg")
            cmd = [self.ffmpeg_path, "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video_path, "-frames:v", "1", "-qscale:v", "2", "-f", "image2", "-c:v", "mjpeg", frame_path]
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                await asyncio.wait_for(process.communicate(), timeout=self._ffmpeg_extract_frames_timeout_sec)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                continue
            if process.returncode == 0 and os.path.exists(frame_path):
                frames.append(self._ensure_jpeg_format(frame_path))
        return frames

    async def extract_audio(self, video_path: str) -> Optional[str]:
        if not self.ffmpeg_path:
            return None
        audio_dir = Path(__file__).resolve().parent.parent / "data" / "temp" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / f"bili_audio_{os.urandom(4).hex()}.wav"
        cmd = [self.ffmpeg_path, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", str(audio_path)]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(process.communicate(), timeout=self._ffmpeg_extract_audio_timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return None
        if process.returncode != 0 or not audio_path.exists():
            return None
        return str(audio_path)
