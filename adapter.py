"""Discourse 论坛平台适配器：把论坛 @提及/回复作为 AstrBot 消息事件接收。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.star import StarTools

from .discourse_api import DiscourseAPI, DiscourseError, html_to_text
from .discourse_event import DiscourseMessageEvent
from .store import RETENTION_SECONDS, StateStore


def _parse_iso(value) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0

DEFAULT_CONFIG = {
    "site_url": "https://xinjianya.top",
    "token": "",
    "poll_interval": 60,
    "auto_reply": True,
    "reply_probability": 0.1,
    "reply_mention_user": True,
}

CONFIG_METADATA = {
    "site_url": {
        "description": "论坛地址",
        "type": "string",
        "hint": "例如 https://xinjianya.top",
    },
    "token": {
        "description": "API key",
        "type": "string",
        "hint": "论坛 偏好设置 → API key 面板生成的 Key",
    },
    "poll_interval": {
        "description": "轮询间隔（秒）",
        "type": "int",
        "hint": "检查论坛 @提及 / 回复的间隔，最小 15 秒",
    },
    "auto_reply": {
        "description": "收到通知后自动唤醒 AI 回复",
        "type": "bool",
        "hint": "关闭后只接收通知、不触发 AI 回复",
    },
    "reply_probability": {
        "description": "自动回复概率",
        "type": "float",
        "hint": "0~1，0.1 表示约一成通知会触发 AI 回复",
    },
    "reply_mention_user": {
        "description": "回帖时额外 @ 对方",
        "type": "bool",
        "hint": "引用原楼层始终会带上，这里只控制是否再 @ 用户",
    },
}


@register_platform_adapter(
    "discourse",
    "Discourse 论坛适配器 - 接收论坛 @提及/回复",
    default_config_tmpl=DEFAULT_CONFIG,
    adapter_display_name="Discourse 论坛",
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
)
class DiscourseAdapter(Platform):
    """通过轮询 /notifications.json 接收提及、回复、引用通知。"""

    # 通知里附带的主题上下文上限：主楼 + 最近几楼，避免撑爆模型上下文
    CONTEXT_FIRST_POST_CHARS = 1200
    CONTEXT_RECENT_POSTS = 3
    CONTEXT_RECENT_POST_CHARS = 400
    # 自动引用原楼层时最多摘取多少字符
    QUOTE_CHARS = 500
    # 同一主题在此时间内只允许发一条回复，避免工具回复后又被补发一条文本
    DUPLICATE_WINDOW = 30

    def __init__(self, config: dict, settings: dict, event_queue: asyncio.Queue):
        super().__init__(config, event_queue)
        self.settings = settings or {}
        self.platform_id = str(config.get("id") or "discourse")
        self.site_url = str(config.get("site_url") or "").rstrip("/")
        self.poll_interval = max(15, int(config.get("poll_interval") or 60))
        self.typing_duration = max(
            3001, int(config.get("typing_duration_msecs") or 20000)
        )
        raw_types = config.get("notification_types") or [1, 2, 3, 6, 15]
        try:
            self.notification_types = {int(item) for item in raw_types}
        except (TypeError, ValueError):
            self.notification_types = {1, 2, 3, 6, 15}

        self.token = str(config.get("token") or "").strip()
        self.auto_reply = bool(config.get("auto_reply", True))
        self.reply_mention_user = bool(config.get("reply_mention_user", True))
        try:
            probability = float(config.get("reply_probability", 0.1))
        except (TypeError, ValueError):
            probability = 0.1
        self.reply_probability = max(0.0, min(1.0, probability))
        self.credential = {
            "api_key": self.token,
            "client_id": str(config.get("api_client_id") or "").strip(),
            "username": "",
        }
        self.api = DiscourseAPI(
            site_url=self.site_url,
            api_base=str(config.get("api_base") or "").strip(),
            host_header=str(config.get("host_header") or "").strip(),
            timeout=30,
        )
        self._metadata = PlatformMetadata(
            name="discourse",
            description="Discourse 论坛适配器",
            id=self.platform_id,
            adapter_display_name="Discourse 论坛",
            support_streaming_message=False,
        )
        self._last_seen_notification_id = 0
        self._self_username = ""
        self._emitted_posts: set[tuple[int, int]] = set()
        # 主题 -> (引用前缀, 楼层号)：供会话纯文本回帖路径复用同一个引用
        self._pending_replies: dict[int, tuple[str, int]] = {}
        try:
            self.store = StateStore(StarTools.get_data_dir("astrbot_plugin_discourse"))
        except Exception:  # noqa: BLE001
            self.store = None
        self._stopped = False
        self._task: asyncio.Task | None = None

    def meta(self) -> PlatformMetadata:
        return self._metadata

    def create_event(self, message: AstrBotMessage) -> DiscourseMessageEvent:
        return DiscourseMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            adapter=self,
        )

    def run(self):
        return self._poll_loop()

    async def _poll_loop(self):
        self._task = asyncio.current_task()
        if not self.token:
            logger.warning("[DiscourseAdapter] 未配置 token，无法接收论坛通知。")
            return

        try:
            user = await self.api.current_user(self.credential)
            self._self_username = str(user.get("username") or "")
        except DiscourseError as exc:
            logger.warning("[DiscourseAdapter] 读取论坛身份失败：%s", exc.message)

        # 首次运行只记录基线，不处理历史通知
        while not self._stopped:
            try:
                data = await self.api.notifications(self.credential, limit=20)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DiscourseAdapter] 读取通知失败：%s", exc)
                await asyncio.sleep(self.poll_interval)

        notifications = (data.get("notifications") or []) if "data" in locals() else []
        self._last_seen_notification_id = max(
            (int(item.get("id") or 0) for item in notifications), default=0
        )
        logger.info(
            "[DiscourseAdapter] 已启动，基线通知 ID=%s，轮询间隔 %s 秒",
            self._last_seen_notification_id,
            self.poll_interval,
        )

        while not self._stopped:
            await asyncio.sleep(self.poll_interval)
            if self.store is not None:
                self.store.prune()
            try:
                data = await self.api.notifications(self.credential, limit=20)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DiscourseAdapter] 轮询通知失败：%s", exc)
                continue

            notifications = data.get("notifications") or []
            new_items = [
                item
                for item in notifications
                if int(item.get("id") or 0) > self._last_seen_notification_id
            ]
            new_items.sort(key=lambda item: int(item.get("id") or 0))

            for item in new_items:
                try:
                    await self._emit_notification(item)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[DiscourseAdapter] 处理通知失败：%s", exc)
                self._last_seen_notification_id = max(
                    self._last_seen_notification_id, int(item.get("id") or 0)
                )

        await self.api.close()

    async def _emit_notification(self, notification: dict):
        notification_type = int(notification.get("notification_type") or 0)
        if notification_type not in self.notification_types:
            return

        data = notification.get("data") or {}
        post_id = data.get("original_post_id")
        topic_id = notification.get("topic_id")
        post_number = notification.get("post_number")

        post = None
        if post_id:
            try:
                post = await self.api.post(int(post_id), self.credential)
            except DiscourseError:
                post = None

        if not post and topic_id and post_number:
            try:
                topic = await self.api.topic(int(topic_id), self.credential)
                posts = (topic.get("post_stream") or {}).get("posts") or []
                post = next(
                    (
                        item
                        for item in posts
                        if int(item.get("post_number") or 0) == int(post_number)
                    ),
                    None,
                )
            except DiscourseError:
                post = None

        if not post:
            logger.info(
                "[DiscourseAdapter] 通知 %s 找不到对应帖子，跳过",
                notification.get("id"),
            )
            return

        username = str(post.get("username") or data.get("display_username") or "未知")
        user_id = post.get("user_id") or data.get("original_user_id") or username
        if post.get("yours") or (self._self_username and username == self._self_username):
            return

        if self.store is not None:
            if self.store.is_blocked(user_id, username):
                logger.info("[DiscourseAdapter] 通知来自黑名单用户 @%s，跳过", username)
                return
            if self.store.has_replied(topic_id, post_number):
                logger.info(
                    "[DiscourseAdapter] 主题 %s 第 %s 楼已经回过，跳过自动回复",
                    topic_id,
                    post_number,
                )
                return

        created = _parse_iso(post.get("created_at"))
        if created and time.time() - created > RETENTION_SECONDS:
            logger.info(
                "[DiscourseAdapter] 帖子发布于 %s，超过三天，不自动回复",
                post.get("created_at"),
            )
            return

        # 同一条帖子可能同时产生多条通知（如"回复了你"+"提到了你"），
        # 只给一次机会，避免对同一楼重复回帖
        dedupe_key = (int(topic_id or 0), int(post_number or 0))
        if dedupe_key in self._emitted_posts:
            logger.info(
                "[DiscourseAdapter] 帖子 %s#%s 已处理过，跳过通知 %s",
                dedupe_key[0],
                dedupe_key[1],
                notification.get("id"),
            )
            return
        if len(self._emitted_posts) > 2000:
            self._emitted_posts.clear()
        self._emitted_posts.add(dedupe_key)

        if not self.auto_reply:
            logger.info(
                "[DiscourseAdapter] 自动回复已关闭，跳过通知 %s（@%s -> 主题 %s）",
                notification.get("id"),
                username,
                topic_id,
            )
            return
        if random.random() > self.reply_probability:
            logger.info(
                "[DiscourseAdapter] 自动回复概率未命中（%.0f%%），跳过通知 %s（@%s -> 主题 %s）",
                self.reply_probability * 100,
                notification.get("id"),
                username,
                topic_id,
            )
            return

        title = html_to_text(
            str(notification.get("fancy_title") or data.get("topic_title") or "")
        ).strip()
        content = html_to_text(str(post.get("cooked") or post.get("raw") or "")).strip()
        if len(content) > 3000:
            content = content[:3000] + "……"

        action_text = "提到了你"
        if notification_type == 2:
            action_text = "回复了你"
        elif notification_type == 3:
            action_text = "引用了你"
        elif notification_type == 6:
            action_text = "在私聊里发了新消息"
        elif notification_type == 15:
            action_text = "在群里提到了你"

        reply_prefix = self._build_reply_prefix(post, topic_id, post_number, username)

        message_str = (
            f"[论坛通知] @{username} 在主题《{title or '未命名'}》"
            f"（ID:{topic_id}）中{action_text}：\n\n{content}\n\n"
        )
        context_block = await self._topic_context(
            int(topic_id or 0), int(post_number or 0)
        )
        if context_block:
            message_str += (
                f"【主题上下文】（只含主楼和最近 {self.CONTEXT_RECENT_POSTS} 楼，"
                f"已按长度截断）\n{context_block}\n\n"
            )
        message_str += (
            f"需要更多楼层请调用 discourse_read_topic(topic_id=\"{topic_id}\")。"
            f"回复时请调用 discourse_publish_reply(topic_id=\"{topic_id}\", content=\"你的回复正文\")，"
            f"topic_id 和 content 两个参数都必须提供。"
            "系统会自动引用对方原楼层"
            + ("并 @ 对方" if self.reply_mention_user else "")
            + "，正文里不要再自己写 [quote] 或重复 @对方。"
            "需要 @ 其他人时，先用 discourse_search_user 确认用户名存在，"
            "再把用户名填进 discourse_publish_reply 的 mentions 参数。"
            "调用了回复工具后就不要再输出额外文字，否则会重复发帖。"
        )

        abm = AstrBotMessage()
        abm.self_id = self.platform_id
        abm.sender = MessageMember(user_id=str(user_id), nickname=username)
        abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = str(topic_id or "")
        abm.message_id = f"discourse_notification_{notification.get('id')}"
        abm.message = [Plain(text=message_str)]
        abm.message_str = message_str
        abm.raw_message = notification
        abm.timestamp = int(time.time())

        event = self.create_event(abm)
        event.set_reply_target(int(topic_id or 0), int(post_number or 0))
        event.set_extra("discourse_topic_id", topic_id)
        event.set_extra("discourse_post_number", post_number)
        event.set_extra("discourse_notification_type", notification_type)
        event.set_extra("discourse_reply_prefix", reply_prefix)
        event.set_extra("discourse_reply_username", username)
        if reply_prefix:
            if len(self._pending_replies) > 50:
                self._pending_replies.clear()
            self._pending_replies[int(topic_id or 0)] = (
                reply_prefix,
                int(post_number or 0),
            )
        event.is_wake = True
        event.is_at_or_wake_command = True

        self.commit_event(event)
        logger.info(
            "[DiscourseAdapter] 已投递通知事件：@%s -> 主题 %s (%s)",
            username,
            topic_id,
            notification_type,
        )

    def _build_reply_prefix(
        self, post: dict, topic_id, post_number, username: str
    ) -> str:
        """构造回复前缀：Discourse 原生引用块 + 可选 @ 对方。"""
        number = int(post_number or 0)
        tid = int(topic_id or 0)
        if tid <= 0 or number <= 0 or not username:
            return ""
        quote_text = str(post.get("raw") or "").strip()
        if not quote_text:
            quote_text = html_to_text(str(post.get("cooked") or "")).strip()
        quote_text = quote_text.replace("[/quote]", "").strip()
        if len(quote_text) > self.QUOTE_CHARS:
            quote_text = quote_text[: self.QUOTE_CHARS] + "……"
        lines = [
            f'[quote="{username}, post:{number}, topic:{tid}"]',
            quote_text,
            "[/quote]",
        ]
        if self.reply_mention_user:
            lines.append(f"@{username}")
        return "\n".join(lines)

    async def _topic_context(self, topic_id: int, trigger_post_number: int) -> str:
        """取主楼 + 最近几楼做摘要；条数和字数都有上限，触发通知的那条不重复。"""
        if topic_id <= 0:
            return ""
        try:
            topic = await self.api.topic(topic_id, self.credential)
        except DiscourseError as exc:
            logger.info("[DiscourseAdapter] 读取主题 %s 上下文失败：%s", topic_id, exc.message)
            return ""

        stream_data = topic.get("post_stream") or {}
        stream = [int(pid) for pid in stream_data.get("stream") or []]
        if not stream:
            return ""

        wanted: list[int] = [stream[0]]
        for pid in stream[-self.CONTEXT_RECENT_POSTS :]:
            if pid not in wanted:
                wanted.append(pid)

        known = {
            int(item.get("id")): item
            for item in stream_data.get("posts") or []
            if item.get("id")
        }
        missing = [pid for pid in wanted if pid not in known]
        if missing:
            try:
                extra = await self.api.topic_posts(topic_id, missing, self.credential)
                for item in (extra.get("post_stream") or {}).get("posts") or []:
                    if item.get("id"):
                        known[int(item["id"])] = item
            except DiscourseError:
                pass

        parts: list[str] = []
        for pid in wanted:
            post = known.get(pid)
            if not post:
                continue
            if self.store is not None and self.store.is_blocked(
                post.get("user_id"), post.get("username") or ""
            ):
                continue
            number = int(post.get("post_number") or 0)
            if number == trigger_post_number:
                continue
            text = html_to_text(str(post.get("cooked") or post.get("raw") or "")).strip()
            limit = (
                self.CONTEXT_FIRST_POST_CHARS
                if number == 1
                else self.CONTEXT_RECENT_POST_CHARS
            )
            if len(text) > limit:
                text = text[:limit] + "……（已截断）"
            if text:
                parts.append(f"#{number} @{post.get('username') or '未知'}：\n{text}")
        return "\n\n".join(parts)

    async def send_by_session(self, session, message_chain) -> None:
        text = (message_chain.get_plain_text() or "").strip()
        if not text:
            return
        topic_id = self._parse_topic_id(getattr(session, "session_id", ""))
        if not topic_id:
            logger.warning(
                "[DiscourseAdapter] 无法从会话解析主题 ID：%s",
                getattr(session, "session_id", ""),
            )
            return
        prefix, post_number = self._pending_replies.pop(topic_id, ("", 0))
        if prefix and not text.lstrip().startswith("[quote="):
            text = f"{prefix}\n\n{text}"
        await self.reply_to_topic(
            topic_id, text, reply_to_post_number=post_number
        )

    async def reply_to_topic(
        self, topic_id: int, text: str, reply_to_post_number: int = 0
    ) -> bool:
        """把文本作为回帖发到指定主题。"""
        if not self.token:
            logger.warning("[DiscourseAdapter] 未配置 token，无法回复论坛。")
            return False
        topic_id = int(topic_id or 0)
        if topic_id <= 0:
            return False
        if self.store is not None and self.store.replied_within(
            topic_id, self.DUPLICATE_WINDOW
        ):
            logger.info(
                "[DiscourseAdapter] 主题 %s 刚刚已经回复过，忽略本次重复文本回复",
                topic_id,
            )
            return False

        payload = {
            "topic_id": topic_id,
            "raw": text,
            "typing_duration_msecs": self.typing_duration,
        }
        if reply_to_post_number:
            payload["reply_to_post_number"] = int(reply_to_post_number)
        try:
            result = await self.api.create_post(self.credential, payload)
        except DiscourseError as exc:
            logger.warning("[DiscourseAdapter] 回复主题 %s 失败：%s", topic_id, exc.message)
            return False

        if str(result.get("action") or "") == "enqueued":
            logger.info("[DiscourseAdapter] 回复主题 %s 已提交，等待审核", topic_id)
        else:
            logger.info("[DiscourseAdapter] 已回复主题 %s", topic_id)
        if self.store is not None:
            self.store.mark_replied(topic_id, reply_to_post_number)
        return True

    @staticmethod
    def _parse_topic_id(value) -> int:
        text = str(value or "")
        match = re.search(r"(\d+)$", text)
        return int(match.group(1)) if match else 0

    async def terminate(self):
        self._stopped = True
        task = self._task
        if task and not task.done():
            task.cancel()
        await self.api.close()
