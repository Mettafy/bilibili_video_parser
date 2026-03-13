# -*- coding: utf-8 -*-
"""
B站API封装模块

本模块封装了与B站（Bilibili）交互的所有API调用，包括：
1. 视频ID提取和解析（BV号、AV号、短链接）
2. 视频基本信息获取（标题、简介、时长、UP主等）
3. 字幕获取（需要SESSDATA Cookie）
4. 视频下载地址获取和视频下载

主要类：
- BilibiliAPI: B站API封装类，提供静态方法

支持的视频ID格式：
- BV号: BV1xx411c7mD
- AV号: av170001
- 完整链接: https://www.bilibili.com/video/BV1xx411c7mD
- 短链接: https://b23.tv/xxxxxx
- 带分P参数: https://www.bilibili.com/video/BV1xx411c7mD?p=2

重试机制：
- 所有网络请求都支持自动重试
- 可配置最大重试次数和重试间隔
- 根据错误类型自动判断是否可重试

错误处理：
- RetryableError: 可重试的错误（网络超时、服务器错误等）
- NonRetryableError: 不可重试的错误（视频不存在、无权限等）

使用示例：
    # 提取视频ID
    video_type, video_id, page = BilibiliAPI.extract_video_id("BV1xx411c7mD")
    
    # 获取视频信息
    info = await BilibiliAPI.get_video_info(video_id, sessdata, page)
    
    # 获取字幕
    subtitle = await BilibiliAPI.get_subtitle(aid, cid, sessdata)
    
    # 下载视频
    download_info = await BilibiliAPI.get_video_download_url(video_id)
    video_path = await BilibiliAPI.download_video(download_info['url'])

依赖：
- aiohttp: 异步HTTP客户端
- retry_utils: 重试工具模块
- safe_delete: 安全删除模块（获取临时目录）

Author: 约瑟夫.k && 白泽
"""
import os
import re
import time
import random
import hashlib
import asyncio
import uuid
from urllib.parse import urlparse, parse_qs, urlencode
from typing import Optional, Dict, Any, Tuple, Callable
import aiohttp
from src.plugin_system import get_logger
from .safe_delete import get_temp_subdir
from .retry_utils import (
    ErrorType,
    RetryableError,
    NonRetryableError,
    classify_bilibili_error,
    classify_http_error,
    retry_async,
)

logger = get_logger("bilibili_api")


class BilibiliAPI:
    """B站API封装类"""
    
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # 统一请求头（避免各接口头不一致）
    COMMON_HEADERS = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 默认重试配置
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_RETRY_INTERVAL = 2.0

    # WBI key 缓存（类级）
    _wbi_keys_cache: Optional[Tuple[str, str]] = None
    _wbi_keys_expire_at: float = 0.0
    _wbi_keys_lock: Optional[asyncio.Lock] = None
    _WBI_CACHE_TTL_SEC = 60 * 30  # 30分钟

    # WBI 混淆映射表
    _WBI_MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]
    
    @staticmethod
    def _build_headers(sessdata: str = "", include_cookie: bool = True) -> Dict[str, str]:
        headers = dict(BilibiliAPI.COMMON_HEADERS)
        if include_cookie and sessdata:
            headers['Cookie'] = f'SESSDATA={sessdata}'
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
        return ''.join(orig[i] for i in BilibiliAPI._WBI_MIXIN_KEY_ENC_TAB)[:32]

    @staticmethod
    def _sign_wbi_params(params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
        mixin_key = BilibiliAPI._get_mixin_key(img_key + sub_key)
        signed = dict(params)
        signed['wts'] = round(time.time())
        signed = dict(sorted(signed.items()))

        filtered = {}
        for k, v in signed.items():
            value = ''.join(ch for ch in str(v) if ch not in "!'()*")
            filtered[k] = value

        query = urlencode(filtered)
        filtered['w_rid'] = hashlib.md5((query + mixin_key).encode('utf-8')).hexdigest()
        return filtered

    @staticmethod
    async def _fetch_wbi_keys(force_refresh: bool = False) -> Tuple[str, str]:
        now = time.time()
        if (
            not force_refresh
            and BilibiliAPI._wbi_keys_cache
            and now < BilibiliAPI._wbi_keys_expire_at
        ):
            return BilibiliAPI._wbi_keys_cache

        lock = BilibiliAPI._ensure_wbi_lock()
        async with lock:
            now = time.time()
            if (
                not force_refresh
                and BilibiliAPI._wbi_keys_cache
                and now < BilibiliAPI._wbi_keys_expire_at
            ):
                return BilibiliAPI._wbi_keys_cache

            nav_url = "https://api.bilibili.com/x/web-interface/nav"
            headers = BilibiliAPI._build_headers(include_cookie=False)
            timeout = aiohttp.ClientTimeout(total=15)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(nav_url, headers=headers) as response:
                    if response.status != 200:
                        raise RetryableError(
                            f"获取WBI key失败: status={response.status}",
                            ErrorType.NETWORK_ERROR,
                        )
                    data = await response.json()

            wbi_img = data.get('data', {}).get('wbi_img', {})
            img_url = wbi_img.get('img_url', '')
            sub_url = wbi_img.get('sub_url', '')
            if not img_url or not sub_url:
                raise RetryableError("WBI key数据缺失", ErrorType.NETWORK_ERROR)

            img_key = img_url.rsplit('/', 1)[-1].split('.', 1)[0]
            sub_key = sub_url.rsplit('/', 1)[-1].split('.', 1)[0]
            if not img_key or not sub_key:
                raise RetryableError("WBI key解析失败", ErrorType.NETWORK_ERROR)

            BilibiliAPI._wbi_keys_cache = (img_key, sub_key)
            BilibiliAPI._wbi_keys_expire_at = time.time() + BilibiliAPI._WBI_CACHE_TTL_SEC
            return img_key, sub_key

    @staticmethod
    async def _call_playurl(
        aid: int,
        cid: int,
        *,
        use_wbi: bool,
        sessdata: str = "",
        force_refresh_wbi: bool = False,
    ) -> Dict[str, Any]:
        base_params = {
            'avid': aid,
            'cid': cid,
            'qn': 64,
            'fnval': 0,
            'fourk': 1,
        }

        if use_wbi:
            img_key, sub_key = await BilibiliAPI._fetch_wbi_keys(force_refresh=force_refresh_wbi)
            signed = BilibiliAPI._sign_wbi_params(base_params, img_key, sub_key)
            query = urlencode(signed)
            url = f"https://api.bilibili.com/x/player/wbi/playurl?{query}"
        else:
            query = urlencode(base_params)
            url = f"https://api.bilibili.com/x/player/playurl?{query}"

        headers = BilibiliAPI._build_headers(sessdata, include_cookie=False)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except Exception as e:
                    raise RetryableError(f"playurl响应解析失败: {e}", ErrorType.NETWORK_ERROR)
                return {
                    'status': status,
                    'data': data,
                    'url': url,
                    'use_wbi': use_wbi,
                }

    @staticmethod
    def _extract_download_url_from_playurl(data: Dict[str, Any]) -> Optional[str]:
        payload = data.get('data', {}) if isinstance(data, dict) else {}

        durl = payload.get('durl') or []
        if durl and isinstance(durl, list):
            first = durl[0] if durl else {}
            if isinstance(first, dict):
                url = first.get('url')
                if url:
                    return url
                backup = first.get('backup_url') or []
                if backup and isinstance(backup, list):
                    return backup[0]

        dash = payload.get('dash') or {}
        if isinstance(dash, dict):
            video_list = dash.get('video') or []
            if video_list and isinstance(video_list, list):
                first_video = video_list[0] if video_list else {}
                if isinstance(first_video, dict):
                    url = first_video.get('baseUrl') or first_video.get('base_url')
                    if url:
                        return url
                    backup = first_video.get('backupUrl') or first_video.get('backup_url') or []
                    if backup and isinstance(backup, list):
                        return backup[0]

        return None

    @staticmethod
    def extract_page_from_url(url: str) -> int:
        """从URL中提取分P号
        
        Args:
            url: B站视频URL
            
        Returns:
            分P号（从1开始），默认返回1
        """
        try:
            parsed = urlparse(url)
            query_params = parse_qs(parsed.query)
            
            # p参数可能在不同位置，parse_qs会自动处理
            if 'p' in query_params:
                return int(query_params['p'][0])
        except (ValueError, IndexError, KeyError):
            pass
        
        return 1  # 默认第1P
    
    @staticmethod
    def extract_video_id(text: str) -> Optional[Tuple[str, str, int]]:
        """从文本中提取视频ID和分P号
        
        Args:
            text: 输入文本
            
        Returns:
            (视频ID类型, 视频ID, 分P号) 或 None
            类型可能是 'bv', 'av', 'short'
            分P号从1开始
        """
        # 匹配B站链接（包含分P参数）
        url_pattern = r'https?://(?:www\.|m\.)?bilibili\.com/video/(BV[a-zA-Z0-9]{10}|av\d+)[^\s]*'
        url_match = re.search(url_pattern, text)
        if url_match:
            full_url = url_match.group(0)
            vid = url_match.group(1)
            page = BilibiliAPI.extract_page_from_url(full_url)
            if vid.startswith('BV'):
                return ('bv', vid, page)
            else:
                return ('av', vid, page)
        
        # 匹配b23.tv短链接
        short_url_pattern = r'https?://b23\.tv/([a-zA-Z0-9]+)'
        short_match = re.search(short_url_pattern, text)
        if short_match:
            short_code = short_match.group(1)
            # 返回短链接类型，需要后续解析获取分P
            return ('short', short_code, 1)  # 分P号将在resolve_short_url中获取
        
        # 匹配纯BV号
        bv_pattern = r'BV[a-zA-Z0-9]{10}'
        bv_match = re.search(bv_pattern, text, re.IGNORECASE)
        if bv_match:
            return ('bv', bv_match.group(0), 1)  # 纯BV号默认第1P
        
        # 匹配纯AV号
        av_pattern = r'av(\d+)'
        av_match = re.search(av_pattern, text, re.IGNORECASE)
        if av_match:
            return ('av', f"av{av_match.group(1)}", 1)  # 纯AV号默认第1P
        
        return None
    
    @staticmethod
    async def resolve_short_url(short_code: str) -> Optional[Tuple[str, int]]:
        """解析b23.tv短链接，获取真实的视频ID和分P号
        
        Args:
            short_code: 短链接代码（如 ocaOWef）
            
        Returns:
            (视频ID, 分P号) 或 None
            视频ID为BV号或AV号，分P号从1开始
        """
        short_url = f"https://b23.tv/{short_code}"
        headers = BilibiliAPI._build_headers(include_cookie=False)
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 不自动跟随重定向，手动获取Location
                async with session.get(
                    short_url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=timeout,
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location', '')
                        if not location:
                            logger.warning(f"[BilibiliAPI] 短链接重定向缺少Location: code={short_code}")
                            return None

                        # 从重定向URL中提取分P号
                        page = BilibiliAPI.extract_page_from_url(location)

                        # 从重定向URL中提取视频ID
                        bv_match = re.search(r'BV[a-zA-Z0-9]{10}', location, re.IGNORECASE)
                        if bv_match:
                            video_id = bv_match.group(0)
                            logger.debug(f"[BilibiliAPI] 短链接解析成功: {short_code} -> {video_id}, p={page}")
                            return (video_id, page)

                        av_match = re.search(r'av(\d+)', location, re.IGNORECASE)
                        if av_match:
                            video_id = f"av{av_match.group(1)}"
                            logger.debug(f"[BilibiliAPI] 短链接解析成功: {short_code} -> {video_id}, p={page}")
                            return (video_id, page)

                        logger.warning(f"[BilibiliAPI] 短链接重定向URL中未找到视频ID: {location}")
                    else:
                        logger.warning(f"[BilibiliAPI] 短链接请求未重定向: status={response.status}, code={short_code}")

            return None
        except asyncio.TimeoutError:
            logger.warning(f"[BilibiliAPI] 解析短链接超时: code={short_code}")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"[BilibiliAPI] 解析短链接网络异常: code={short_code}, error={e}")
            return None
        except Exception as e:
            logger.error(f"[BilibiliAPI] 解析短链接失败: {e}")
            return None

    @staticmethod
    async def get_video_info(
        video_id: str,
        sessdata: str = "",
        page: int = 1,
        max_attempts: int = None,
        retry_interval: float = None,
    ) -> Optional[Dict[str, Any]]:
        """获取视频基本信息（带重试机制）
        
        Args:
            video_id: 视频ID (BV号或AV号)
            sessdata: B站SESSDATA Cookie
            page: 分P号（从1开始），默认为1
            max_attempts: 最大重试次数，默认使用类默认值
            retry_interval: 重试间隔（秒），默认使用类默认值
            
        Returns:
            视频信息字典，包含aid、cid、title、desc、page、page_title等
            
        Raises:
            NonRetryableError: 不可重试的错误（如视频不存在）
        """
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        
        # 根据视频ID类型构建URL
        if video_id.startswith('av'):
            aid = re.search(r'av(\d+)', video_id, re.IGNORECASE).group(1)
            url = f"https://api.bilibili.com/x/web-interface/view?aid={aid}"
        else:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={video_id}"
        
        headers = BilibiliAPI._build_headers(include_cookie=False)
        
        if sessdata:
            headers['Cookie'] = f'SESSDATA={sessdata}'
        
        logger.debug(f"[BilibiliAPI] 获取视频信息: {video_id}, 分P: {page}")
        
        async def _fetch():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            data = await response.json()
                            code = data.get('code', 0)
                            
                            if code == 0:
                                video_data = data.get('data', {})
                                pages = video_data.get('pages', [])
                                if pages:
                                    # 根据分P号获取对应的cid
                                    # page从1开始，数组索引从0开始
                                    page_index = max(0, min(page - 1, len(pages) - 1))
                                    selected_page = pages[page_index]
                                    
                                    # 获取分P标题（如果有）
                                    page_title = selected_page.get('part', '')
                                    page_duration = selected_page.get('duration', video_data.get('duration'))
                                    
                                    # 计算合集总时长（所有分P时长之和）
                                    total_duration = sum(p.get('duration', 0) for p in pages)
                                    
                                    result = {
                                        'aid': video_data.get('aid'),
                                        'bvid': video_data.get('bvid'),
                                        'cid': selected_page.get('cid'),
                                        'title': video_data.get('title'),
                                        'desc': video_data.get('desc'),
                                        'duration': page_duration,  # 使用分P的时长
                                        'owner': video_data.get('owner', {}),
                                        'page': page_index + 1,  # 实际使用的分P号
                                        'page_title': page_title,  # 分P标题
                                        'total_pages': len(pages),  # 总分P数
                                        'total_duration': total_duration,  # 合集总时长
                                    }
                                    
                                    return result
                            else:
                                # 根据B站错误码分类
                                message = data.get('message', '未知错误')
                                error_type, retryable = classify_bilibili_error(code, message)
                                
                                if retryable:
                                    raise RetryableError(f"B站API错误: code={code}, message={message}", error_type)
                                else:
                                    raise NonRetryableError(f"B站API错误: code={code}, message={message}", error_type)
                        else:
                            # 根据HTTP状态码分类
                            error_type, retryable = classify_http_error(response.status)
                            
                            if retryable:
                                raise RetryableError(f"HTTP请求失败: status={response.status}", error_type)
                            else:
                                raise NonRetryableError(f"HTTP请求失败: status={response.status}", error_type)
                
                await asyncio.sleep(0.5)  # 请求间隔
                return None
                
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 网络错误，可重试
                raise RetryableError(f"网络错误: {e}", ErrorType.NETWORK_ERROR)
        
        try:
            return await retry_async(
                _fetch,
                max_attempts=max_attempts,
                interval_sec=retry_interval,
                retryable_exceptions=(RetryableError,),
            )
        except NonRetryableError:
            raise
        except RetryableError as e:
            logger.error(f"[BilibiliAPI] 获取视频信息失败（重试{max_attempts}次后）: {e}")
            return None
        except Exception as e:
            logger.error(f"[BilibiliAPI] 获取视频信息失败: {e}")
            return None

    @staticmethod
    async def get_subtitle(
        aid: int,
        cid: int,
        sessdata: str = "",
        max_attempts: int = None,
        retry_interval: float = None,
    ) -> Optional[str]:
        """获取视频字幕（带重试机制）
        
        Args:
            aid: 视频AV号
            cid: 视频CID
            sessdata: B站SESSDATA Cookie
            max_attempts: 最大重试次数
            retry_interval: 重试间隔（秒）
            
        Returns:
            字幕文本
        """
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        
        url = f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}"
        headers = BilibiliAPI._build_headers(sessdata)
        
        logger.debug(f"[BilibiliAPI] 获取字幕: aid={aid}, cid={cid}")
        
        async def _fetch():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            data = await response.json()
                            code = data.get('code', 0)
                            
                            if code == 0:
                                subtitle_data = data.get('data', {}).get('subtitle', {})
                                subtitles = subtitle_data.get('subtitles', [])
                                
                                if not subtitles:
                                    need_login = data.get('data', {}).get('need_login_subtitle', False)
                                    if need_login:
                                        logger.warning("[BilibiliAPI] 获取字幕需要登录，请配置SESSDATA")
                                    else:
                                        logger.debug("[BilibiliAPI] 该视频没有可用的字幕")
                                    return None
                                
                                # 优先选择中文字幕
                                selected_subtitle = None
                                for subtitle in subtitles:
                                    lan_doc = subtitle.get('lan_doc', '')
                                    if '中文' in lan_doc:
                                        selected_subtitle = subtitle
                                        break
                                
                                # 如果没有中文字幕，选择第一个
                                if not selected_subtitle and subtitles:
                                    selected_subtitle = subtitles[0]
                                
                                if selected_subtitle:
                                    subtitle_url = selected_subtitle.get('subtitle_url')
                                    if subtitle_url:
                                        # 确保URL是完整的
                                        if subtitle_url.startswith('//'):
                                            subtitle_url = 'https:' + subtitle_url
                                        elif not subtitle_url.startswith('http'):
                                            subtitle_url = 'https://' + subtitle_url
                                        
                                        return await BilibiliAPI._download_subtitle(subtitle_url)
                            else:
                                # 字幕获取失败通常不是致命错误，记录日志但不抛异常
                                message = data.get('message', '未知错误')
                                logger.warning(f"[BilibiliAPI] 获取字幕API返回错误: code={code}, message={message}")
                                return None
                        else:
                            # HTTP错误，根据状态码决定是否重试
                            error_type, retryable = classify_http_error(response.status)
                            if retryable:
                                raise RetryableError(f"HTTP请求失败: status={response.status}", error_type)
                            else:
                                logger.warning(f"[BilibiliAPI] 获取字幕HTTP请求失败: status={response.status}")
                                return None
                
                await asyncio.sleep(0.5)
                return None
                
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise RetryableError(f"网络错误: {e}", ErrorType.NETWORK_ERROR)
        
        try:
            return await retry_async(
                _fetch,
                max_attempts=max_attempts,
                interval_sec=retry_interval,
                retryable_exceptions=(RetryableError,),
            )
        except RetryableError as e:
            logger.error(f"[BilibiliAPI] 获取字幕失败（重试{max_attempts}次后）: {e}")
            return None
        except Exception as e:
            logger.error(f"[BilibiliAPI] 获取字幕失败: {e}")
            return None

    @staticmethod
    async def _download_subtitle(subtitle_url: str) -> Optional[str]:
        """下载字幕文件并提取文本
        
        Args:
            subtitle_url: 字幕文件URL
            
        Returns:
            字幕文本
        """
        headers = BilibiliAPI._build_headers(include_cookie=False)
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(subtitle_url, headers=headers) as response:
                    if response.status == 200:
                        subtitle_data = await response.json()
                        body = subtitle_data.get('body', [])
                        
                        if not body:
                            logger.warning("[BilibiliAPI] 字幕文件为空")
                            return None
                        
                        # 提取所有字幕文本
                        subtitle_texts = []
                        for item in body:
                            content = item.get('content', '').strip()
                            if content:
                                subtitle_texts.append(content)
                        
                        if not subtitle_texts:
                            logger.warning("[BilibiliAPI] 字幕内容为空")
                            return None
                        
                        full_text = ' '.join(subtitle_texts)
                        return full_text
                    else:
                        logger.warning(f"[BilibiliAPI] 下载字幕HTTP请求失败: status={response.status}")
            
            await asyncio.sleep(0.5)
            return None
        except Exception as e:
            logger.error(f"[BilibiliAPI] 下载字幕失败: {e}")
            return None
    
    @staticmethod
    async def get_video_download_url(
        video_id: str,
        sessdata: str = "",
        page: int = 1,
        max_attempts: int = None,
        retry_interval: float = None,
    ) -> Optional[Dict[str, Any]]:
        """获取视频下载链接（带重试机制）

        下载链路优先使用 WBI 签名接口以降低 412，失败后回退旧接口。

        Args:
            video_id: 视频ID (BV号或AV号)
            sessdata: B站SESSDATA Cookie（仅保留参数兼容，下载链路默认不依赖）
            page: 分P号（从1开始），默认为1
            max_attempts: 最大重试次数
            retry_interval: 重试间隔（秒）

        Returns:
            包含下载链接和视频信息的字典

        Raises:
            NonRetryableError: 不可重试的错误
        """
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL

        # 先获取视频基本信息（下载链路不依赖SESSDATA）
        video_info = await BilibiliAPI.get_video_info(video_id, "", page, max_attempts, retry_interval)
        if not video_info:
            return None

        aid = video_info.get('aid')
        cid = video_info.get('cid')
        if not aid or not cid:
            logger.error("[BilibiliAPI] 无法获取视频aid或cid")
            return None

        logger.debug(f"[BilibiliAPI] 获取下载地址: aid={aid}, cid={cid}")

        last_non_retryable: Optional[NonRetryableError] = None

        for attempt in range(1, max_attempts + 1):
            try:
                # 1) 优先 WBI
                try:
                    resp = await BilibiliAPI._call_playurl(
                        aid,
                        cid,
                        use_wbi=True,
                        sessdata="",
                        force_refresh_wbi=(attempt > 1),
                    )
                except RetryableError as e:
                    logger.warning(f"[BilibiliAPI] WBI请求失败，回退旧接口: {e}")
                    resp = await BilibiliAPI._call_playurl(
                        aid,
                        cid,
                        use_wbi=False,
                        sessdata="",
                    )

                status = resp.get('status', 0)
                data = resp.get('data', {}) if isinstance(resp, dict) else {}
                use_wbi = bool(resp.get('use_wbi'))

                # 2) HTTP状态码处理（412定向恢复）
                if status != 200:
                    if status == 412:
                        BilibiliAPI._invalidate_wbi_cache()
                        wait_time = retry_interval * (2 ** (attempt - 1)) + random.uniform(0, 0.6)
                        if attempt < max_attempts:
                            logger.warning(
                                "[BilibiliAPI] 下载地址触发412（%s），第%d/%d次，%.2fs后重试",
                                "WBI" if use_wbi else "Legacy",
                                attempt,
                                max_attempts,
                                wait_time,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        raise NonRetryableError(
                            "HTTP请求失败: status=412（可能触发B站风控）",
                            ErrorType.PERMISSION_DENIED,
                        )

                    error_type, retryable = classify_http_error(status)
                    if retryable:
                        raise RetryableError(f"HTTP请求失败: status={status}", error_type)
                    raise NonRetryableError(f"HTTP请求失败: status={status}", error_type)

                # 3) B站业务码处理
                code = data.get('code', 0)
                if code != 0:
                    message = data.get('message', '未知错误')
                    error_type, retryable = classify_bilibili_error(code, message)

                    # 部分场景风控会返回业务码+412提示
                    if ('412' in str(message)) and attempt < max_attempts:
                        BilibiliAPI._invalidate_wbi_cache()
                        wait_time = retry_interval * (2 ** (attempt - 1)) + random.uniform(0, 0.6)
                        logger.warning(
                            "[BilibiliAPI] 下载地址业务风控（code=%s, message=%s），第%d/%d次，%.2fs后重试",
                            code,
                            message,
                            attempt,
                            max_attempts,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    if retryable:
                        raise RetryableError(
                            f"B站API错误: code={code}, message={message}",
                            error_type,
                        )
                    raise NonRetryableError(
                        f"B站API错误: code={code}, message={message}",
                        error_type,
                    )

                # 4) 解析下载地址（兼容 durl/dash）
                download_url = BilibiliAPI._extract_download_url_from_playurl(data)
                if download_url:
                    return {
                        'url': download_url,
                        'title': video_info.get('title'),
                        'duration': video_info.get('duration'),
                        'aid': aid,
                        'cid': cid,
                    }

                # 5) WBI 成功但无链接，则回退旧接口再试一次
                if use_wbi:
                    fallback = await BilibiliAPI._call_playurl(
                        aid,
                        cid,
                        use_wbi=False,
                        sessdata="",
                    )
                    fallback_status = fallback.get('status', 0)
                    fallback_data = fallback.get('data', {}) if isinstance(fallback, dict) else {}

                    if fallback_status == 200 and fallback_data.get('code', 0) == 0:
                        download_url = BilibiliAPI._extract_download_url_from_playurl(fallback_data)
                        if download_url:
                            return {
                                'url': download_url,
                                'title': video_info.get('title'),
                                'duration': video_info.get('duration'),
                                'aid': aid,
                                'cid': cid,
                            }

                raise NonRetryableError("未找到可用的视频下载链接", ErrorType.NO_CONTENT)

            except NonRetryableError as e:
                last_non_retryable = e
                if '412' in str(e) and attempt < max_attempts:
                    wait_time = retry_interval * (2 ** (attempt - 1)) + random.uniform(0, 0.6)
                    logger.warning(
                        "[BilibiliAPI] 下载地址遇到412，第%d/%d次，%.2fs后重试",
                        attempt,
                        max_attempts,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                break
            except (RetryableError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_attempts:
                    wait_time = retry_interval * (2 ** (attempt - 1)) + random.uniform(0, 0.6)
                    logger.warning(
                        "[BilibiliAPI] 下载地址网络异常: %s，第%d/%d次，%.2fs后重试",
                        str(e),
                        attempt,
                        max_attempts,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(f"[BilibiliAPI] 获取下载地址失败（重试{max_attempts}次后）: {e}")
                return None
            except Exception as e:
                logger.error(f"[BilibiliAPI] 获取下载地址失败: {e}")
                return None

        if last_non_retryable:
            raise last_non_retryable
        return None
    
    @staticmethod
    async def download_video(
        url: str,
        max_size_mb: int = 100,
        timeout_sec: int = 300,
        max_attempts: int = None,
        retry_interval: float = None,
    ) -> Optional[str]:
        """下载视频到临时文件（带重试机制）
        
        Args:
            url: 视频下载地址
            max_size_mb: 最大文件大小(MB)
            timeout_sec: 下载超时时间（秒），默认300秒
            max_attempts: 最大重试次数
            retry_interval: 重试间隔（秒）
            
        Returns:
            临时文件路径
            
        Raises:
            NonRetryableError: 不可重试的错误（如文件过大）
        """
        max_attempts = max_attempts or BilibiliAPI.DEFAULT_MAX_ATTEMPTS
        retry_interval = retry_interval or BilibiliAPI.DEFAULT_RETRY_INTERVAL
        
        # 使用插件的临时目录
        videos_temp_dir = get_temp_subdir("videos")
        tmp_filename = f"bili_video_{uuid.uuid4().hex[:8]}.mp4"
        tmp_path = os.path.join(videos_temp_dir, tmp_filename)
        
        max_bytes = max_size_mb * 1024 * 1024
        
        headers = BilibiliAPI._build_headers(include_cookie=False)
        
        logger.debug(f"[BilibiliAPI] 开始下载视频到: {tmp_path}")
        
        async def _download():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as response:
                        if response.status != 200:
                            error_type, retryable = classify_http_error(response.status)
                            
                            if retryable:
                                raise RetryableError(f"下载失败: status={response.status}", error_type)
                            else:
                                raise NonRetryableError(f"下载失败: status={response.status}", error_type)
                        
                        # 检查文件大小
                        content_length = response.headers.get('Content-Length')
                        if content_length and int(content_length) > max_bytes:
                            raise NonRetryableError(
                                f"视频文件过大: {int(content_length)/1024/1024:.2f}MB > {max_size_mb}MB",
                                ErrorType.VIDEO_TOO_LARGE
                            )
                        
                        # 下载视频
                        total_downloaded = 0
                        with open(tmp_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(8192):
                                if not chunk:
                                    break
                                total_downloaded += len(chunk)
                                if total_downloaded > max_bytes:
                                    try:
                                        f.close()
                                        os.remove(tmp_path)
                                    except Exception:
                                        pass
                                    raise NonRetryableError(
                                        f"下载超过大小限制: {total_downloaded/1024/1024:.2f}MB > {max_size_mb}MB",
                                        ErrorType.VIDEO_TOO_LARGE
                                    )
                                f.write(chunk)
                        
                        logger.debug(f"[BilibiliAPI] 视频下载完成: {total_downloaded / 1024 / 1024:.2f}MB")
                        return tmp_path
                        
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 清理可能的部分下载文件
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                raise RetryableError(f"网络错误: {e}", ErrorType.NETWORK_ERROR)
        
        try:
            return await retry_async(
                _download,
                max_attempts=max_attempts,
                interval_sec=retry_interval,
                retryable_exceptions=(RetryableError,),
            )
        except NonRetryableError:
            # 清理临时文件
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise
        except RetryableError as e:
            logger.error(f"[BilibiliAPI] 下载视频失败（重试{max_attempts}次后）: {e}")
            # 清理临时文件
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return None
        except Exception as e:
            logger.error(f"[BilibiliAPI] 下载视频异常: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return None