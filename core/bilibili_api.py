"""B 站 API 封装。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time
import uuid
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .retry_utils import ErrorType, NonRetryableError, RetryableError, classify_bilibili_error, classify_http_error, retry_async


class BilibiliAPI:
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    COMMON_HEADERS = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_RETRY_INTERVAL = 2.0
    _WBI_MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]
    _wbi_keys_cache: Optional[Tuple[str, str]] = None
    _wbi_keys_expire_at: float = 0.0
    _wbi_keys_lock: Optional[asyncio.Lock] = None
    _WBI_CACHE_TTL_SEC = 60 * 30

    @staticmethod
    def _build_headers(sessdata: str = "", include_cookie: bool = True) -> dict[str, str]:
        headers = dict(BilibiliAPI.COMMON_HEADERS)
        if include_cookie and sessdata:
            headers["Cookie"] = f"SESSDATA={sessdata}"
        return headers

    @staticmethod
    def _ensure_wbi_lock() -> asyncio.Lock:
        if BilibiliAPI._wbi_keys_lock is None:
            BilibiliAPI._wbi_keys_lock = asyncio.Lock()
        return BilibiliAPI._wbi_keys_lock

    @staticmethod
    def _invalidate_wbi_cache() -> None:
        BilibiliAPI._wbi_keys_cache = None
        BilibiliAPI._wbi_keys_expire_at = 0.0

    @staticmethod
    def _get_mixin_key(orig: str) -> str:
        return "".join(orig[i] for i in BilibiliAPI._WBI_MIXIN_KEY_ENC_TAB)[:32]

    @staticmethod
    def _sign_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, Any]:
        mixin_key = BilibiliAPI._get_mixin_key(img_key + sub_key)
        signed = dict(params)
        signed["wts"] = round(time.time())
        signed = dict(sorted(signed.items()))
        filtered: dict[str, Any] = {}
        for key, value in signed.items():
            filtered[key] = "".join(ch for ch in str(value) if ch not in "!'()*")
        query = urlencode(filtered)
        filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return filtered

    @staticmethod
    async def _fetch_wbi_keys(force_refresh: bool = False) -> Tuple[str, str]:
        now = time.time()
        if not force_refresh and BilibiliAPI._wbi_keys_cache and now < BilibiliAPI._wbi_keys_expire_at:
            return BilibiliAPI._wbi_keys_cache

        lock = BilibiliAPI._ensure_wbi_lock()
        async with lock:
            now = time.time()
            if not force_refresh and BilibiliAPI._wbi_keys_cache and now < BilibiliAPI._wbi_keys_expire_at:
                return BilibiliAPI._wbi_keys_cache

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get("https://api.bilibili.com/x/web-interface/nav", headers=BilibiliAPI._build_headers(include_cookie=False)) as response:
                    if response.status != 200:
                        raise RetryableError("获取WBI key失败", ErrorType.NETWORK_ERROR)
                    data = await response.json()

            wbi_img = data.get("data", {}).get("wbi_img", {})
            img_url = wbi_img.get("img_url", "")
            sub_url = wbi_img.get("sub_url", "")
            if not img_url or not sub_url:
                raise RetryableError("WBI key数据缺失", ErrorType.NETWORK_ERROR)
            img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
            BilibiliAPI._wbi_keys_cache = (img_key, sub_key)
            BilibiliAPI._wbi_keys_expire_at = time.time() + BilibiliAPI._WBI_CACHE_TTL_SEC
            return img_key, sub_key

    @staticmethod
    async def _call_playurl(aid: int, cid: int, *, use_wbi: bool, force_refresh_wbi: bool = False, timeout_sec: int = 30) -> dict[str, Any]:
        base_params = {"avid": aid, "cid": cid, "qn": 64, "fnval": 0, "fourk": 1}
        if use_wbi:
            img_key, sub_key = await BilibiliAPI._fetch_wbi_keys(force_refresh=force_refresh_wbi)
            query = urlencode(BilibiliAPI._sign_wbi_params(base_params, img_key, sub_key))
            url = f"https://api.bilibili.com/x/player/wbi/playurl?{query}"
        else:
            url = f"https://api.bilibili.com/x/player/playurl?{urlencode(base_params)}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
            async with session.get(url, headers=BilibiliAPI._build_headers(include_cookie=False)) as response:
                return {"status": response.status, "data": await response.json(content_type=None), "use_wbi": use_wbi}

    @staticmethod
    def extract_page_from_url(url: str) -> int:
        try:
            parsed = urlparse(url)
            query_params = parse_qs(parsed.query)
            if "p" in query_params:
                return int(query_params["p"][0])
        except Exception:
            pass
        return 1

    @staticmethod
    def extract_video_id(text: str) -> Optional[Tuple[str, str, int]]:
        url_pattern = r'https?://(?:www\.|m\.)?bilibili\.com/video/(BV[a-zA-Z0-9]{10}|av\d+)[^\s]*'
        url_match = re.search(url_pattern, text)
        if url_match:
            full_url = url_match.group(0)
            vid = url_match.group(1)
            page = BilibiliAPI.extract_page_from_url(full_url)
            return ("bv", vid, page) if vid.startswith("BV") else ("av", vid, page)
        short_match = re.search(r'https?://b23\.tv/([a-zA-Z0-9]+)', text)
        if short_match:
            return ("short", short_match.group(1), 1)
        bv_match = re.search(r'\b(BV[a-zA-Z0-9]{10})\b', text, re.IGNORECASE)
        if bv_match:
            return ("bv", bv_match.group(1), 1)
        av_match = re.search(r'\bav(\d+)\b', text, re.IGNORECASE)
        if av_match:
            return ("av", f"av{av_match.group(1)}", 1)
        return None

    @staticmethod
    async def resolve_short_url(short_code: str, timeout_sec: int = 10) -> Optional[Tuple[str, int]]:
        short_url = f"https://b23.tv/{short_code}"
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(short_url, headers=BilibiliAPI._build_headers(include_cookie=False), allow_redirects=False) as response:
                    if response.status not in (301, 302, 303, 307, 308):
                        return None
                    location = response.headers.get("Location", "")
                    if not location:
                        return None
                    page = BilibiliAPI.extract_page_from_url(location)
                    bv_match = re.search(r'BV[a-zA-Z0-9]{10}', location, re.IGNORECASE)
                    if bv_match:
                        return bv_match.group(0), page
                    av_match = re.search(r'av(\d+)', location, re.IGNORECASE)
                    if av_match:
                        return f"av{av_match.group(1)}", page
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    @staticmethod
    async def get_video_info(video_id: str, sessdata: str = "", page: int = 1, max_attempts: int | None = None, retry_interval: float | None = None, timeout_sec: int = 30) -> Optional[dict[str, Any]]:
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        if video_id.startswith("av"):
            aid = re.search(r'av(\d+)', video_id, re.IGNORECASE).group(1)
            url = f"https://api.bilibili.com/x/web-interface/view?aid={aid}"
        else:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={video_id}"
        headers = BilibiliAPI._build_headers(sessdata, include_cookie=False)
        if sessdata:
            headers["Cookie"] = f"SESSDATA={sessdata}"

        async def _fetch() -> dict[str, Any] | None:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as response:
                    if response.status != 200:
                        error_type, retryable = classify_http_error(response.status)
                        if retryable:
                            raise RetryableError(f"HTTP请求失败: status={response.status}", error_type)
                        raise NonRetryableError(f"HTTP请求失败: status={response.status}", error_type)
                    data = await response.json()
                    code = data.get("code", 0)
                    if code != 0:
                        message = data.get("message", "未知错误")
                        error_type, retryable = classify_bilibili_error(code, message)
                        if retryable:
                            raise RetryableError(f"B站API错误: code={code}, message={message}", error_type)
                        raise NonRetryableError(f"B站API错误: code={code}, message={message}", error_type)
                    video_data = data.get("data", {})
                    pages = video_data.get("pages", [])
                    if not pages:
                        return None
                    page_index = max(0, min(page - 1, len(pages) - 1))
                    selected_page = pages[page_index]
                    return {
                        "aid": video_data.get("aid"),
                        "bvid": video_data.get("bvid"),
                        "cid": selected_page.get("cid"),
                        "title": video_data.get("title"),
                        "desc": video_data.get("desc"),
                        "duration": selected_page.get("duration", video_data.get("duration")),
                        "owner": video_data.get("owner", {}),
                        "page": page_index + 1,
                        "page_title": selected_page.get("part", ""),
                        "total_pages": len(pages),
                        "total_duration": sum(p.get("duration", 0) for p in pages),
                    }

        try:
            return await retry_async(_fetch, max_attempts=max_attempts, interval_sec=retry_interval, retryable_exceptions=(RetryableError,))
        except NonRetryableError:
            raise
        except RetryableError:
            return None

    @staticmethod
    async def get_subtitle(aid: int, cid: int, sessdata: str = "", max_attempts: int | None = None, retry_interval: float | None = None, timeout_sec: int = 30) -> Optional[str]:
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        url = f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}"
        headers = BilibiliAPI._build_headers(sessdata)

        async def _fetch() -> Optional[str]:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as response:
                    if response.status != 200:
                        error_type, retryable = classify_http_error(response.status)
                        if retryable:
                            raise RetryableError(f"HTTP请求失败: status={response.status}", error_type)
                        return None
                    data = await response.json()
                    if data.get("code", 0) != 0:
                        return None
                    subtitles = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
                    if not subtitles:
                        return None
                    selected = next((s for s in subtitles if "中文" in s.get("lan_doc", "")), subtitles[0])
                    subtitle_url = selected.get("subtitle_url")
                    if not subtitle_url:
                        return None
                    if subtitle_url.startswith("//"):
                        subtitle_url = "https:" + subtitle_url
                    elif not subtitle_url.startswith("http"):
                        subtitle_url = "https://" + subtitle_url
                    return await BilibiliAPI._download_subtitle(subtitle_url, timeout_sec=timeout_sec)

        try:
            return await retry_async(_fetch, max_attempts=max_attempts, interval_sec=retry_interval, retryable_exceptions=(RetryableError,))
        except RetryableError:
            return None

    @staticmethod
    async def _download_subtitle(subtitle_url: str, timeout_sec: int = 30) -> Optional[str]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as session:
                async with session.get(subtitle_url, headers=BilibiliAPI._build_headers(include_cookie=False)) as response:
                    if response.status != 200:
                        return None
                    subtitle_data = await response.json(content_type=None)
                    body = subtitle_data.get("body", []) if isinstance(subtitle_data, dict) else []
                    texts = [str(item.get("content", "")).strip() for item in body if isinstance(item, dict) and str(item.get("content", "")).strip()]
                    return " ".join(texts) if texts else None
        except Exception:
            return None

    @staticmethod
    async def get_video_download_url(video_id: str, sessdata: str = "", page: int = 1, max_attempts: int | None = None, retry_interval: float | None = None, timeout_sec: int = 30) -> Optional[dict[str, Any]]:
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        video_info = await BilibiliAPI.get_video_info(video_id, sessdata, page, max_attempts, retry_interval)
        if not video_info:
            return None
        aid = video_info.get("aid")
        cid = video_info.get("cid")
        if not aid or not cid:
            return None

        async def _fetch() -> Optional[dict[str, Any]]:
            try:
                resp = await BilibiliAPI._call_playurl(aid, cid, use_wbi=True, timeout_sec=timeout_sec)
            except RetryableError:
                resp = await BilibiliAPI._call_playurl(aid, cid, use_wbi=False, timeout_sec=timeout_sec)
            status = resp.get("status", 0)
            data = resp.get("data", {})
            if status != 200:
                if status == 412:
                    BilibiliAPI._invalidate_wbi_cache()
                    raise RetryableError("HTTP请求失败: status=412", ErrorType.PERMISSION_DENIED)
                error_type, retryable = classify_http_error(status)
                if retryable:
                    raise RetryableError(f"HTTP请求失败: status={status}", error_type)
                raise NonRetryableError(f"HTTP请求失败: status={status}", error_type)
            if data.get("code", 0) != 0:
                message = data.get("message", "未知错误")
                error_type, retryable = classify_bilibili_error(data.get("code", 0), message)
                if retryable:
                    raise RetryableError(f"B站API错误: code={data.get('code', 0)}, message={message}", error_type)
                raise NonRetryableError(f"B站API错误: code={data.get('code', 0)}, message={message}", error_type)
            download_url = None
            durl = data.get("data", {}).get("durl") or []
            if durl and isinstance(durl, list):
                download_url = durl[0].get("url") or (durl[0].get("backup_url") or [None])[0]
            dash = data.get("data", {}).get("dash") or {}
            if not download_url and isinstance(dash, dict):
                video_list = dash.get("video") or []
                if video_list:
                    first_video = video_list[0]
                    download_url = first_video.get("baseUrl") or first_video.get("base_url") or (first_video.get("backupUrl") or first_video.get("backup_url") or [None])[0]
            if not download_url:
                return None
            return {"url": download_url, "title": video_info.get("title"), "duration": video_info.get("duration"), "aid": aid, "cid": cid}

        try:
            return await retry_async(_fetch, max_attempts=max_attempts, interval_sec=retry_interval, retryable_exceptions=(RetryableError,))
        except NonRetryableError:
            raise
        except RetryableError:
            return None

    @staticmethod
    async def download_video(url: str, max_size_mb: int = 100, timeout_sec: int = 300, max_attempts: int | None = None, retry_interval: float | None = None) -> Optional[str]:
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        videos_temp_dir = os.path.join(os.path.dirname(__file__), "..", "data", "temp", "videos")
        os.makedirs(videos_temp_dir, exist_ok=True)
        tmp_path = os.path.join(videos_temp_dir, f"bili_video_{uuid.uuid4().hex[:8]}.mp4")
        max_bytes = max_size_mb * 1024 * 1024

        async def _download() -> Optional[str]:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=BilibiliAPI._build_headers(include_cookie=False), timeout=aiohttp.ClientTimeout(total=timeout_sec)) as response:
                    if response.status != 200:
                        error_type, retryable = classify_http_error(response.status)
                        if retryable:
                            raise RetryableError(f"下载失败: status={response.status}", error_type)
                        raise NonRetryableError(f"下载失败: status={response.status}", error_type)
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > max_bytes:
                        raise NonRetryableError("视频文件过大", ErrorType.VIDEO_TOO_LARGE)
                    total_downloaded = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(8192):
                            total_downloaded += len(chunk)
                            if total_downloaded > max_bytes:
                                try:
                                    os.remove(tmp_path)
                                except Exception:
                                    pass
                                raise NonRetryableError("下载超过大小限制", ErrorType.VIDEO_TOO_LARGE)
                            f.write(chunk)
                    return tmp_path

        try:
            return await retry_async(_download, max_attempts=max_attempts, interval_sec=retry_interval, retryable_exceptions=(RetryableError,))
        except NonRetryableError:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise
        except RetryableError:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return None
