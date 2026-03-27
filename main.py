"""
AstrBot Platform Parser Plugin
视频解析API插件
"""

import asyncio
import json
import os
import re
import tempfile
from urllib.parse import quote, urlparse

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Video
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 版本号获取函数
def get_version():
    """从 metadata.yaml 中读取版本号"""
    metadata_path = os.path.join(os.path.dirname(__file__), 'metadata.yaml')
    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            content = f.read()
            for line in content.split('\n'):
                if line.startswith('version:'):
                    return line.split(':')[1].strip().strip('"')
    except Exception:
        pass
    return "1.0.0"

# 下载视频时使用的 headers（模拟浏览器，避免 TikTok 等平台 403）
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}

@register("platform_hx", "hx", "解析部分平台API的插件", get_version())
class PlatformParser(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
        self.api_base_url = config.get("api_base_url", "http://localhost:10010")
        logger.info(f"PlatformParser 插件初始化完成，版本: {get_version()}")

    async def initialize(self):
        """插件异步初始化方法"""
        logger.info("PlatformParser 插件启动完成")

    def _match_supported_url(self, message_str: str) -> str | None:
        """从消息中提取并验证支持的视频链接"""
        supported_domains = {
            'tiktok.com', 'douyin.com', 'youtube.com', 'youtu.be',
            'vimeo.com', 'instagram.com', 'twitter.com', 'x.com',
            'bilibili.com', 'b23.tv',
        }
        url_pattern = r'https?://[^\s]+'
        urls = re.findall(url_pattern, message_str)

        for url in urls:
            try:
                parsed = urlparse(url)
                netloc = parsed.netloc.lower().removeprefix('www.')
                if any(netloc == d or netloc.endswith('.' + d) for d in supported_domains):
                    logger.info(f"[auto_parse_video] 检测到视频链接: {url}")
                    return url
            except Exception:
                continue
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_parse_video(self, event: AstrMessageEvent):
        """自动检测消息中的视频链接并解析"""
        message_str = event.message_str.strip()

        video_url = self._match_supported_url(message_str)
        if not video_url:
            return

        # 验证URL格式
        try:
            parsed_url = urlparse(video_url)
            if not all([parsed_url.scheme, parsed_url.netloc]):
                raise ValueError("Invalid URL")
        except ValueError:
            return event.plain_result("❌ 无效的URL格式")

        # 请求解析接口（异步，不阻塞事件循环）
        try:
            logger.info(f"开始解析视频: {video_url}")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_base_url}/parse",
                    json={"url": video_url},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    status = resp.status
                    logger.info(f"API响应状态: {status}")
                    if status != 200:
                        text = await resp.text()
                        logger.error(f"API错误: HTTP {status}, 响应: {text}")
                        return event.plain_result(f"❌ 解析失败：HTTP {status}\n{text}")
                    result = await resp.json()

            logger.info(f"解析结果: {result}")
            title = result.get("title", "Unknown Video")
            download_url = result.get("real_download_url", None)

            # CDN 链接直接下载会 403，改用 API 的 /download 端点
            parsed_video = urlparse(video_url)
            video_netloc = parsed_video.netloc.lower().removeprefix('www.')
            need_api_download = any(
                video_netloc == d or video_netloc.endswith('.' + d)
                for d in ('tiktok.com', 'douyin.com', 'bilibili.com', 'b23.tv')
            )
            if need_api_download:
                download_url = f"{self.api_base_url}/download?url={quote(video_url, safe='')}"
                logger.info(f"使用API下载端点: {download_url}")

            # 尝试下载并发送文件
            file_sent = False
            temp_path = None

            try:
                if download_url:
                    async with aiohttp.ClientSession() as dl_session:
                        async with dl_session.get(
                            download_url,
                            headers=DOWNLOAD_HEADERS,
                            timeout=aiohttp.ClientTimeout(total=180),
                        ) as dl_resp:
                            if dl_resp.status == 200:
                                # 解析文件名
                                filename = "video.mp4"
                                cd = dl_resp.headers.get("content-disposition", "")
                                m = re.search(r'filename="?([^";\s]+)"?', cd)
                                if m:
                                    filename = m.group(1)

                                # 写入临时文件
                                fd, temp_path = tempfile.mkstemp(suffix=os.path.splitext(filename)[1] or '.mp4')
                                with os.fdopen(fd, 'wb') as f:
                                    async for chunk in dl_resp.content.iter_chunked(8192):
                                        f.write(chunk)

                                chain = MessageChain()
                                chain.chain.append(Plain(text=f"📹 {title}"))
                                chain.chain.append(Video.fromFileSystem(temp_path))
                                await event.send(chain)
                                file_sent = True
                            else:
                                logger.warning(f"下载接口返回 {dl_resp.status}, 未发送文件")
            except Exception as e:
                logger.warning(f"下载阶段失败: {e}")
            finally:
                # 清理临时文件
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

            if file_sent:
                return
            else:
                logger.warning(f"无法发送视频文件，仅显示标题: {title}")
                return event.plain_result(f"📹 {title}\n\n⚠️ 视频下载失败，请稍后重试")

        except asyncio.TimeoutError:
            logger.error("API请求超时")
            return event.plain_result("❌ 请求超时，请稍后重试")
        except aiohttp.ClientError as e:
            logger.error(f"连接错误: {str(e)}")
            return event.plain_result("❌ 无法连接到解析服务")
        except Exception as e:
            logger.error(f"解析异常: {str(e)}", exc_info=True)
            return event.plain_result(f"❌ 解析出错：{str(e)}")

    @filter.command("api_status")
    async def api_status_command(self, event: AstrMessageEvent):
        """检查解析API服务状态"""
        try:
            logger.info("检查API服务状态...")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base_url}/openapi.json",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        logger.info("API服务正常")
                        return event.plain_result("✅ 解析API服务正常")
                    else:
                        logger.error(f"API服务异常: HTTP {resp.status}")
                        return event.plain_result(f"⚠️ API服务响应异常：HTTP {resp.status}")
        except Exception as e:
            logger.error(f"API连接失败: {str(e)}")
            return event.plain_result(f"❌ 无法连接到API服务：{str(e)}")

    @filter.command("ping_api")
    async def ping_api_command(self, event: AstrMessageEvent):
        """测试API连接"""
        try:
            logger.info("测试API连接...")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base_url}/",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status in [200, 404]:
                        logger.info("API服务器可达")
                        return event.plain_result("✅ API服务器连接正常")
                    else:
                        logger.error(f"API服务器异常: HTTP {resp.status}")
                        return event.plain_result(f"⚠️ API服务器异常：HTTP {resp.status}")
        except Exception as e:
            logger.error(f"API连接测试失败: {str(e)}")
            return event.plain_result(f"❌ 无法连接到API服务器：{str(e)}")

    @filter.command("help")
    async def help_command(self, event: AstrMessageEvent):
        """显示详细帮助信息"""
        help_text = f"""
🎥 视频解析插件帮助 (v{get_version()})

用法：
• 直接发送视频链接 - 自动解析并发送
  支持：TikTok、抖音、YouTube、Vimeo、Instagram

命令：
• /help - 显示此帮助信息
• /sphe - 快速帮助
• /test - 测试插件状态
• /api_status - 检查API服务状态
• /ping_api - 测试API连接

API地址：{self.api_base_url} (本地服务器)
版本: {get_version()}
        """
        return event.plain_result(help_text.strip())

    @filter.command("sphe")
    async def sphe_command(self, event: AstrMessageEvent):
        """快速显示插件帮助"""
        logger.info("收到 sphe 命令")
        help_text = f"""
🎥 视频解析插件 v{get_version()}

用法：直接发送视频链接，自动解析
支持：TikTok、抖音、YouTube、Vimeo、Instagram

▪️ /help - 详细帮助
▪️ /test - 测试插件
▪️ /api_status - API状态
▪️ /ping_api - 测试连接

📍 API: {self.api_base_url} (本地服务器)
        """
        return event.plain_result(help_text.strip())

    @filter.command("test")
    async def test_command(self, event: AstrMessageEvent):
        """测试插件状态"""
        user_name = event.get_sender_name()
        logger.info(f"收到 test 命令，来自用户: {user_name}")
        return event.plain_result(f"✅ 插件工作正常！\n👋 你好 {user_name}\n📊 当前版本: {get_version()}")

    async def terminate(self):
        """插件销毁方法"""
        logger.info("PlatformParser 插件已停止")
