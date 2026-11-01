"""免费图床上传：catbox / litterbox / uguu / 0x0 / sm.ms / imgbb。"""

from __future__ import annotations

import base64

import aiohttp

USER_AGENT = "Mozilla/5.0 (compatible; AstrBot-Discourse/1.0)"
UPLOAD_TIMEOUT = 60


async def upload_to_bed(
    name: str,
    filename: str,
    data: bytes,
    mime: str,
    smms_token: str = "",
    imgbb_key: str = "",
) -> str:
    """把图片上传到指定图床，返回图片直链；失败抛异常。"""
    name = str(name or "").strip().lower()
    timeout = aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as session:
        if name == "catbox":
            return await _catbox(session, filename, data, mime)
        if name == "litterbox":
            return await _litterbox(session, filename, data, mime)
        if name == "uguu":
            return await _uguu(session, filename, data, mime)
        if name == "0x0":
            return await _0x0(session, filename, data, mime)
        if name == "smms":
            return await _smms(session, filename, data, mime, smms_token)
        if name == "imgbb":
            return await _imgbb(session, data, imgbb_key)
    raise ValueError(f"未知图床：{name}")


async def _catbox(session, filename, data, mime) -> str:
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("fileToUpload", data, filename=filename, content_type=mime)
    async with session.post(
        "https://catbox.moe/user/api.php", data=form
    ) as response:
        text = (await response.text()).strip()
        if response.status == 200 and text.startswith("http"):
            return text
        raise RuntimeError(f"HTTP {response.status}: {text[:120]}")


async def _litterbox(session, filename, data, mime) -> str:
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", "72h")
    form.add_field("fileToUpload", data, filename=filename, content_type=mime)
    async with session.post(
        "https://litterbox.catbox.moe/resources/internals/api.php", data=form
    ) as response:
        text = (await response.text()).strip()
        if response.status == 200 and text.startswith("http"):
            return text
        raise RuntimeError(f"HTTP {response.status}: {text[:120]}")


async def _uguu(session, filename, data, mime) -> str:
    form = aiohttp.FormData()
    form.add_field("files[]", data, filename=filename, content_type=mime)
    async with session.post(
        "https://uguu.se/upload.php?output=text", data=form
    ) as response:
        text = (await response.text()).strip()
        if response.status == 200 and text.startswith("http"):
            return text
        raise RuntimeError(f"HTTP {response.status}: {text[:120]}")


async def _0x0(session, filename, data, mime) -> str:
    form = aiohttp.FormData()
    form.add_field("file", data, filename=filename, content_type=mime)
    async with session.post("https://0x0.st", data=form) as response:
        text = (await response.text()).strip()
        if response.status == 200 and text.startswith("http"):
            return text
        raise RuntimeError(f"HTTP {response.status}: {text[:120]}")


async def _smms(session, filename, data, mime, token: str) -> str:
    token = str(token or "").strip()
    if not token:
        raise ValueError("未配置 sm.ms Token")
    form = aiohttp.FormData()
    form.add_field("smfile", data, filename=filename, content_type=mime)
    async with session.post(
        "https://sm.ms/api/v2/upload",
        data=form,
        headers={"Authorization": token},
    ) as response:
        payload = await response.json(content_type=None)
        if response.status == 200 and isinstance(payload, dict) and payload.get("success"):
            url = ((payload.get("data") or {}).get("url")) or ""
            if url:
                return str(url)
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("errors") or "")
        raise RuntimeError(f"HTTP {response.status}: {message[:120]}")


async def _imgbb(session, data, key: str) -> str:
    key = str(key or "").strip()
    if not key:
        raise ValueError("未配置 imgbb API Key")
    form = aiohttp.FormData()
    form.add_field("image", base64.b64encode(data).decode("ascii"))
    async with session.post(
        f"https://api.imgbb.com/1/upload?key={key}", data=form
    ) as response:
        payload = await response.json(content_type=None)
        if response.status == 200 and isinstance(payload, dict) and payload.get("success", True):
            url = ((payload.get("data") or {}).get("url")) or ""
            if url:
                return str(url)
        message = ""
        if isinstance(payload, dict):
            message = str((payload.get("error") or {}).get("message") or "")
        raise RuntimeError(f"HTTP {response.status}: {message[:120]}")
