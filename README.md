# AstrBot 视频解析插件（Platform Parser）

这是一个针对 **AstrBot** 的视频解析插件，包含两个部分：

1. **AstrBot 插件** (`main.py`)：定义命令与逻辑，调用本地解析 API 服务。
2. **解析服务器** (`api.py`)：基于 FastAPI + yt_dlp，提供 `/parse` 和 `/download` 接口。

## 功能

- 支持 TikTok、抖音、YouTube、Vimeo、Instagram、Twitter/X 以及 B站（哔哩哔哩）等常见平台视频链接解析。
- 自动模糊匹配任何消息中的链接，用户无需前缀命令。
- 支持解析 QQ 小程序/分享卡片中的 JSON 负载，可从 `qqdocurl`、`url` 等字段提取真实视频链接。
- 单条消息内包含多个视频链接时，会逐个解析并依次发送结果。
- 提供可直接下载的视频真实地址或通过 API 下载。
- 插件自动读取并管理版本号。

## 安装与部署

1. 克隆仓库到本地：
   ```bash
   git clone https://github.com/huanxherta/astrbot_platform_hx.git
   cd astrbot_platform_hx
   ```
2. 安装 Python 依赖：
   ```bash
   pip install -r requirements.txt
   pip install fastapi uvicorn yt-dlp
   ```
3. 在插件目录中运行解析服务（或使用进程管理器）：
   ```bash
   python api.py
   ```
   服务默认监听 `http://localhost:10010`。

4. 将本插件整个目录放到 AstrBot 的 `data/plugins/` 下，重启 AstrBot。

## AstrBot 使用方式

- 直接发送视频链接，插件会自动识别并解析。
- 直接发送 QQ 小程序卡片或分享 JSON，插件会自动提取其中的目标视频链接。
- 单条消息内如果有多个链接，会逐个解析并依次发送。

解析结果示例：

> ⚠️ 对于 YouTube 链接，插件会将解析超时设置为 **40 秒**，以应对较慢的响应。

*原有的 `/download` 命令已移除，解析结果中包含 `download_via_api` 字段，可直接访问下载接口。

解析命令会自动尝试下载视频并将文件通过 AstrBot 的消息链发送给用户。
对于 OneBot/aiocqhttp 等平台，插件会在后台构造一个 `File` 组件并调用 `event.send()`，
由框架负责在下发时生成 CQ 码或其它平台格式，因此不会再出现 `str` 对象错误。
解析结果本身仍以纯文本消息返回，文件则作为上一条消息单独发送。

如果一条消息中包含多个普通链接，或包含 QQ 小程序卡片/JSON 分享内容中的多个目标链接，插件会并发解析并按识别顺序逐个发送。


```
✅ 解析成功！
```json
{
  "title": "Example Video",
  "real_download_url": "https://....mp4",
  "download_via_api": "http://localhost:10010/download?url=...",
  "platform": "YouTube",
  "duration": 123,
  "view_count": 45678,
  "uploader": "ChannelName"
}
```

## API 文档

- `POST /parse`：接收 JSON `{ "url": "..." }`，返回视频信息。
- `GET /download?url=...`：下载视频文件并返回。
- 健康检查：`GET /`, `/status`, `/ping`。

可以通过 `http://localhost:10010/docs` 查看 Swagger UI。

## 配置

- `www.youtube.com_cookies.txt`：可选的 YouTube cookie，用于解析需要登录的视频。
- `metadata.yaml`：插件元数据，`version` 字段由插件加载时自动更新。

## 其他说明

- 默认解析/下载请求超时时间已扩展至 180 秒，以便处理较慢或大体积链接（如 Twitter/X）。
- 更新日志请参见 `CHANGELOG.md` 文件。

## 许可证

见项目根目录中的 `LICENSE` 文件。
