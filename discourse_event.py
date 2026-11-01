"""Discourse 论坛消息事件：把 LLM 的回复发回对应主题。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

if TYPE_CHECKING:
    from .adapter import DiscourseAdapter


class DiscourseMessageEvent(AstrMessageEvent):
    """论坛通知事件；send() 会把文本回复到对应的 Discourse 主题。"""

    def __init__(
        self,
        message_str: str,
        message_obj,
        platform_meta,
        session_id: str,
        adapter: "DiscourseAdapter",
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._adapter = adapter
        sid = str(session_id or "")
        self._topic_id = int(sid) if sid.isdigit() else 0
        self._chat_channel_id = 0
        if sid.startswith("chat:"):
            tail = sid.split(":", 1)[1]
            if tail.isdigit():
                self._chat_channel_id = int(tail)
        self._reply_to_post_number = 0

    @property
    def adapter(self) -> "DiscourseAdapter":
        return self._adapter

    @property
    def topic_id(self) -> int:
        return self._topic_id

    def set_reply_target(self, topic_id: int, post_number: int = 0) -> None:
        if topic_id:
            self._topic_id = int(topic_id)
        self._reply_to_post_number = int(post_number or 0)

    async def send(self, message: MessageChain | None) -> None:
        await super().send(message)

        if message is None:
            return
        message_type = str(getattr(message, "type", "") or "")
        if message_type in {"tool_call", "tool_direct_result"}:
            return

        text = (message.get_plain_text() or "").strip()
        images = await self._adapter.images_markdown(message)
        if not text and not images:
            return

        if self.get_extra("discourse_skip_reply", False):
            logger.info(
                "[DiscourseAdapter] LLM 决定不回复目标 %s，忽略本次文本",
                self._chat_channel_id or self._topic_id,
            )
            return

        if self._chat_channel_id:
            if images and not text:
                self._adapter.stage_images(f"chat:{self._chat_channel_id}", images)
                return
            combined = "\n\n".join(part for part in (text, images) if part)
            if await self._adapter.send_to_chat(
                self._chat_channel_id, combined, remember=bool(text)
            ):
                self.set_extra("discourse_tool_replied", True)
            return

        if not self._topic_id:
            logger.warning("[DiscourseAdapter] 事件没有主题 ID，无法把回复发回论坛。")
            return

        if images and not text:
            # 只有图片（如其它插件的表情包）：先暂存，等随下一条文字回复一起发
            self._adapter.stage_images(f"topic:{self._topic_id}", images)
            return

        if self.get_extra("discourse_tool_replied", False):
            logger.info(
                "[DiscourseAdapter] 已通过工具回复主题 %s，忽略重复文本回复", self._topic_id
            )
            return

        if images:
            text = f"{text}\n\n{images}"

        prefix = str(self.get_extra("discourse_reply_prefix") or "")
        if prefix and not text.lstrip().startswith(prefix):
            text = f"{prefix}\n\n{text}"

        await self._adapter.reply_to_topic(
            self._topic_id,
            text,
            reply_to_post_number=self._reply_to_post_number,
        )
        self.set_extra("discourse_tool_replied", True)

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        """论坛不支持流式输出，收集完整文本后一次性回复。"""
        chunks: list[str] = []
        images: list = []
        async for chain in generator:
            chunk = (chain.get_plain_text() or "").strip()
            if chunk:
                chunks.append(chunk)
            for comp in getattr(chain, "chain", None) or []:
                if type(comp).__name__ == "Image":
                    images.append(comp)
        text = "\n".join(chunks).strip()
        if text or images:
            message = MessageChain().message(text) if text else MessageChain()
            for comp in images:
                message.chain.append(comp)
            await self.send(message)
