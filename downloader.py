"""
Pixiv 图片下载模块。

负责：
1. 下载 Pixiv 图片到本地缓存
2. 优先下载 original，超限则降级到 regular
3. SHA256 哈希缓存，命中跳过下载

移植自 HIKARI BOT NEO 的 pixiv_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

from astrbot.api import logger

try:
    from .parser import PixivPage, _get_http_client
except ImportError:
    from parser import PixivPage, _get_http_client


class DownloadTooLargeError(RuntimeError):
    """下载内容超过允许大小。"""


def get_suffix_from_url(url: str) -> str:
    """从 URL 推断文件后缀。"""
    path = url.split("?", 1)[0]
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    return ".jpg"


def cache_path_for_url(url: str, cache_dir: str) -> Path:
    """根据 URL 生成缓存文件路径（SHA256 哈希）。"""
    suffix = get_suffix_from_url(url)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{digest}{suffix}"


async def download_image(
    url: str,
    illust_id: str,
    cookie: str,
    proxy: str = "",
    cache_dir: str = "",
    max_bytes: int | None = None,
) -> Path:
    """下载单张图片到本地缓存（命中缓存直接返回）。"""
    path = cache_path_for_url(url, cache_dir)

    # 缓存命中
    if path.exists() and path.stat().st_size > 0:
        logger.debug(f"[Pixiv] 缓存命中 pid={illust_id} → {path.name}")
        return path

    logger.info(f"[Pixiv] 下载图片 pid={illust_id} → {url[:100]}...")
    t_start = time.time()

    async with _get_http_client(illust_id, cookie, proxy) as client:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".part")
        tmp_path.unlink(missing_ok=True)
        written = 0
        try:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"下载到的不是图片：{content_type}")

                content_length = resp.headers.get("content-length")
                content_length_bytes = int(content_length) if content_length and content_length.isdigit() else 0
                if max_bytes is not None and content_length_bytes > max_bytes:
                    raise DownloadTooLargeError(
                        f"图片超过大小限制：{content_length_bytes / 1024 / 1024:.1f}MB"
                    )

                with tmp_path.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes is not None and written > max_bytes:
                            raise DownloadTooLargeError(
                                f"图片超过大小限制：{written / 1024 / 1024:.1f}MB"
                            )
                        f.write(chunk)
            tmp_path.replace(path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    elapsed = time.time() - t_start
    logger.info(
        f"[Pixiv] 下载完成 pid={illust_id} → {path.name} "
        f"({path.stat().st_size / 1024:.0f}KB, {elapsed:.1f}s)"
    )
    return path


async def download_with_fallback(
    page: PixivPage,
    illust_id: str,
    cookie: str,
    proxy: str = "",
    cache_dir: str = "",
    max_file_mb: int = 25,
) -> tuple[Path, bool]:
    """下载图片，优先 original，超限则降级到 regular。

    Returns:
        (文件路径, 是否为原图)
    """
    max_bytes = max(max_file_mb, 1) * 1024 * 1024

    # 尝试 original
    original_path: Path | None = None
    try:
        original_path = await download_image(
            page.original_url, illust_id, cookie, proxy, cache_dir,
            max_bytes=max_bytes,
        )
    except DownloadTooLargeError as e:
        logger.warning(f"[Pixiv] 原图下载超限，尝试 regular → pid={illust_id} p={page.index}: {e}")

    if original_path is not None and original_path.stat().st_size <= max_bytes:
        return original_path, True

    # original 过大，尝试 regular
    try:
        regular_path = await download_image(
            page.regular_url, illust_id, cookie, proxy, cache_dir,
            max_bytes=max_bytes,
        )
    except DownloadTooLargeError as e:
        raise RuntimeError(f"图片过大，regular 超过 {max_file_mb}MB") from e

    if regular_path.stat().st_size <= max_bytes:
        return regular_path, False

    raise RuntimeError(f"图片过大，original 与 regular 均超过 {max_file_mb}MB")
