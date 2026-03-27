"""
AstrBot Platform Parser Plugin
视频解析API插件
"""

import asyncio
import json
import os
import re
import tempfile
from typing import Any
from urllib.parse import quote, urlparse
import html  # 新增导入html模块

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Video
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

SUPPORTED_DOMAINS = {
    "tiktok.com",
    "douyin.com",
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "bilibili.com",
    "b23.tv",
}

PRIORITY_JSON_KEYS = {
    "qqdocurl",
    "url",
    "jumpurl",
    "jump_url",
    "targeturl",
    "target_url",
    "contenturl",
    "content_url",
    "shareurl",
    "share_url",
}

URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+')
MAX_VIDEO_DURATION_SECONDS = 30 * 60
ENABLE_PARSE_COMMAND = "开启解析"
DISABLE_PARSE_COMMAND = "关闭解析"


# 版本号获取函数
def get_version():
    """从 metadata.yaml 中读取版本号"""
    metadata_path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            content = f.read()
            for line in content.split("\n"):
                if line.startswith("version:"):
                    return line.split(":")[1].strip().strip('"')
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
        self.plugin_dir = os.path.dirname(__file__)
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
        self.api_base_url = config.get("api_base_url", "http://localhost:10010")
        self.group_state_path = os.path.join(self.plugin_dir, "group_parse_state.json")
        self.group_parse_state = self._load_group_parse_state()
        logger.info(f"PlatformParser 插件初始化完成，版本: {get_version()}")

    async def initialize(self):
        """插件异步初始化方法"""
        logger.info("PlatformParser 插件启动完成")

    def _load_group_parse_state(self) -> dict[str, bool]:
        try:
            with open(self.group_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(key): bool(value) for key, value in data.items()}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"加载群解析状态失败: {e}")
        return {}

    def _save_group_parse_state(self):
        try:
            with open(self.group_state_path, "w", encoding="utf-8") as f:
                json.dump(self.group_parse_state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存群解析状态失败: {e}")

    def _extract_group_key(self, event: AstrMessageEvent) -> str | None:
        candidates = [
            getattr(event, "group_id", None),
            getattr(event, "groupId", None),
            getattr(event, "session_id", None),
            getattr(event, "sessionId", None),
            getattr(event, "conversation_id", None),
            getattr(event, "conversationId", None),
        ]

        for candidate in candidates:
            if candidate not in (None, ""):
                return f"group:{candidate}"

        for method_name in ("get_group_id", "get_session_id", "get_conversation_id"):
            method = getattr(event, method_name, None)
            if callable(method):
                try:
                    value = method()
                except Exception:
                    continue
                if value not in (None, ""):
                    return f"group:{value}"

        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            for attr_name in ("group_id", "groupId", "session_id", "sessionId"):
                value = getattr(message_obj, attr_name, None)
                if value not in (None, ""):
                    return f"group:{value}"

        return None

    def _is_group_parsing_enabled(self, event: AstrMessageEvent) -> bool:
        group_key = self._extract_group_key(event)
        if not group_key:
            return True
        return self.group_parse_state.get(group_key, True)

    async def _handle_toggle_command(
        self, event: AstrMessageEvent, message_str: str
    ) -> bool:
        if message_str not in {ENABLE_PARSE_COMMAND, DISABLE_PARSE_COMMAND}:
            return False

        group_key = self._extract_group_key(event)
        if not group_key:
            await self._send_plain_text(event, "⚠️ 该命令仅支持群聊使用")
            return True

        enabled = message_str == ENABLE_PARSE_COMMAND
        self.group_parse_state[group_key] = enabled
        self._save_group_parse_state()
        await self._send_plain_text(
            event,
            "✅ 本群已开启自动解析" if enabled else "✅ 本群已关闭自动解析",
        )
        return True

    def _normalize_candidate_url(self, value: str) -> str | None:
        candidate = value.strip().strip("\"'()[]{}<>,;")
        candidate = candidate.replace("\\/", "/")
        if candidate.startswith("http:\\/") or candidate.startswith("https:\\/"):
            candidate = candidate.replace("\\/", "/")

        try:
            parsed = urlparse(candidate)
            netloc = parsed.netloc.lower().removeprefix("www.")
            if parsed.scheme in {"http", "https"} and any(
                netloc == d or netloc.endswith("." + d) for d in SUPPORTED_DOMAINS
            ):
                return candidate
        except Exception:
            return None
        return None

    def _collect_supported_urls_from_text(self, text: str) -> list[str]:
        matches: list[str] = []
        for candidate in URL_PATTERN.findall(text):
            normalized = self._normalize_candidate_url(candidate)
            if normalized:
                matches.append(normalized)
        return matches

    def _extract_json_payloads(self, text: str) -> list[Any]:
        payloads: list[Any] = []
        decoder = json.JSONDecoder()
        index = 0

        while index < len(text):
            if text[index] not in "{[":
                index += 1
                continue
            try:
                payload, end = decoder.raw_decode(text, index)
                payloads.append(payload)
                index = end
            except json.JSONDecodeError:
                index += 1

        return payloads

    def _collect_supported_urls_from_payload(self, payload: Any) -> list[str]:
        prioritized: list[str] = []
        discovered: list[str] = []
        visited: set[int] = set()

        def visit(value: Any, key: str | None = None):
            if isinstance(value, (dict, list)):
                obj_id = id(value)
                if obj_id in visited:
                    return
                visited.add(obj_id)

            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    visit(child_value, str(child_key).lower())
                return

            if isinstance(value, list):
                for item in value:
                    visit(item, key)
                return

            if not isinstance(value, str):
                return

            stripped = value.strip()
            if not stripped:
                return

            normalized = self._normalize_candidate_url(stripped)
            if normalized:
                if key in PRIORITY_JSON_KEYS:
                    prioritized.append(normalized)
                else:
                    discovered.append(normalized)

            for candidate in self._collect_supported_urls_from_text(stripped):
                if key in PRIORITY_JSON_KEYS:
                    prioritized.append(candidate)
                else:
                    discovered.append(candidate)

            if stripped[:1] in "{[":
                try:
                    visit(json.loads(stripped), key)
                except Exception:
                    pass

        visit(payload)
        return prioritized + discovered

    def _extract_supported_urls(self, message_str: str) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def add(url: str):
            if url not in seen:
                seen.add(url)
                ordered.append(url)

        for candidate in self._collect_supported_urls_from_text(message_str):
            add(candidate)

        unescaped_message = message_str.replace("\\/", "/")
        if unescaped_message != message_str:
            for candidate in self._collect_supported_urls_from_text(unescaped_message):
                add(candidate)

        for payload in self._extract_json_payloads(message_str):
            for candidate in self._collect_supported_urls_from_payload(payload):
                add(candidate)

        return ordered

    async def _parse_and_download_video(
        self, video_url: str, semaphore: asyncio.Semaphore
    ) -> dict[str, Any]:
        async with semaphore:
            logger.info(f"开始解析视频: {video_url}")
            temp_path = None

            try:
                try:
                    parsed_url = urlparse(video_url)
                    if not all([parsed_url.scheme, parsed_url.netloc]):
                        raise ValueError("Invalid URL")
                except ValueError:
                    return {"url": video_url, "error": "❌ 无效的URL格式"}

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.api_base_url}/parse",
                        json={"url": video_url},
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=180),
                    ) as resp:
                        status = resp.status
                        logger.info(f"API响应状态: {status} ({video_url})")
                        if status != 200:
                            text = await resp.text()
                            logger.error(
                                f"API错误: HTTP {status}, url={video_url}, 响应: {text}"
                            )
                            return {
                                "url": video_url,
                                "error": f"❌ 解析失败\n链接: {video_url}\nHTTP {status}\n{text}",
                            }
                        result = await resp.json()

                logger.info(f"解析结果({video_url}): {result}")
                title = result.get("title", "Unknown Video")
                download_url = result.get("real_download_url")
                duration = result.get("duration")

                if (
                    isinstance(duration, (int, float))
                    and duration > MAX_VIDEO_DURATION_SECONDS
                ):
                    minutes = round(float(duration) / 60, 1)
                    return {
                        "url": video_url,
                        "title": title,
                        "error": (
                            f"📹 {title}\n\n⚠️ 视频时长超过限制\n"
                            f"当前时长: {minutes} 分钟\n最大允许: 30 分钟\n链接: {video_url}"
                        ),
                    }

                parsed_video = urlparse(video_url)
                video_netloc = parsed_video.netloc.lower().removeprefix("www.")
                need_api_download = any(
                    video_netloc == d or video_netloc.endswith("." + d)
                    for d in ("tiktok.com", "douyin.com", "bilibili.com", "b23.tv")
                )
                if need_api_download:
                    download_url = (
                        f"{self.api_base_url}/download?url={quote(video_url, safe='')}"
                    )
                    logger.info(f"使用API下载端点: {download_url}")

                if not download_url:
                    return {
                        "url": video_url,
                        "title": title,
                        "error": f"📹 {title}\n\n⚠️ 未获取到可下载地址\n链接: {video_url}",
                    }

                async with aiohttp.ClientSession() as dl_session:
                    async with dl_session.get(
                        download_url,
                        headers=DOWNLOAD_HEADERS,
                        timeout=aiohttp.ClientTimeout(total=180),
                    ) as dl_resp:
                        if dl_resp.status != 200:
                            logger.warning(
                                f"下载接口返回 {dl_resp.status}, url={video_url}"
                            )
                            return {
                                "url": video_url,
                                "title": title,
                                "error": f"📹 {title}\n\n⚠️ 视频下载失败\n链接: {video_url}\nHTTP {dl_resp.status}",
                            }

                        filename = "video.mp4"
                        cd = dl_resp.headers.get("content-disposition", "")
                        match = re.search(r'filename="?([^";\s]+)"?', cd)
                        if match:
                            filename = match.group(1)

                        fd, temp_path = tempfile.mkstemp(
                            suffix=os.path.splitext(filename)[1] or ".mp4"
                        )
                        with os.fdopen(fd, "wb") as f:
                            async for chunk in dl_resp.content.iter_chunked(8192):
                                f.write(chunk)

                return {"url": video_url, "title": title, "temp_path": temp_path}

            except asyncio.TimeoutError:
                logger.error(f"API请求超时: {video_url}")
                return {"url": video_url, "error": f"❌ 请求超时\n链接: {video_url}"}
            except aiohttp.ClientError as e:
                logger.error(f"连接错误: {video_url}, {str(e)}")
                return {
                    "url": video_url,
                    "error": f"❌ 无法连接到解析服务\n链接: {video_url}",
                }
            except Exception as e:
                logger.error(f"解析异常: {video_url}, {str(e)}", exc_info=True)
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                return {
                    "url": video_url,
                    "error": f"❌ 解析出错\n链接: {video_url}\n{str(e)}",
                }

    async def _send_plain_text(self, event: AstrMessageEvent, text: str):
        chain = MessageChain()
        chain.chain.append(Plain(text=text))
        await event.send(chain)

    def _build_message_text_for_parsing(self, event: AstrMessageEvent) -> str:
        message_str = (getattr(event, "message_str", "") or "").strip()
        message = getattr(event, "message", None)
        if not message:
            return message_str
        for comp in message:
            comp_type = getattr(comp, "type", None)
            comp_data = getattr(comp, "data", None)
            if comp_type not in ("json", "Json") or not comp_data:
                continue
            raw = None
            if isinstance(comp_data, dict):
                raw = comp_data.get("data") or comp_data.get("content") or str(comp_data)
            elif comp_data is not None:
                raw = str(comp_data)
            if not isinstance(raw, str) or ("{" not in raw and "[" not in raw):
                continue
            # ===
