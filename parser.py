"""
Pixiv 作品信息解析模块。

负责：
1. URL 正则匹配（仅 artworks / i）
2. 调用 Pixiv Web Ajax API 获取作品元信息与多页图片 URL
3. 数据结构定义

移植自 HIKARI BOT NEO 的 pixiv_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from astrbot.api import logger

# =========================
# 正则
# =========================

# 仅匹配 artworks 和 i URL（支持 /en/ 前缀）
PIXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?pixiv\.net"
    r"/(?:en/)?"
    r"(?:artworks|i)/(?P<id>\d{5,12})",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.7103.48 Safari/537.36"
)

PIXIV_ACCEPT_LANGUAGE = (
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja-JP;q=0.6,ja;q=0.5"
)


# =========================
# 数据结构
# =========================


@dataclass
class PixivPage:
    """Pixiv 作品的单页图片信息。"""

    index: int
    original_url: str
    regular_url: str
    thumb_url: str
    width: int
    height: int


@dataclass
class PixivArtwork:
    """Pixiv 作品完整信息。"""

    illust_id: str
    title: str
    user_name: str
    user_id: str
    page_count: int
    x_restrict: int
    ai_type: int
    sanity_level: int
    tags: list[str] = field(default_factory=list)
    pages: list[PixivPage] = field(default_factory=list)

    @property
    def is_r18(self) -> bool:
        """x_restrict: 0 普通，1 R-18，2 R-18G"""
        return self.x_restrict in (1, 2)


# =========================
# URL 提取
# =========================


def extract_pixiv_ids(text: str) -> list[str]:
    """从文本中提取所有 Pixiv 作品 ID（去重，保持顺序）。"""
    ids: list[str] = []
    seen: set[str] = set()
    for match in PIXIV_URL_RE.finditer(text):
        illust_id = match.group("id")
        if illust_id and illust_id not in seen:
            seen.add(illust_id)
            ids.append(illust_id)
    return ids


# =========================
# HTTP 客户端
# =========================


def _build_headers(illust_id: Optional[str] = None, cookie: str = "") -> dict[str, str]:
    """构建 Pixiv 请求头。"""
    headers = {
        "accept-language": PIXIV_ACCEPT_LANGUAGE,
        "user-agent": USER_AGENT,
    }
    if cookie:
        headers["cookie"] = cookie.strip()
    if illust_id:
        headers["referer"] = f"https://www.pixiv.net/artworks/{illust_id}"
    else:
        headers["referer"] = "https://www.pixiv.net/"
    return headers


def _get_http_client(
    illust_id: Optional[str] = None,
    cookie: str = "",
    proxy: str = "",
) -> httpx.AsyncClient:
    """创建 httpx 异步客户端。"""
    proxy_url = proxy.strip() or None
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=20.0),
        headers=_build_headers(illust_id, cookie),
        proxy=proxy_url,
        follow_redirects=True,
    )


def _ensure_json(resp: httpx.Response, stage: str, illust_id: str) -> dict:
    """确保 Pixiv Ajax 返回的是 JSON，而不是 HTML（被 Cloudflare 拦截等）。"""
    content_type = resp.headers.get("content-type", "")
    body_preview = resp.text[:300]

    if "text/html" in content_type.lower() or resp.text.lstrip().lower().startswith("<!doctype html"):
        if "just a moment" in resp.text.lower():
            raise RuntimeError("Pixiv Web Ajax 被 Cloudflare 拦截，当前 Cookie 方案不可用")
        raise RuntimeError(f"Pixiv Web Ajax 返回 HTML，无法按 JSON 解析：{body_preview!r}")

    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Pixiv Web Ajax JSON 解析失败：{e} body={body_preview!r}") from e


async def fetch_artwork(illust_id: str, cookie: str, proxy: str = "") -> PixivArtwork:
    """获取 Pixiv 作品元信息 + 多页图片 URL。

    Raises:
        RuntimeError: 未配置 Cookie / API 返回错误 / Cloudflare 拦截
        httpx.HTTPStatusError: HTTP 状态错误
    """
    if not cookie:
        raise RuntimeError("未配置 Pixiv Cookie，无法解析。请在插件配置中配置 cookie。")

    logger.info(f"[Pixiv] 开始获取作品信息 → pid={illust_id}")
    t_start = time.time()

    async with _get_http_client(illust_id, cookie, proxy) as client:
        # —— 作品元信息 ——
        info_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
        info_resp = await client.get(info_url)
        info_resp.raise_for_status()
        info_json = _ensure_json(info_resp, "元信息", illust_id)
        if info_json.get("error"):
            err_msg = info_json.get("message") or "unknown error"
            raise RuntimeError(f"Pixiv 返回错误：{err_msg}")

        # —— 多页图片 URL ——
        pages_url = f"https://www.pixiv.net/ajax/illust/{illust_id}/pages?lang=zh"
        pages_resp = await client.get(pages_url)
        pages_resp.raise_for_status()
        pages_json = _ensure_json(pages_resp, "pages", illust_id)
        if pages_json.get("error"):
            err_msg = pages_json.get("message") or "unknown error"
            raise RuntimeError(f"Pixiv pages 返回错误：{err_msg}")

    body = info_json.get("body") or {}

    # 解析图片列表
    raw_pages = pages_json.get("body") or []
    pages: list[PixivPage] = []
    for index, item in enumerate(raw_pages):
        if not isinstance(item, dict):
            continue
        urls = item.get("urls") or {}
        original = urls.get("original")
        regular = urls.get("regular") or urls.get("small") or original
        thumb = urls.get("thumb_mini") or urls.get("small") or regular
        if not original:
            logger.warning(f"[Pixiv] pid={illust_id} page#{index} 缺少 original URL，跳过")
            continue
        pages.append(PixivPage(
            index=index,
            original_url=original,
            regular_url=regular,
            thumb_url=thumb,
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
        ))

    # 标签处理
    tags_raw = body.get("tags", {}).get("tags", [])
    tags: list[str] = []
    for item in tags_raw:
        tag_name = item.get("tag")
        if not tag_name:
            continue
        if tag_name == "R-18":
            tag_name = "R18"
        translation = item.get("translation") or {}
        if translation.get("en"):
            tag_name = translation["en"]
        tags.append(str(tag_name).replace(" ", "_"))

    artwork = PixivArtwork(
        illust_id=illust_id,
        title=str(body.get("illustTitle") or body.get("title") or "Untitled"),
        user_name=str(body.get("userName") or "Unknown"),
        user_id=str(body.get("userId") or ""),
        page_count=int(body.get("pageCount") or len(pages) or 1),
        x_restrict=int(body.get("xRestrict") or 0),
        ai_type=int(body.get("aiType") or 0),
        sanity_level=int(body.get("sl") or 0),
        tags=tags,
        pages=pages,
    )

    elapsed = time.time() - t_start
    logger.info(
        f"[Pixiv] 作品信息获取完成 → pid={illust_id} 「{artwork.title}」by {artwork.user_name} "
        f"共 {artwork.page_count} 页 / {len(pages)} 张图片 ({elapsed:.2f}s)"
    )
    return artwork
