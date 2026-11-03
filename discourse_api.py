"""Discourse HTTP 客户端（基于 aiohttp）。

读接口匿名即可访问；写接口需要用户本人授权的 User API Key。
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urlparse

import aiohttp

USER_AGENT = "AstrBot-Discourse-Forum/1.0"


class DiscourseError(Exception):
    """Discourse 请求失败。status 为 None 表示网络层错误。

    retry_after：429 时论坛建议的等待秒数，用于退避轮询。
    """

    def __init__(
        self,
        status: int | None,
        message: str,
        payload=None,
        retry_after: float = 0.0,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload
        self.retry_after = retry_after


class DiscourseAPI:
    def __init__(
        self,
        site_url: str,
        api_base: str = "",
        host_header: str = "",
        timeout: int = 30,
    ):
        self.site_url = site_url.rstrip("/")
        self.api_base = (api_base or self.site_url).rstrip("/")
        self.host_header = host_header.strip()
        self.timeout = max(5, int(timeout))
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(limit=20),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self, credential: dict | None) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if credential:
            api_key = str(credential.get("api_key", "")).strip()
            if api_key:
                # 不发送 User-Api-Client-Id：Discourse 收到和 Key 不匹配的客户端 ID 时，
                # 会为每个新 ID 新建一条 UserApiKeyClient 记录（连接 ID 越涨越多）。
                headers["User-Api-Key"] = api_key
        if self.host_header:
            base_host = urlparse(self.api_base).netloc
            if base_host != self.host_header:
                headers["Host"] = self.host_header
        return headers

    async def request(
        self,
        method: str,
        path: str,
        credential: dict | None = None,
        params=None,
        json_data: dict | None = None,
    ) -> dict:
        session = await self._get_session()
        url = f"{self.api_base}{path}"
        try:
            async with session.request(
                method,
                url,
                headers=self._headers(credential),
                params=params,
                json=json_data,
            ) as response:
                text = await response.text()
                payload = None
                try:
                    payload = await response.json(content_type=None)
                except Exception:  # noqa: BLE001
                    payload = {"text": text[:300]}
                if response.status < 200 or response.status >= 300:
                    raise DiscourseError(
                        response.status,
                        _error_message(response.status, payload),
                        payload,
                        _retry_after(response.status, response.headers, payload),
                    )
                return payload if isinstance(payload, dict) else {"data": payload}
        except DiscourseError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DiscourseError(None, f"无法连接 Discourse（{self.api_base}）：{exc}") from exc

    # ---- 读取 ----

    async def latest(self, page: int = 0, credential: dict | None = None) -> dict:
        return await self.request(
            "GET",
            "/latest.json",
            credential=credential,
            params={"page": max(0, int(page)), "no_definitions": "true"},
        )

    async def top(
        self, period: str = "weekly", page: int = 0, credential: dict | None = None
    ) -> dict:
        return await self.request(
            "GET",
            "/top.json",
            credential=credential,
            params={"period": period, "page": max(0, int(page))},
        )

    async def category(
        self, category_id: int, page: int = 0, credential: dict | None = None
    ) -> dict:
        return await self.request(
            "GET",
            f"/c/{int(category_id)}.json",
            credential=credential,
            params={"page": max(0, int(page))},
        )

    async def topic(self, topic_id: int, credential: dict | None = None) -> dict:
        return await self.request(
            "GET", f"/t/{int(topic_id)}.json", credential=credential
        )

    async def topic_posts(
        self, topic_id: int, post_ids: list[int], credential: dict | None = None
    ) -> dict:
        params = [("post_ids[]", int(pid)) for pid in post_ids]
        return await self.request(
            "GET",
            f"/t/{int(topic_id)}/posts.json",
            credential=credential,
            params=params,
        )

    async def search(
        self, query: str, page: int = 0, credential: dict | None = None
    ) -> dict:
        return await self.request(
            "GET",
            "/search.json",
            credential=credential,
            params={"q": query, "page": max(0, int(page))},
        )

    async def notifications(
        self, credential: dict | None = None, limit: int = 20
    ) -> dict:
        return await self.request(
            "GET",
            "/notifications.json",
            credential=credential,
            params={"recent": "true", "limit": max(1, int(limit))},
        )

    async def mark_notifications_read(self, credential: dict) -> dict:
        return await self.request(
            "PUT", "/notifications/mark-read.json", credential=credential
        )

    async def message_bus_poll(
        self,
        credential: dict,
        client_id: str,
        channels: dict[str, int],
        seq: int,
        long_poll: bool = True,
        timeout: int = 45,
    ) -> list[dict]:
        """MessageBus 订阅接口：Discourse 网页端就是靠它实时收通知和聊天。

        表单里每个键都是频道名，值是该频道本地最后收到的 message_id；
        返回结果里的 message_id 用于下次续订，所以不会重放旧消息。
        """
        session = await self._get_session()
        form = aiohttp.FormData()
        for channel, position in channels.items():
            form.add_field(channel, str(int(position)))
        form.add_field("__seq", str(int(seq)))
        headers = self._headers(credential)
        headers["Dont-Chunk"] = "true"
        headers["X-SILENCE-LOGGER"] = "true"
        url = f"{self.api_base}/message-bus/{client_id}/poll"
        if not long_poll:
            url += "?dlp=t"
        try:
            async with session.post(
                url,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=max(10, int(timeout))),
            ) as response:
                text = await response.text()
                try:
                    payload = json.loads(text) if text else []
                except ValueError:
                    payload = None
                if response.status < 200 or response.status >= 300:
                    raise DiscourseError(
                        response.status,
                        _error_message(response.status, payload),
                        payload,
                        _retry_after(response.status, response.headers, payload),
                    )
                return payload if isinstance(payload, list) else []
        except DiscourseError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DiscourseError(None, f"MessageBus 连接中断：{exc}") from exc

    async def post(self, post_id: int, credential: dict | None = None) -> dict:
        return await self.request(
            "GET", f"/posts/{int(post_id)}.json", credential=credential
        )

    async def post_by_number(
        self, topic_id: int, post_number: int, credential: dict | None = None
    ) -> dict:
        return await self.request(
            "GET",
            f"/posts/by_number/{int(topic_id)}/{int(post_number)}.json",
            credential=credential,
        )

    async def categories(self, credential: dict | None = None) -> dict:
        return await self.request("GET", "/categories.json", credential=credential)

    async def current_user(self, credential: dict) -> dict:
        data = await self.request("GET", "/session/current.json", credential=credential)
        return data.get("current_user", {}) or {}

    async def search_users(self, term: str, credential: dict | None = None) -> dict:
        return await self.request(
            "GET",
            "/u/search/users.json",
            credential=credential,
            params={"term": str(term or "").strip(), "include_groups": "false"},
        )

    async def user(self, username: str, credential: dict | None = None) -> dict:
        return await self.request(
            "GET", f"/u/{str(username or '').strip()}.json", credential=credential
        )

    async def user_profile(
        self, username: str, credential: dict | None = None
    ) -> dict:
        return await self.request(
            "GET",
            f"/u/{str(username or '').strip()}.json",
            credential=credential,
            params={"include_bio": "true"},
        )

    async def private_messages(
        self, username: str, page: int = 0, credential: dict | None = None
    ) -> dict:
        return await self.request(
            "GET",
            f"/topics/private-messages/{str(username or '').strip()}.json",
            credential=credential,
            params={"page": max(0, int(page))},
        )

    async def follow_user(
        self, credential: dict, username: str, follow: bool = True
    ) -> dict:
        return await self.request(
            "PUT" if follow else "DELETE",
            f"/follow/{str(username or '').strip()}.json",
            credential=credential,
        )

    async def follow_list(
        self, credential: dict, username: str, kind: str = "following"
    ) -> dict:
        target = "followers" if str(kind).strip().lower() in ("followers", "粉丝") else "following"
        return await self.request(
            "GET",
            f"/u/{str(username or '').strip()}/follow/{target}.json",
            credential=credential,
        )

    # ---- 聊天（Discourse Chat）----

    async def chat_dm_channels(self, credential: dict) -> dict:
        """当前账号参与的聊天频道列表（含直接消息）。"""
        return await self.request(
            "GET", "/chat/api/me/channels", credential=credential
        )

    async def chat_messages(
        self, credential: dict, channel_id: int, page_size: int = 5
    ) -> dict:
        """读取聊天频道的最近消息。"""
        return await self.request(
            "GET",
            f"/chat/api/channels/{int(channel_id)}/messages",
            credential=credential,
            params={"page_size": max(1, int(page_size))},
        )

    async def chat_send(
        self, credential: dict, channel_id: int, text: str, in_reply_to: int = 0
    ) -> dict:
        """在聊天频道（含直接消息）发一条消息。"""
        payload = {"message": str(text or "")}
        if in_reply_to:
            payload["in_reply_to_id"] = int(in_reply_to)
        return await self.request(
            "POST",
            f"/chat/{int(channel_id)}",
            credential=credential,
            json_data=payload,
        )

    # ---- 写入 ----

    async def create_post(self, credential: dict, payload: dict) -> dict:
        return await self.request(
            "POST", "/posts.json", credential=credential, json_data=payload
        )

    async def delete_post(self, credential: dict, post_id: int) -> dict:
        return await self.request(
            "DELETE", f"/posts/{int(post_id)}.json", credential=credential
        )

    async def like_post(self, credential: dict, post_id: int) -> dict:
        """点赞（post_action_type_id=2 即 like）。"""
        return await self.request(
            "POST",
            "/post_actions.json",
            credential=credential,
            json_data={"id": int(post_id), "post_action_type_id": 2},
        )

    async def unlike_post(self, credential: dict, post_id: int) -> dict:
        """取消点赞。"""
        return await self.request(
            "DELETE",
            "/post_actions/2.json",
            credential=credential,
            params={"id": int(post_id)},
        )

    async def upload(
        self,
        credential: dict,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict:
        """上传文件到论坛图床（POST /uploads.json）。"""
        import aiohttp

        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field(
            "file",
            data,
            filename=filename or "upload.bin",
            content_type=content_type,
        )
        form.add_field("type", "composer")
        form.add_field("synchronous", "true")
        url = f"{self.api_base}/uploads.json"
        headers = self._headers(credential)
        # 图片上传比普通接口慢得多（大 GIF 可能十几秒），单独放宽超时
        upload_timeout = aiohttp.ClientTimeout(total=max(120, self.timeout * 4))
        try:
            async with session.post(
                url, data=form, headers=headers, timeout=upload_timeout
            ) as response:
                payload = None
                try:
                    payload = await response.json(content_type=None)
                except Exception:  # noqa: BLE001
                    payload = None
                if response.status < 200 or response.status >= 300:
                    raise DiscourseError(
                        response.status,
                        _error_message(response.status, payload),
                        payload,
                        _retry_after(response.status, response.headers, payload),
                    )
                return payload if isinstance(payload, dict) else {"data": payload}
        except DiscourseError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DiscourseError(
                None,
                f"上传失败（{type(exc).__name__}）：{exc or '请求超时或连接中断'}",
            ) from exc

    # ---- URL ----

    def absolute_url(self, path: str) -> str:
        if not path:
            return self.site_url
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.site_url}{path}"

    def topic_url(self, topic_id: int) -> str:
        return f"{self.site_url}/t/{int(topic_id)}"


def _to_seconds(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if seconds > 0 else 0.0


def _retry_after(status: int, headers, payload) -> float:
    """429 的等待秒数：优先 Retry-After 头，其次响应体里的 retry_after。"""
    if status != 429:
        return 0.0
    seconds = _to_seconds(headers.get("Retry-After") if headers else None)
    if seconds > 0:
        return seconds
    if isinstance(payload, dict):
        candidate = payload.get("retry_after")
        extras = payload.get("extras")
        if isinstance(extras, dict):
            candidate = candidate or extras.get("retry_after")
        seconds = _to_seconds(candidate)
    return seconds


def _error_message(status: int, payload) -> str:
    errors = ""
    if isinstance(payload, dict):
        raw_errors = payload.get("errors")
        if isinstance(raw_errors, list) and raw_errors:
            errors = "；".join(str(item) for item in raw_errors)
        elif isinstance(raw_errors, str):
            errors = raw_errors
    if status == 400:
        return f"请求参数有误（400）{('：' + errors) if errors else ''}"
    if status == 401:
        return "授权已失效或 API Key 无效（401），请重新绑定论坛账号。"
    if status == 403:
        if "silenced" in errors.lower() or "暂停" in errors or "禁言" in errors:
            return (
                f"账号当前被禁言或受限（403）：{errors}"
                if errors
                else "账号当前被禁言或受限（403），需要管理员解禁。"
            )
        return f"没有权限执行该操作（403）{('：' + errors) if errors else ''}"
    if status == 404:
        return "目标主题、楼层或资源不存在（404）。"
    if status == 422:
        return f"内容未通过校验（422）{('：' + errors) if errors else ''}"
    if status == 429:
        return "请求过于频繁（429），请稍后再试。"
    if status >= 500:
        return f"Discourse 服务端错误（{status}）{('：' + errors) if errors else ''}"
    return f"Discourse 返回 HTTP {status}{('：' + errors) if errors else ''}"


_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>', re.IGNORECASE)
_A_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"\n{3,}")


def html_to_text(cooked: str) -> str:
    """把 Discourse 的 cooked HTML 转成适合 LLM 阅读的纯文本。"""
    if not cooked:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", cooked, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = _IMG_RE.sub(lambda m: f"![图片]({m.group(1)})", text)
    text = _A_RE.sub(
        lambda m: f"{_TAG_RE.sub('', m.group(2)).strip()} ({m.group(1)})", text
    )
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()
