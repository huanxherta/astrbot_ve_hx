from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
import uvicorn
import logging
import os
import re
import json
from typing import Any, cast
from urllib.parse import quote
import asyncio
import datetime
from datetime import timedelta
from typing import Dict, Any as AnyT
import httpx

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# 加载配置文件
def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"⚠️ 无法加载配置文件: {e}")
        return {}


CONFIG = load_config()
API_BASE_URL = CONFIG.get("api_base_url", "http://localhost:10010")


class VideoItem(BaseModel):
    url: str


async def _parse_douyin_via_api(url: str) -> dict:
    """
    使用新的抖音 API 解析视频信息
    API 文档: https://dy.0d000721.cv
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # 先获取完整的视频信息
            api_url = f"https://dy.0d000721.cv/?url={quote(url, safe='')}&data"
            logger.info(f"📱 调用抖音API: {api_url}")

            response = await client.get(api_url)
            response.raise_for_status()

            data = response.json()

            if "error" in data:
                error_msg = data.get("error", "未知错误")
                logger.error(f"抖音API错误: {error_msg}")
                raise HTTPException(
                    status_code=400,
                    detail=f"抖音解析失败：{error_msg}。请检查链接是否有效或稍后重试。",
                )

            # 提取视频直链
            video_info = data.get("video", {})
            direct_url = video_info.get("direct_url")

            if not direct_url:
                logger.error("无法获取抖音视频直链")
                # 尝试从响应中获取更多信息
                logger.debug(f"API 完整响应: {data}")
                raise HTTPException(
                    status_code=400,
                    detail="无法获取视频直链。该视频可能已被删除或无法访问。",
                )

            # 提取其他信息
            title = data.get("desc", "抖音视频")
            author_info = data.get("author", {})
            stats = data.get("statistics", {})

            return {
                "title": title,
                "real_download_url": direct_url,
                "platform": "DouYin",
                "uploader": author_info.get("nickname", "未知作者"),
                "duration": None,  # API 文档中没有提供
                "view_count": stats.get("play_count", 0),
                "like_count": stats.get("digg_count", 0),
                "comment_count": stats.get("comment_count", 0),
                "download_via_api": f"{API_BASE_URL}/download?url={quote(url, safe='')}",
                "raw_api_data": data,
            }
    except httpx.HTTPError as e:
        logger.error(f"HTTP错误: {e}")
        raise HTTPException(status_code=503, detail=f"解析服务暂时不可用。请稍后重试。")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"解析抖音视频异常: {e}")
        raise HTTPException(status_code=500, detail=f"解析异常：{str(e)}")


async def _download_douyin_via_api(url: str, download_dir: str) -> str:
    """
    通过 API 下载抖音视频
    """
    try:
        # 先获取视频直链
        video_info = await _parse_douyin_via_api(url)
        direct_url = video_info.get("real_download_url")

        if not direct_url:
            raise HTTPException(status_code=400, detail="无法获取视频直链")

        # 生成文件名
        title = video_info.get("title", "抖音视频").replace("/", "_").replace("\\", "_")
        title = re.sub(r'[<>:"|?*]', "", title)[:50]  # 清理非法字符，限制长度
        file_path = os.path.join(download_dir, f"{title}.mp4")

        # 下载视频
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            logger.info(f"⬇️ 下载抖音视频: {direct_url}")
            response = await client.get(direct_url)
            response.raise_for_status()

            with open(file_path, "wb") as f:
                f.write(response.content)

            logger.info(f"✅ 抖音视频下载完成: {file_path}")
            return file_path
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"抖音视频下载失败: {e}")
        raise HTTPException(status_code=500, detail=f"下载失败: {str(e)}")


def _build_ydl_opts(url: str) -> dict[str, Any]:
    ydl_opts: dict[str, Any] = {
        "format": "best[ext=mp4]/best",
        "quiet": False,
        "no_warnings": False,
        "nocheckcertificate": True,
        "extract_flat": False,
        "ignoreerrors": False,
    }

    # 从配置文件中获取平台配置
    platforms = CONFIG.get("supported_platforms", {})

    if "tiktok.com" in url or "douyin.com" in url:
        logger.info("📱 检测到TikTok/抖音链接")
        ydl_opts["impersonate"] = ImpersonateTarget("chrome")
    elif "youtube.com" in url or "youtu.be" in url:
        logger.info("📺 检测到YouTube链接")
        cookie_path = os.path.join(
            os.path.dirname(__file__), "www.youtube.com_cookies.txt"
        )
        if os.path.exists(cookie_path):
            ydl_opts["cookiefile"] = cookie_path
            logger.info(f"🍪 加载Cookie: {cookie_path}")
        else:
            logger.warning("⚠️ 未找到Cookie文件，匿名解析")
        # YouTube不强制mp4，让yt-dlp自动选择最佳可用格式
        ydl_opts.pop("format", None)
    elif "bilibili.com" in url or "b23.tv" in url:
        logger.info("📺 检测到B站链接")
        # 从配置文件中获取B站配置
        bilibili_config = platforms.get("bilibili", {})
        if bilibili_config.get("enabled", True):
            headers = bilibili_config.get(
                "headers",
                {
                    "Referer": "https://www.bilibili.com",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                },
            )
            ydl_opts["http_headers"] = headers
        # B站不指定具体格式，让yt-dlp自动选择最佳可用格式
        ydl_opts.pop("format", None)
    else:
        logger.info("🌐 其他平台链接")

    return ydl_opts


def _normalize_error_detail(err: Exception, url: str) -> HTTPException:
    detail = str(err) if str(err) else repr(err)
    detail = re.sub(r"\x1b\[[0-9;]*m", "", detail).strip()

    if ("tiktok.com" in url or "douyin.com" in url) and "/hk/notfound" in detail:
        return HTTPException(
            status_code=400,
            detail="TikTok分享链接已失效、被下架或地区不可用，请改用可直接打开的原始视频链接（https://www.tiktok.com/@xxx/video/xxx）",
        )

    return HTTPException(status_code=500, detail=detail)


@app.post("/parse")
async def parse_video(item: VideoItem):
    url = item.url
    logger.info(f"🚀 收到解析任务: {url}")

    try:
        # 检测是否为抖音链接 - 支持多种格式
        is_douyin = any(
            [
                "douyin.com" in url,
                "v.douyin.com" in url,
                "dy.ixigua.com" in url,
                "ixigua.com" in url and "v" in url,
            ]
        )

        if is_douyin:
            logger.info("📱 检测到抖音链接，使用新API解析")
            return await _parse_douyin_via_api(url)

        # 其他平台使用 yt_dlp
        ydl_opts = _build_ydl_opts(url)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            raw_info = ydl.extract_info(url, download=False)

            if not raw_info or not isinstance(raw_info, dict):
                raise HTTPException(
                    status_code=400, detail="无法获取视频信息（info为空）"
                )

            info = cast(dict[str, Any], raw_info)
            title = info.get("title", "Unknown Video")
            real_url = None

            # 从formats中查找最佳链接
            formats = info.get("formats", [])
            if isinstance(formats, list):
                normalized_formats = [f for f in formats if isinstance(f, dict)]
                for fmt in sorted(
                    normalized_formats,
                    key=lambda x: x.get("height", 0) or 0,
                    reverse=True,
                ):
                    fmt_url = fmt.get("url", "")
                    if (
                        fmt_url
                        and fmt.get("vcodec") != "none"
                        and "m3u8" not in str(fmt_url)
                    ):
                        real_url = str(fmt_url)
                        break

            if not real_url:
                real_url = str(info.get("webpage_url") or info.get("url") or "")

            platform = (
                "TikTok"
                if "tiktok.com" in url
                else "YouTube"
                if "youtube.com" in url or "youtu.be" in url
                else "Bilibili"
                if "bilibili.com" in url or "b23.tv" in url
                else "Vimeo"
                if "vimeo.com" in url
                else "Instagram"
                if "instagram.com" in url
                else "Twitter"
                if "twitter.com" in url or "x.com" in url
                else "Unknown"
            )
            download_api_url = (
                f"{API_BASE_URL}/download?url={quote(url, safe='')}"
            )
            logger.info(f"✅ 解析成功: {title}")

            return {
                "title": title,
                "real_download_url": real_url,
                "download_via_api": download_api_url,
                "platform": platform,
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
                "uploader": info.get("uploader"),
            }

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        err = traceback.format_exc()
        logger.error(f"❌ 解析异常: {err}")
        raise _normalize_error_detail(e, url)


@app.get("/download")
async def download_video(url: str):
    logger.info(f"⬇️ 收到下载任务: {url}")
    download_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(download_dir, exist_ok=True)

    try:
        # 检测是否为抖音链接 - 支持多种格式
        is_douyin = any(
            [
                "douyin.com" in url,
                "v.douyin.com" in url,
                "dy.ixigua.com" in url,
                "ixigua.com" in url and "v" in url,
            ]
        )

        if is_douyin:
            logger.info("📱 检测到抖音链接，使用API下载")
            file_path = await _download_douyin_via_api(url, download_dir)
            return FileResponse(
                path=file_path,
                filename=os.path.basename(file_path),
                media_type="application/octet-stream",
            )

        # 其他平台使用 yt_dlp
        ydl_opts = _build_ydl_opts(url)
        ydl_opts.update(
            {
                "outtmpl": os.path.join(download_dir, "%(id)s.%(ext)s"),
                "noplaylist": True,
            }
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise HTTPException(
                    status_code=400, detail="下载失败：无法获取视频信息"
                )

            file_path = ydl.prepare_filename(info)
            if not os.path.exists(file_path):
                raise HTTPException(status_code=500, detail="下载失败：未找到下载文件")

            logger.info(f"✅ 下载完成: {file_path}")
            return FileResponse(
                path=file_path,
                filename=os.path.basename(file_path),
                media_type="application/octet-stream",
            )

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        err = traceback.format_exc()
        logger.error(f"❌ 下载异常: {err}")
        raise _normalize_error_detail(e, url)


@app.delete("/download")
async def delete_video(url: str):
    """删除已缓存的下载文件（仅在 downloads/ 目录内）。

    Args:
        url: 原始视频 URL，用于计算下载文件名并删除对应文件。
    """
    logger.info(f"🗑️ 收到删除请求: {url}")
    download_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(download_dir, exist_ok=True)

    try:
        # 检测是否为抖音链接 - 支持多种格式
        is_douyin = any(
            [
                "douyin.com" in url,
                "v.douyin.com" in url,
                "dy.ixigua.com" in url,
                "ixigua.com" in url and "v" in url,
            ]
        )

        if is_douyin:
            logger.info("📱 处理抖音删除请求")
            # 对于抖音，需要从 API 获取标题来计算文件名
            try:
                video_info = await _parse_douyin_via_api(url)
                title = (
                    video_info.get("title", "抖音视频")
                    .replace("/", "_")
                    .replace("\\", "_")
                )
                title = re.sub(r'[<>:"|?*]', "", title)[:50]
                file_path = os.path.join(download_dir, f"{title}.mp4")
            except Exception:
                logger.warning("无法从 API 获取抖音视频信息，尝试查找匹配文件")
                file_path = None
        else:
            # 其他平台使用 yt_dlp
            ydl_opts = _build_ydl_opts(url)
            ydl_opts.update(
                {
                    "outtmpl": os.path.join(download_dir, "%(id)s.%(ext)s"),
                    "noplaylist": True,
                }
            )

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 只获取信息，计算目标文件名
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise HTTPException(
                        status_code=400, detail="无法获取视频信息，无法删除"
                    )

                file_path = ydl.prepare_filename(info)

        # 验证文件路径安全性
        if file_path:
            real_download_dir = os.path.realpath(download_dir)
            real_file_path = os.path.realpath(file_path)
            if not real_file_path.startswith(real_download_dir + os.sep):
                logger.error(f"尝试删除不在 downloads 目录的文件: {real_file_path}")
                raise HTTPException(status_code=400, detail="拒绝删除非缓存目录文件")

            if os.path.exists(real_file_path):
                try:
                    os.remove(real_file_path)
                    logger.info(f"✅ 已删除缓存文件: {real_file_path}")
                    return {"deleted": True, "path": real_file_path}
                except Exception as e:
                    logger.error(f"删除文件失败: {e}")
                    raise HTTPException(status_code=500, detail=f"删除失败: {e}")
            else:
                logger.info(f"未找到要删除的文件: {real_file_path}")
                raise HTTPException(status_code=404, detail="未找到缓存文件")
        else:
            raise HTTPException(status_code=400, detail="无法确定要删除的文件")

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        err = traceback.format_exc()
        logger.error(f"❌ 删除异常: {err}")
        raise _normalize_error_detail(e, url)


# ----------------
# 下载目录周期清理任务
# ----------------

# 全局下载目录常量（模块级）
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 从配置文件中获取清理配置
cleanup_config = CONFIG.get("cleanup", {})
# 可通过环境变量覆盖
DOWNLOAD_RETENTION_DAYS = int(
    os.getenv("DOWNLOAD_RETENTION_DAYS", str(cleanup_config.get("retention_days", 7)))
)
# 清理间隔（秒），默认每天一次
DOWNLOAD_CLEANUP_INTERVAL = int(
    os.getenv(
        "DOWNLOAD_CLEANUP_INTERVAL",
        str(cleanup_config.get("interval_seconds", 24 * 3600)),
    )
)


def _cleanup_once(retention_days: int = DOWNLOAD_RETENTION_DAYS) -> Dict[str, AnyT]:
    now = datetime.datetime.now()
    cutoff = now - timedelta(days=retention_days)
    removed_files = []

    try:
        for root, _, files in os.walk(DOWNLOAD_DIR):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp))
                except Exception:
                    continue
                if mtime < cutoff:
                    try:
                        os.remove(fp)
                        removed_files.append(fp)
                        logger.info(f"🧹 已删除过期缓存文件: {fp}")
                    except Exception as e:
                        logger.error(f"删除文件失败 {fp}: {e}")
        return {"deleted_count": len(removed_files), "deleted": removed_files}
    except Exception as e:
        logger.error(f"清理异常: {e}")
        return {"deleted_count": 0, "deleted": [], "error": str(e)}


async def _periodic_cleanup_task(
    interval: int = DOWNLOAD_CLEANUP_INTERVAL,
    retention_days: int = DOWNLOAD_RETENTION_DAYS,
):
    logger.info(
        f"启动下载目录周期清理任务: 每 {interval}s 清理一次，保留 {retention_days} 天内的文件"
    )
    while True:
        try:
            res = _cleanup_once(retention_days)
            if res.get("deleted_count", 0) > 0:
                logger.info(f"清理完成，移除 {res['deleted_count']} 个文件")
        except Exception as e:
            logger.error(f"周期清理出错: {e}")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def startup_cleanup_task():
    # 在后台启动周期清理任务
    asyncio.create_task(_periodic_cleanup_task())


@app.post("/cleanup")
async def trigger_cleanup(dry_run: bool = False):
    """手动触发一次清理。默认会删除过期文件；dry_run=True 时仅返回将被删除的列表。"""
    # 返回将要删除或已删除的文件信息
    if dry_run:
        # 模拟：列出但不删除
        now = datetime.datetime.now()
        cutoff = now - timedelta(days=DOWNLOAD_RETENTION_DAYS)
        will_delete = []
        for root, _, files in os.walk(DOWNLOAD_DIR):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp))
                except Exception:
                    continue
                if mtime < cutoff:
                    will_delete.append(fp)
        return {"will_delete_count": len(will_delete), "will_delete": will_delete}

    return _cleanup_once(DOWNLOAD_RETENTION_DAYS)


@app.get("/")
async def root():
    return {
        "message": "视频解析API服务运行中",
        "endpoints": {"parse": "POST /parse", "download": "GET /download?url=..."},
    }


@app.get("/status")
async def status():
    return {"status": "healthy", "service": "video_parser"}


@app.get("/ping")
async def ping():
    return {"ping": "pong"}


if __name__ == "__main__":
    logger.info("🚀 启动视频解析API服务器")
    logger.info(f"📍 访问地址: {API_BASE_URL}")
    logger.info(f"📖 API文档: {API_BASE_URL}/docs")
    uvicorn.run(app, host="0.0.0.0", port=10010, timeout_keep_alive=60)
