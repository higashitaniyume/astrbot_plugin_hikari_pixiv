"""
Pixiv 插画解析插件。

自动识别 pixiv.net/artworks/ID 与 pixiv.net/i/ID 链接：
1. 获取作品信息（标题/作者/标签/R-18/AI 标记）
2. 下载图片（优先原图，超限降级到 regular）
3. 发送作品信息 + 图片

需配置 Pixiv Cookie（至少 PHPSESSID）。

移植自 HIKARI BOT NEO 的 pixiv_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:
    from .downloader import download_with_fallback
except ImportError:
    from downloader import download_with_fallback
try:
    from .parser import PIXIV_URL_RE, PixivArtwork, extract_pixiv_ids, fetch_artwork
except ImportError:
    from parser import PIXIV_URL_RE, PixivArtwork, extract_pixiv_ids, fetch_artwork

# 临时缓存目录
if os.name == "nt":
    _TEMP_ROOT = Path(tempfile.gettempdir()) / "astrbot_pixiv"
else:
    _TEMP_ROOT = Path("/tmp/astrbot_pixiv")


@register("pixiv", "higashitaniyume", "Pixiv 插画解析：识别 pixiv 链接，下载并发送作品图片", "1.0.0")
class PixivPlugin(Star):
    """Pixiv 插画解析插件。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context, config)
        self.config = config

    def _cfg(self) -> dict[str, Any]:
        return self.config or {}

    def _is_blocked_group(self, group_id: str) -> bool:
        """群黑名单检查。"""
        blocked = self._cfg().get("blocked_groups")
        if isinstance(blocked, list):
            return bool(group_id) and str(group_id) in [str(g) for g in blocked]
        return False

    def _build_info_text(self, artwork: PixivArtwork, image_count: int, original_count: int) -> str:
        """构建作品信息文本。"""
        r18_text = ""
        if artwork.x_restrict == 1:
            r18_text = " / R-18"
        elif artwork.x_restrict == 2:
            r18_text = " / R-18G"
        ai_text = " / AI" if artwork.ai_type == 2 else ""

        tags = " ".join(f"#{t}" for t in artwork.tags[:8])
        if tags:
            tags = "\n" + tags

        lines = [
            f"标题：{artwork.title}",
            f"作者：{artwork.user_name}",
            f"PID：{artwork.illust_id}",
            f"页数：{image_count}（原图 {original_count}）{r18_text}{ai_text}",
        ]
        if tags:
            lines.append(tags)
        return "\n".join(lines)

    @filter.regex(PIXIV_URL_RE.pattern)
    async def on_pixiv_link(self, event: AstrMessageEvent, id: str = ""):
        """匹配 pixiv 作品链接并解析"""
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return

        group_id = event.get_group_id()
        if self._is_blocked_group(group_id):
            return

        text = event.message_str or ""
        ids = extract_pixiv_ids(text)
        if not ids:
            return

        cookie = str(cfg.get("cookie") or "")
        if not cookie:
            yield event.plain_result("未配置 Pixiv Cookie（需要 PHPSESSID），无法解析")
            return

        proxy = str(cfg.get("proxy") or "")
        max_send = max(1, int(cfg.get("max_send", 6)))
        max_file_mb = int(cfg.get("max_file_mb", 25))
        allow_r18 = bool(cfg.get("allow_r18", False))
        send_link_info = bool(cfg.get("send_link_info", True))
        cache_dir = str(cfg.get("cache_dir") or _TEMP_ROOT / "images")

        for illust_id in ids[:max_send]:
            async for result in self._process_one(
                event, illust_id, cookie, proxy, cache_dir,
                max_send=max_send, max_file_mb=max_file_mb,
                allow_r18=allow_r18, send_link_info=send_link_info,
            ):
                yield result

    async def _process_one(
        self, event: AstrMessageEvent, illust_id: str, cookie: str, proxy: str,
        cache_dir: str, *, max_send: int, max_file_mb: int,
        allow_r18: bool, send_link_info: bool,
    ):
        """处理单个作品：获取信息 → 下载 → 发送。"""
        # 获取作品信息
        try:
            artwork = await fetch_artwork(illust_id, cookie, proxy)
        except Exception as e:
            logger.warning(f"[Pixiv] 获取作品信息失败 pid={illust_id}: {e}")
            yield event.plain_result(f"获取 PID {illust_id} 失败：{e}")
            return

        # R-18 检查
        if artwork.is_r18 and not allow_r18:
            logger.info(f"[Pixiv] R-18 作品被拦截 → pid={illust_id}")
            yield event.plain_result(f"该作品为 R-18，未开启 allow_r18，已拦截")
            return

        # 选择要发送的页面
        selected = artwork.pages[:max_send]
        if not selected:
            yield event.plain_result(f"PID {illust_id} 没有可发送的图片")
            return

        # 下载图片
        image_paths: list[Path] = []
        original_count = 0
        download_errors = 0
        for page in selected:
            try:
                path, is_original = await download_with_fallback(
                    page, illust_id, cookie, proxy, cache_dir, max_file_mb,
                )
                image_paths.append(path)
                if is_original:
                    original_count += 1
                await asyncio.sleep(0.2)  # 避免下载过快
            except Exception as e:
                download_errors += 1
                logger.exception(f"[Pixiv] 图片下载失败 → pid={illust_id} p={page.index}: {e}")

        if not image_paths:
            yield event.plain_result(f"PID {illust_id} 所有图片下载失败")
            return

        # 发送作品信息
        if send_link_info:
            yield event.plain_result(
                self._build_info_text(artwork, len(image_paths), original_count)
            )

        # 逐张发送图片
        for i, path in enumerate(image_paths):
            try:
                yield event.image_result(str(path))
            except Exception as e:
                logger.warning(f"[Pixiv] 图片发送失败 pid={illust_id} p={i}: {e}")
            finally:
                _try_cleanup(path)
            await asyncio.sleep(0.5)

        note = f"（{download_errors} 张下载失败）" if download_errors else ""
        logger.info(
            f"[Pixiv] 作品发送完成 → pid={illust_id} "
            f"发送 {len(image_paths)} 张 (原图 {original_count}){note}"
        )


def _try_cleanup(path: Path) -> None:
    """删除已发送的临时文件（静默忽略错误）。"""
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.warning(f"[Pixiv] 清理临时文件失败: {path} ({e})")
