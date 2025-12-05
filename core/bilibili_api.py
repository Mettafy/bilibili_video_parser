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
import asyncio
import uuid
from urllib.parse import urlparse, parse_qs
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
    
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    
    # 默认重试配置
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_RETRY_INTERVAL = 2.0
    
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
        headers = {
            'User-Agent': BilibiliAPI.USER_AGENT,
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # 不自动跟随重定向，手动获取Location
                async with session.get(short_url, headers=headers, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location', '')
                        
                        # 从重定向URL中提取分P号
                        page = BilibiliAPI.extract_page_from_url(location)
                        
                        # 从重定向URL中提取视频ID
                        bv_match = re.search(r'BV[a-zA-Z0-9]{10}', location, re.IGNORECASE)
                        if bv_match:
                            video_id = bv_match.group(0)
                            return (video_id, page)
                        
                        av_match = re.search(r'av(\d+)', location, re.IGNORECASE)
                        if av_match:
                            video_id = f"av{av_match.group(1)}"
                            return (video_id, page)
                        
                        logger.warning(f"[BilibiliAPI] 短链接重定向URL中未找到视频ID: {location}")
                    else:
                        logger.warning(f"[BilibiliAPI] 短链接请求未重定向: status={response.status}")
            
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
        
        headers = {
            'User-Agent': BilibiliAPI.USER_AGENT,
            'Referer': 'https://www.bilibili.com/'
        }
        
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
        headers = {
            'User-Agent': BilibiliAPI.USER_AGENT,
            'Referer': 'https://www.bilibili.com/'
        }
        
        if sessdata:
            headers['Cookie'] = f'SESSDATA={sessdata}'
        
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
        headers = {
            'User-Agent': BilibiliAPI.USER_AGENT,
            'Referer': 'https://www.bilibili.com/'
        }
        
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
        
        Args:
            video_id: 视频ID (BV号或AV号)
            sessdata: B站SESSDATA Cookie
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
        
        # 先获取视频基本信息（已有重试机制）
        video_info = await BilibiliAPI.get_video_info(video_id, sessdata, page, max_attempts, retry_interval)
        if not video_info:
            return None
        
        aid = video_info.get('aid')
        cid = video_info.get('cid')
        
        if not aid or not cid:
            logger.error("[BilibiliAPI] 无法获取视频aid或cid")
            return None
        
        # 获取视频流地址
        url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn=64&fnval=0&fourk=1"
        headers = {
            'User-Agent': BilibiliAPI.USER_AGENT,
            'Referer': 'https://www.bilibili.com/'
        }
        
        if sessdata:
            headers['Cookie'] = f'SESSDATA={sessdata}'
        
        logger.debug(f"[BilibiliAPI] 获取下载地址: aid={aid}, cid={cid}")
        
        async def _fetch():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            data = await response.json()
                            code = data.get('code', 0)
                            
                            if code == 0:
                                durl = data.get('data', {}).get('durl', [])
                                if durl:
                                    download_url = durl[0].get('url')
                                    if download_url:
                                            return {
                                            'url': download_url,
                                            'title': video_info.get('title'),
                                            'duration': video_info.get('duration'),
                                            'aid': aid,
                                            'cid': cid
                                        }
                            else:
                                message = data.get('message', '未知错误')
                                error_type, retryable = classify_bilibili_error(code, message)
                                
                                if retryable:
                                    raise RetryableError(f"B站API错误: code={code}, message={message}", error_type)
                                else:
                                    raise NonRetryableError(f"B站API错误: code={code}, message={message}", error_type)
                        else:
                            error_type, retryable = classify_http_error(response.status)
                            
                            if retryable:
                                raise RetryableError(f"HTTP请求失败: status={response.status}", error_type)
                            else:
                                raise NonRetryableError(f"HTTP请求失败: status={response.status}", error_type)
                
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
        except NonRetryableError:
            raise
        except RetryableError as e:
            logger.error(f"[BilibiliAPI] 获取下载地址失败（重试{max_attempts}次后）: {e}")
            return None
        except Exception as e:
            logger.error(f"[BilibiliAPI] 获取下载地址失败: {e}")
            return None
    
    @staticmethod
    async def download_video(
        url: str,
        max_size_mb: int = 100,
        max_attempts: int = None,
        retry_interval: float = None,
    ) -> Optional[str]:
        """下载视频到临时文件（带重试机制）
        
        Args:
            url: 视频下载地址
            max_size_mb: 最大文件大小(MB)
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
        
        headers = {
            'User-Agent': BilibiliAPI.USER_AGENT,
            'Referer': 'https://www.bilibili.com/'
        }
        
        logger.debug(f"[BilibiliAPI] 开始下载视频到: {tmp_path}")
        
        async def _download():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as response:
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