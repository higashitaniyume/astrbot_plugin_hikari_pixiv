# astrbot_plugin_pixiv

Pixiv 插画解析插件：识别 `pixiv.net/artworks/ID` 与 `pixiv.net/i/ID` 链接，自动下载并发送作品图片。

## 用法

发送 Pixiv 作品链接（私聊或群聊）即可自动解析：

```
https://www.pixiv.net/artworks/12345678
https://www.pixiv.net/i/12345678
https://www.pixiv.net/en/artworks/12345678
```

回复内容：作品信息（标题/作者/PID/标签/R-18·AI 标记）+ 图片（逐张发送，多图作品默认最多 6 张）。

## 配置

- **必填**：`cookie` — 登录 pixiv.net 后从浏览器复制的 Cookie（至少需要 `PHPSESSID`），用于访问 Web Ajax API
- `proxy`：代理地址（Pixiv 需要科学上网时配置，可选）
- `max_send`：单次最多发送的图片数（默认 6）
- `max_file_mb`：单张大小上限，原图超限自动降级到压缩图（默认 25MB）
- `allow_r18`：是否允许发送 R-18 作品（默认 false）
- `blocked_groups`：不自动解析的群号列表

## 依赖

- `httpx>=0.27.0`（在 requirements.txt 中声明）

## 协议

AGPL-3.0-or-later
