"""Discourse 论坛平台适配器：把论坛 @提及/回复作为 AstrBot 消息事件接收。"""

from __future__ import annotations

import asyncio
import mimetypes
import random
import re
import time
from datetime import datetime
from pathlib import Path

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
from .imagebed import upload_to_bed
from .store import RETENTION_SECONDS, StateStore


def _plugin_version() -> str:
    """读取 metadata.yaml 的版本号，方便在日志里确认加载的是哪一版。"""
    try:
        import yaml

        path = Path(__file__).resolve().parent / "metadata.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str(data.get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
    "notify_title_only": False,
    "image_host": "auto",
    "image_host_fallback": True,
    "smms_token": "",
    "imgbb_api_key": "",
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
        "hint": "回复本身已挂在对方楼层下，这里只控制是否再 @ 用户",
    },
    "notify_title_only": {
        "description": "只给 AI 主题标题，由它决定是否查看/回复",
        "type": "bool",
        "hint": "开启后通知只附带标题，AI 需要时自己调用 discourse_read_topic；可显著减少提示词长度",
    },
    "image_host": {
        "description": "图片上传服务",
        "type": "string",
        "hint": "auto=按顺序尝试；也可填 catbox / litterbox / uguu / 0x0 / smms / imgbb / discourse",
    },
    "image_host_fallback": {
        "description": "图床失败后自动尝试下一个",
        "type": "bool",
        "hint": "关闭后只用第一个图床，失败就不再尝试",
    },
    "smms_token": {
        "description": "sm.ms Token（可选）",
        "type": "string",
        "hint": "填了才会把 sm.ms 加入 auto 顺序，格式通常是带 Bearer 前缀的 Token",
    },
    "imgbb_api_key": {
        "description": "imgbb API Key（可选）",
        "type": "string",
        "hint": "填了才会把 imgbb 加入 auto 顺序",
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
    # 同一主题在此时间内只允许发一条回复：模型重试/分段插件会拖到几十秒后才发
    DUPLICATE_WINDOW = 120
    # LLM 决定跳过某条通知后，这段时间内该目标的残留文本一律丢弃
    SKIP_WINDOW = 600
    # 聊天通知类型：chat_mention / chat_message / chat_quoted
    CHAT_TYPES = {29, 30, 33}
    # 聊天消息原文直通，指令可正常触发
    BUILD_TAG = "2026-11-02-chatcmd"
    # 其它插件发来的图片，先暂存这么久，等随下一条回复一起发；没等到就单独发
    IMAGE_HOLD_SECONDS = 15

    def __init__(self, config: dict, settings: dict, event_queue: asyncio.Queue):
        super().__init__(config, event_queue)
        self.settings = settings or {}
        self.platform_id = str(config.get("id") or "discourse")
        self.site_url = str(config.get("site_url") or "").rstrip("/")
        self.poll_interval = max(15, int(config.get("poll_interval") or 60))
        self.typing_duration = max(
            3001, int(config.get("typing_duration_msecs") or 20000)
        )
        raw_types = config.get("notification_types") or [1, 2, 3, 6, 15, 29, 30, 33]
        try:
            self.notification_types = {int(item) for item in raw_types}
        except (TypeError, ValueError):
            self.notification_types = {1, 2, 3, 6, 15, 29, 30, 33}

        self.token = str(config.get("token") or "").strip()
        self.auto_reply = bool(config.get("auto_reply", True))
        self.reply_mention_user = bool(config.get("reply_mention_user", True))
        self.notify_title_only = bool(config.get("notify_title_only", False))
        self.image_host = str(config.get("image_host") or "auto").strip().lower()
        self.image_host_fallback = bool(config.get("image_host_fallback", True))
        self.smms_token = str(config.get("smms_token") or "").strip()
        self.imgbb_api_key = str(config.get("imgbb_api_key") or "").strip()
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
        self._emitted_chat: set[tuple[int, int]] = set()
        # 主题 -> (引用前缀, 楼层号)：供会话纯文本回帖路径复用同一个引用
        self._pending_replies: dict[int, tuple[str, int]] = {}
        # "topic:12" / "chat:3" -> 跳过时间：LLM 决定不回复后，残留文本不再发帖
        self._skipped_targets: dict[str, float] = {}
        # 主题 -> 最近一次发图片时间，用于去重（图片不占用楼层去重记录）
        self._image_sent_at: dict[int, float] = {}
        # "topic:12" / "chat:3" -> 待随下一条回复一起发送的图片
        self._pending_images: dict[str, list[str]] = {}
        self._image_flush_tasks: dict[str, asyncio.Task] = {}
        # 论坛没启用聊天 / 无权访问时停止轮询
        self._chat_poll_disabled = False
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
            "[DiscourseAdapter] v%s（build %s）已启动，基线通知 ID=%s，轮询间隔 %s 秒",
            _plugin_version() or "?",
            self.BUILD_TAG,
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

            # 聊天直接消息不一定产生通知，额外轮询一次频道做兜底
            try:
                await self._poll_chat_dms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DiscourseAdapter] 轮询聊天频道失败：%s", exc)

        await self.api.close()

    async def _emit_notification(self, notification: dict):
        notification_type = int(notification.get("notification_type") or 0)
        if notification_type not in self.notification_types:
            logger.info(
                "[DiscourseAdapter] 通知 %s 类型 %s 不在监听列表内，跳过",
                notification.get("id"),
                notification_type,
            )
            return

        if notification_type in self.CHAT_TYPES:
            await self._emit_chat_notification(notification, notification_type)
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

        # 同一主题来了新通知，解除上一次“决定不回复”的拦截，避免误伤后续回复
        self._skipped_targets.pop(f"topic:{int(topic_id or 0)}", None)

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

        # 提及 / 引用 / 群组提及 / 私信：必须回复，不走概率
        forced_reply = notification_type in (1, 3, 6, 15)
        # 回复了你的帖子（自己主题或别人主题里的楼层）：交给 LLM 判断要不要回
        let_llm_decide = notification_type == 2

        if (
            not forced_reply
            and not let_llm_decide
            and random.random() > self.reply_probability
        ):
            logger.info(
                "[DiscourseAdapter] 自动回复概率未命中（%.0f%%），跳过通知 %s"
                "（type=%s，@%s -> 主题 %s）",
                self.reply_probability * 100,
                notification.get("id"),
                notification_type,
                username,
                topic_id,
            )
            return

        title = html_to_text(
            str(
                notification.get("fancy_title")
                or data.get("topic_title")
                or ""
            )
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

        reply_prefix = self._build_reply_prefix(topic_id, post_number, username)

        if self.notify_title_only:
            message_str = (
                f"[论坛通知] @{username} 在主题《{title or '未命名'}》"
                f"（ID:{topic_id}）{action_text}。\n\n"
                "通知这里只提供标题，是否查看或回复由你决定：\n"
                f"- 想了解具体内容：discourse_read_topic(topic_id=\"{topic_id}\")\n"
                f"- 决定回复：discourse_publish_reply(topic_id=\"{topic_id}\", content=\"你的回复正文\")\n"
                f"- 不想回复：discourse_skip_reply\n"
                "调用工具后请输出一句很短的话（例如“已处理”），系统会忽略这段文字。"
            )
            if forced_reply:
                message_str += (
                    "\n【这条属于提及/引用/私信，必须回复】"
                    "先读取内容再回复；只有骚扰或垃圾内容才允许跳过或拉黑。"
                )
        else:
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
                "系统会自动把回复挂到对方楼层下"
                + ("并 @ 对方" if self.reply_mention_user else "")
                + "，正文里不要再自己写 [quote] 或重复 @对方。"
                "需要 @ 其他人时，先用 discourse_search_user 确认用户名存在，"
                "再把用户名填进 discourse_publish_reply 的 mentions 参数。"
                "调用了回复工具后，只需输出一句很短的确认（例如“已回复”），"
                "系统会自动忽略这段文字，不会重复发帖。"
            )
            if forced_reply:
                message_str += (
                    "\n\n【对方主动提及/引用/私信了你，必须回复】"
                    "请调用 discourse_publish_reply 回复。"
                    "只有内容明显是骚扰或垃圾信息时，才可以调用 discourse_skip_reply 跳过，"
                    "或先用 discourse_block_user 拉黑对方再跳过。"
                    "工具调用后输出一句短确认即可，系统会忽略。"
                )
            elif let_llm_decide:
                message_str += (
                    "\n\n【对方回复了你的帖子】"
                    "请你自行判断这条回复是否值得回应："
                    "值得就调用 discourse_publish_reply 回复；"
                    "觉得没必要就不用回，调用 discourse_skip_reply 明确跳过。"
                    "无论回不回，工具调用后都请输出一句短确认，系统会忽略这段文字。"
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

    async def _emit_chat_notification(
        self, notification: dict, notification_type: int
    ) -> None:
        """Discourse Chat 通知（聊天直接消息 / 频道提及）。"""
        data = notification.get("data") or {}
        channel_id = _as_int(data.get("chat_channel_id"))
        message_id = _as_int(data.get("chat_message_id"))
        if channel_id <= 0:
            logger.info(
                "[DiscourseAdapter] 聊天通知 %s 缺少频道 ID，跳过",
                notification.get("id"),
            )
            return
        if len(self._emitted_chat) > 2000:
            self._emitted_chat.clear()
        if message_id and (channel_id, message_id) in self._emitted_chat:
            logger.info(
                "[DiscourseAdapter] 聊天消息 %s#%s 已处理过，跳过通知 %s",
                channel_id,
                message_id,
                notification.get("id"),
            )
            return

        try:
            payload = await self.api.chat_messages(self.credential, channel_id, 5)
        except DiscourseError as exc:
            logger.info(
                "[DiscourseAdapter] 读取聊天频道 %s 消息失败：%s",
                channel_id,
                exc.message,
            )
            return

        messages = payload.get("messages") or []
        message = None
        if message_id:
            message = next(
                (item for item in messages if _as_int(item.get("id")) == message_id),
                None,
            )
        if not message:
            # 通知没带可用的消息 ID 时，取最后一条不是自己发的
            for item in reversed(messages):
                if self._self_username and self._chat_username(item) == self._self_username:
                    continue
                message = item
                break
        if not message:
            logger.info(
                "[DiscourseAdapter] 聊天频道 %s 没有找到对应消息，跳过通知 %s",
                channel_id,
                notification.get("id"),
            )
            return

        self._dispatch_chat_message(
            channel_id=channel_id,
            message=message,
            channel_title=str(data.get("chat_channel_title") or "").strip(),
            is_dm=bool(data.get("is_direct_message_channel")),
            notification_type=notification_type,
            source_id=str(notification.get("id") or ""),
        )

    @staticmethod
    def _chat_username(message: dict) -> str:
        user = message.get("user") or {}
        return str(message.get("username") or user.get("username") or "未知")

    def _dispatch_chat_message(
        self,
        channel_id: int,
        message: dict,
        channel_title: str = "",
        is_dm: bool = False,
        notification_type: int = 30,
        source_id: str = "",
    ) -> None:
        """把一条聊天消息构造为 AstrBot 事件并投递。"""
        message_id = _as_int(message.get("id"))
        if len(self._emitted_chat) > 2000:
            self._emitted_chat.clear()
        if message_id and (channel_id, message_id) in self._emitted_chat:
            return
        username = self._chat_username(message)
        user = message.get("user") or {}
        user_id = user.get("id") or username
        if self._self_username and username == self._self_username:
            return
        if self.store is not None and self.store.is_blocked(user_id, username):
            logger.info("[DiscourseAdapter] 聊天消息来自黑名单用户 @%s，跳过", username)
            return

        content = str(message.get("message") or "").strip()
        if not content:
            content = html_to_text(str(message.get("cooked") or "")).strip()
        if not content:
            content = str(message.get("excerpt") or "").strip()
        if not content:
            logger.info("[DiscourseAdapter] 聊天消息 %s 没有文本内容，跳过", message_id)
            return
        if len(content) > 3000:
            content = content[:3000] + "……"

        # 原文直接交给 AstrBot：任何前缀都会让 /help 等指令匹配失败
        message_str = content
        self._emitted_chat.add((channel_id, message_id))

        abm = AstrBotMessage()
        abm.self_id = self.platform_id
        abm.sender = MessageMember(user_id=str(user_id), nickname=username)
        abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = f"chat:{channel_id}"
        abm.message_id = f"discourse_chat_{source_id or message_id}"
        abm.message = [Plain(text=message_str)]
        abm.message_str = message_str
        abm.raw_message = message
        abm.timestamp = int(time.time())

        event = self.create_event(abm)
        event.set_extra("discourse_chat_channel_id", channel_id)
        event.set_extra("discourse_chat_message_id", message_id)
        event.set_extra("discourse_notification_type", notification_type)
        event.set_extra("discourse_reply_username", username)
        event.is_wake = True
        event.is_at_or_wake_command = True

        self.commit_event(event)
        logger.info(
            "[DiscourseAdapter] 已投递聊天消息：@%s -> %s %s（%s）",
            username,
            "私信" if is_dm else "频道",
            channel_title or channel_id,
            notification_type,
        )

    async def _poll_chat_dms(self) -> None:
        """兜底轮询直接消息频道：论坛不产生聊天通知时也能收到私聊。"""
        if self._chat_poll_disabled or self.store is None:
            return
        try:
            payload = await self.api.chat_dm_channels(self.credential)
        except DiscourseError as exc:
            if exc.status in (403, 404):
                self._chat_poll_disabled = True
                logger.info(
                    "[DiscourseAdapter] 论坛未启用聊天或无权访问，停止轮询聊天频道：%s",
                    exc.message,
                )
            else:
                logger.info("[DiscourseAdapter] 读取聊天频道列表失败：%s", exc.message)
            return

        for channel in payload.get("direct_message_channels") or []:
            channel_id = _as_int(channel.get("id"))
            if channel_id <= 0:
                continue
            try:
                data = await self.api.chat_messages(self.credential, channel_id, 3)
            except DiscourseError as exc:
                logger.info(
                    "[DiscourseAdapter] 读取聊天频道 %s 消息失败：%s",
                    channel_id,
                    exc.message,
                )
                continue
            messages = data.get("messages") or []
            if not messages:
                continue
            ids = [_as_int(item.get("id")) for item in messages]
            latest = max(ids)
            seen = self.store.chat_last_seen(channel_id)
            if seen <= 0:
                # 首次见到该频道只记录基线，不回复历史消息
                self.store.mark_chat_seen(channel_id, latest)
                logger.info(
                    "[DiscourseAdapter] 聊天频道 %s 记录基线消息 %s", channel_id, latest
                )
                continue
            if latest <= seen:
                continue
            target = None
            for item in sorted(messages, key=lambda x: _as_int(x.get("id"))):
                if _as_int(item.get("id")) <= seen:
                    continue
                if self._self_username and self._chat_username(item) == self._self_username:
                    continue
                target = item
            self.store.mark_chat_seen(channel_id, latest)
            if target is None:
                continue
            self._dispatch_chat_message(
                channel_id=channel_id,
                message=target,
                channel_title=str(channel.get("title") or "").strip(),
                is_dm=True,
                notification_type=30,
                source_id="poll",
            )

    def _build_reply_prefix(self, topic_id, post_number, username: str) -> str:
        """构造回复前缀：只保留可选的 @ 对方，不再写引用块。

        Discourse 会自动删掉紧跟在原楼层后的整楼引用，引用块时有时无；
        回复关系一律交给 reply_to_post_number 由论坛原生显示。
        """
        number = int(post_number or 0)
        tid = int(topic_id or 0)
        if tid <= 0 or number <= 0 or not username:
            return ""
        return f"@{username}" if self.reply_mention_user else ""

    async def _topic_context(self, topic_id: int, trigger_post_number: int) -> str:
        """取主楼 + 最近几楼做摘要；条数和字数都有上限，触发通知的那条不重复。"""
        if topic_id <= 0:
            return ""
        try:
            topic = await self.api.topic(topic_id, self.credential)
        except DiscourseError as exc:
            logger.info(
                "[DiscourseAdapter] 读取主题 %s 上下文失败：%s", topic_id, exc.message
            )
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
        images = await self.images_markdown(message_chain)
        if not text and not images:
            return
        if text and "<|" in text:
            logger.warning(
                "[DiscourseAdapter] 模型输出含特殊标记，丢弃以免发到论坛：%s",
                text[:80],
            )
            return
        session_id = str(getattr(session, "session_id", "") or "")
        if session_id.startswith("chat:"):
            channel_id = self._parse_topic_id(session_id)
            if not channel_id:
                logger.warning(
                    "[DiscourseAdapter] 无法从会话解析聊天频道 ID：%s", session_id
                )
                return
            combined = "\n\n".join(part for part in (text, images) if part)
            await self.send_to_chat(channel_id, combined)
            return
        topic_id = self._parse_topic_id(session_id)
        if not topic_id:
            logger.warning(
                "[DiscourseAdapter] 无法从会话解析主题 ID：%s",
                session_id,
            )
            return
        skipped_at = self._skipped_targets.get(f"topic:{topic_id}")
        if skipped_at and time.time() - skipped_at < self.SKIP_WINDOW:
            logger.info(
                "[DiscourseAdapter] LLM 已跳过主题 %s 的本次通知，丢弃会话文本",
                topic_id,
            )
            self._pending_replies.pop(topic_id, None)
            return
        if images and not text:
            # 只有图片（例如其它插件发来的表情包）：单独发一层，不占用楼层去重记录
            await self.send_image_reply(topic_id, images)
            return
        if images:
            text = f"{text}\n\n{images}"
        prefix, post_number = self._pending_replies.pop(topic_id, ("", 0))
        if prefix and not text.lstrip().startswith(prefix):
            text = f"{prefix}\n\n{text}"
        await self.reply_to_topic(
            topic_id, text, reply_to_post_number=post_number
        )

    def _image_providers(self) -> list[str]:
        host = (self.image_host or "auto").lower()
        if host and host != "auto":
            return [host]
        providers: list[str] = []
        if self.smms_token:
            providers.append("smms")
        if self.imgbb_api_key:
            providers.append("imgbb")
        providers.extend(["catbox", "uguu", "0x0", "discourse"])
        return providers

    async def upload_image_bytes(self, filename: str, data: bytes, mime: str) -> str:
        """按配置顺序上传图片，返回图片地址；全部失败返回空串。"""
        if not data:
            return ""
        for name in self._image_providers():
            try:
                if name == "discourse":
                    result = await self.api.upload(self.credential, filename, data, mime)
                    url = self.api.absolute_url(str(result.get("url") or ""))
                else:
                    url = await upload_to_bed(
                        name, filename, data, mime, self.smms_token, self.imgbb_api_key
                    )
                if url:
                    logger.info("[DiscourseAdapter] 图片上传成功（%s）：%s", name, url)
                    return url
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[DiscourseAdapter] 图床 %s 上传失败（%s）：%s",
                    name,
                    type(exc).__name__,
                    getattr(exc, "message", "") or exc,
                )
            if not self.image_host_fallback:
                break
        return ""

    async def images_markdown(self, message_chain) -> str:
        """把消息链里的图片上传到论坛图床，返回 Markdown 图片标记。"""
        parts: list[str] = []
        for comp in getattr(message_chain, "chain", None) or []:
            if type(comp).__name__ != "Image":
                continue
            try:
                path = await comp.convert_to_file_path()
                data = Path(path).read_bytes()
                name = Path(path).name or "image.png"
                mime = mimetypes.guess_type(name)[0] or "image/png"
                if not mime.startswith("image/"):
                    continue
                url = await self.upload_image_bytes(name, data, mime)
                if not url:
                    continue
                # 用 Markdown 的尺寸语法按原尺寸的 50% 显示，不修改原图
                width, height = self._image_size(data)
                size = f"|{max(1, width // 2)}x{max(1, height // 2)}" if width and height else ""
                parts.append(f"![图片{size}]({url})")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[DiscourseAdapter] 图片上传失败（%s）：%s",
                    type(exc).__name__,
                    getattr(exc, "message", "") or exc,
                )
        return "\n".join(parts)

    @staticmethod
    def _image_size(data: bytes) -> tuple[int, int]:
        """读取图片宽高，用于 Markdown 尺寸语法。"""
        try:
            import io

            from PIL import Image as PILImage

            with PILImage.open(io.BytesIO(data)) as img:
                return int(img.width), int(img.height)
        except Exception:  # noqa: BLE001
            return (0, 0)

    def stage_images(self, target_key: str, markdown: str) -> None:
        """暂存图片，等随下一条文字回复一起发；超时没等到就单独发一层。"""
        markdown = str(markdown or "").strip()
        if not markdown:
            return
        pending = self._pending_images.setdefault(target_key, [])
        if markdown in pending:
            return
        pending.append(markdown)
        task = self._image_flush_tasks.get(target_key)
        if task is None or task.done():
            self._image_flush_tasks[target_key] = asyncio.create_task(
                self._flush_images_later(target_key)
            )
        logger.info("[DiscourseAdapter] 已暂存图片到 %s，等待随回复发送", target_key)

    def take_pending_images(self, target_key: str) -> str:
        """取出并清除暂存图片，取消超时单独发送。"""
        parts = self._pending_images.pop(target_key, [])
        task = self._image_flush_tasks.pop(target_key, None)
        if task is not None and not task.done():
            task.cancel()
        return "\n\n".join(parts)

    async def _flush_images_later(self, target_key: str) -> None:
        try:
            await asyncio.sleep(self.IMAGE_HOLD_SECONDS)
        except asyncio.CancelledError:
            return
        parts = self._pending_images.pop(target_key, [])
        self._image_flush_tasks.pop(target_key, None)
        if not parts:
            return
        markdown = "\n\n".join(parts)
        target_id = _as_int(target_key.split(":", 1)[-1])
        if target_key.startswith("chat:"):
            await self.send_to_chat(target_id, markdown)
        else:
            await self.send_image_reply(target_id, markdown)

    async def send_image_reply(self, topic_id: int, text: str) -> bool:
        """把图片作为单独一层回帖发到主题。"""
        if not self.token or not text:
            return False
        topic_id = int(topic_id or 0)
        if topic_id <= 0:
            return False
        now = time.time()
        if now - self._image_sent_at.get(topic_id, 0.0) < self.DUPLICATE_WINDOW:
            logger.info(
                "[DiscourseAdapter] 主题 %s 刚刚发过图片，忽略重复图片", topic_id
            )
            return False
        payload = {
            "topic_id": topic_id,
            "raw": text,
            "typing_duration_msecs": self.typing_duration,
        }
        try:
            await self.api.create_post(self.credential, payload)
        except DiscourseError as exc:
            logger.warning(
                "[DiscourseAdapter] 发送图片到主题 %s 失败：%s", topic_id, exc.message
            )
            return False
        self._image_sent_at[topic_id] = now
        logger.info("[DiscourseAdapter] 已发送图片到主题 %s", topic_id)
        return True

    async def send_to_chat(self, channel_id: int, text: str) -> bool:
        """把文本发到 Discourse 聊天频道（含直接消息）。"""
        if not self.token:
            logger.warning("[DiscourseAdapter] 未配置 token，无法发聊天消息。")
            return False
        channel_id = int(channel_id or 0)
        if channel_id <= 0:
            return False
        images = self.take_pending_images(f"chat:{channel_id}")
        if images:
            text = f"{text}\n\n{images}"
        if "<|" in text:
            logger.warning(
                "[DiscourseAdapter] 聊天消息含模型特殊标记，丢弃：%s", text[:80]
            )
            return False
        # 论坛单条聊天消息有长度上限，/help 这类指令输出要分段发
        for chunk in self._chat_chunks(text, 4000):
            try:
                await self.api.chat_send(self.credential, channel_id, chunk)
            except DiscourseError as exc:
                logger.warning(
                    "[DiscourseAdapter] 发送聊天消息到频道 %s 失败：%s",
                    channel_id,
                    exc.message,
                )
                return False
        logger.info("[DiscourseAdapter] 已发送聊天消息到频道 %s", channel_id)
        return True

    @staticmethod
    def _chat_chunks(text: str, limit: int) -> list[str]:
        """按行把长文本切成不超过 limit 的若干段。"""
        chunks: list[str] = []
        current = ""
        for line in text.splitlines(keepends=True):
            while len(line) > limit:
                if current:
                    chunks.append(current.rstrip())
                    current = ""
                chunks.append(line[:limit])
                line = line[limit:]
            if len(current) + len(line) > limit:
                chunks.append(current.rstrip())
                current = line
            else:
                current += line
        if current.strip():
            chunks.append(current.rstrip())
        return chunks or ([text] if text.strip() else [])

    def mark_skipped(self, key: str) -> None:
        """记录 LLM 已决定跳过某个目标（"topic:12" 或 "chat:3"）。"""
        key = str(key or "").strip()
        if not key:
            return
        now = time.time()
        self._skipped_targets = {
            name: stamp
            for name, stamp in self._skipped_targets.items()
            if now - stamp < self.SKIP_WINDOW
        }
        self._skipped_targets[key] = now

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
        images = self.take_pending_images(f"topic:{topic_id}")
        if images:
            text = f"{text}\n\n{images}"
        if self.store is not None and self.store.replied_within(
            topic_id, reply_to_post_number, self.DUPLICATE_WINDOW
        ):
            logger.info(
                "[DiscourseAdapter] 主题 %s 第 %s 楼刚刚已经回复过，忽略本次重复文本回复",
                topic_id,
                reply_to_post_number,
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
        if self.store is not None and reply_to_post_number:
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
