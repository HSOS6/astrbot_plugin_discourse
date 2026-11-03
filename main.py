"""Discourse 论坛插件：AI 看论坛、读帖子、搜索，并用配置的 API Key 发帖、回帖。"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .adapter import _plugin_version, dedupe_mentions
from .discourse_api import DiscourseAPI, DiscourseError, html_to_text
from .render import (
    format_categories,
    format_digest,
    format_search,
    format_topic,
    format_topic_list,
    merge_posts,
    topic_url,
)
from .store import RETENTION_SECONDS, StateStore

PLUGIN_NAME = "astrbot_plugin_discourse"

NO_KEY_HINT = (
    "插件还没有配置论坛 Key。请先在插件配置中填写 token（Discourse User API Key）。"
)

_NOTIFICATION_TYPE_NAMES = {
    1: "提到了你",
    2: "回复了你",
    3: "引用了你",
    4: "编辑了你的帖子",
    5: "赞了你",
    6: "私信",
    7: "私信邀请",
    8: "接受了私信邀请",
    12: "授予徽章",
    15: "群组提及",
    16: "群组消息",
    19: "点赞汇总",
    20: "帖子已通过审核",
}


class DiscourseForumPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.site_url = str(config.get("site_url", "https://xinjianya.top")).rstrip("/")
        self.api = DiscourseAPI(
            site_url=self.site_url,
            api_base=str(config.get("api_base", "") or "").strip(),
            host_header=str(config.get("host_header", "") or "").strip(),
            timeout=self._cfg_int("request_timeout", 30, 5),
        )
        self.store = StateStore(StarTools.get_data_dir(PLUGIN_NAME))
        self._write_times: dict[str, list[float]] = {}
        self._topic_times: dict[str, list[float]] = {}
        self._categories_cache: tuple[float, dict[int, str]] = (0.0, {})
        self._auto_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self):
        if self._cfg_bool("auto_browse_enabled", True):
            self._auto_task = asyncio.create_task(self._auto_browse_loop())
            logger.info(
                "[Discourse] v%s 自动逛论坛已启动，间隔 %s 秒",
                _plugin_version() or "?",
                self._auto_interval(),
            )

    async def terminate(self):
        task = self._auto_task
        self._auto_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.api.close()

    # ------------------------------------------------------------------
    # 配置与基础工具
    # ------------------------------------------------------------------

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value if value is not None else default)

    def _cfg_int(self, key: str, default: int, minimum: int | None = None) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None and value < minimum:
            value = minimum
        return value

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(0.0, min(1.0, value))

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _categories_allowed(self) -> list[int]:
        raw = self.config.get("allowed_categories", [6])
        result: list[int] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                try:
                    result.append(int(item))
                except (TypeError, ValueError):
                    continue
        return result or [self._cfg_int("default_category", 6)]

    def _default_category(self) -> int:
        return self._cfg_int("default_category", 6)

    def _credential(self) -> dict | None:
        """从插件配置读取论坛 User API Key。

        与参考插件一致，只需要一个 token；如果该 Key 带有 client_id，
        再额外填写 api_client_id 即可。
        """
        token = self._cfg_str("token", "").strip()
        if not token:
            return None
        return {
            "api_key": token,
            "client_id": self._cfg_str("api_client_id", "").strip(),
            "username": "",
        }

    def _typing_duration(self) -> int:
        # Discourse fast_typer 阈值默认 3000ms，低于阈值会进审核甚至自动禁言
        return max(3001, self._cfg_int("typing_duration_msecs", 20000))

    @staticmethod
    def _user_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            sender = getattr(event, "sender", None)
            return str(getattr(sender, "user_id", "unknown"))

    @staticmethod
    def _parse_topic_id(value) -> int:
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"/t/(?:[^/]+/)?(\d+)", text)
        if match:
            return int(match.group(1))
        return 0

    @staticmethod
    def _parse_post_id(value) -> int:
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"/p/(\d+)", text)
        if match:
            return int(match.group(1))
        return 0

    @staticmethod
    def _mention_tokens(mentions: str) -> list[str]:
        """把 "alice,bob" 拆成合法用户名候选（最多 5 个）。"""
        tokens = [
            item.strip().lstrip("@")
            for item in re.split(r"[,，;；\s]+", str(mentions or ""))
            if item.strip()
        ]
        unique: list[str] = []
        for token in tokens:
            if token not in unique and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", token):
                unique.append(token)
        return unique[:5]

    async def _resolve_mentions(
        self, mentions: str, existing_text: str = ""
    ) -> tuple[str, list[str]]:
        """校验 @ 的用户名是否存在，返回（@ 后缀, 找不到的用户名）。

        existing_text 里已经出现过的用户名不再追加，避免同一句里 @ 两次。
        """
        tokens = self._mention_tokens(mentions)
        if not tokens:
            return "", []
        existing = {
            name.lower()
            for name in re.findall(r"@([A-Za-z0-9_.-]{1,60})", existing_text or "")
        }
        tokens = [name for name in tokens if name.lower() not in existing]
        if not tokens:
            return "", []
        valid: list[str] = []
        missing: list[str] = []
        for name in tokens:
            try:
                await self._read(self.api.user, name)
            except DiscourseError as exc:
                if exc.status == 404:
                    missing.append(name)
                    continue
                # 网络或权限问题无法判定存在性，按可用处理
            valid.append(name)
        suffix = ("\n\n" + " ".join(f"@{name}" for name in valid)) if valid else ""
        return suffix, missing

    def _rate_ok(self, user_id: str) -> bool:
        now = time.time()
        recent = [stamp for stamp in self._write_times.get(user_id, []) if now - stamp < 3600]
        self._write_times[user_id] = recent
        return len(recent) < max(1, self._cfg_int("max_writes_per_hour", 6))

    def _record_write(self, user_id: str) -> None:
        self._write_times.setdefault(user_id, []).append(time.time())

    def _topic_rate_ok(self, user_id: str) -> bool:
        now = time.time()
        recent = [stamp for stamp in self._topic_times.get(user_id, []) if now - stamp < 86400]
        self._topic_times[user_id] = recent
        return len(recent) < max(1, self._cfg_int("auto_post_daily_limit", 2))

    def _record_topic(self, user_id: str) -> None:
        self._topic_times.setdefault(user_id, []).append(time.time())

    @staticmethod
    def _parse_iso(value) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    @classmethod
    def _is_stale(cls, value) -> bool:
        """发布时间超过三天的内容不再参与自动回复。"""
        created = cls._parse_iso(value)
        return bool(created) and time.time() - created > RETENTION_SECONDS

    def _validate_content(self, content: str) -> str | None:
        text = (content or "").strip()
        if "<|" in text:
            return "正文里混入了模型特殊标记（如 <|end|>），请重新组织语言后再发。"
        minimum = max(1, self._cfg_int("min_content_chars", 8))
        maximum = max(minimum, self._cfg_int("max_content_chars", 8000))
        if len(text) < minimum:
            return f"正文太短，至少需要 {minimum} 个字符。"
        if len(text) > maximum:
            return f"正文太长，最多 {maximum} 个字符。"
        return None

    def _validate_title(self, title: str) -> str | None:
        text = (title or "").strip()
        if len(text) < 2:
            return "标题太短，至少需要 2 个字符。"
        maximum = max(2, self._cfg_int("max_title_chars", 120))
        if len(text) > maximum:
            return f"标题太长，最多 {maximum} 个字符。"
        return None

    async def _category_names(self, credential: dict | None = None) -> dict[int, str]:
        cached_at, mapping = self._categories_cache
        if mapping and time.time() - cached_at < 3600:
            return mapping
        credential = credential if credential is not None else self._credential()
        try:
            data = await self.api.categories(credential=credential)
        except DiscourseError:
            return mapping
        new_mapping: dict[int, str] = {}
        for category in (data.get("category_list") or {}).get("categories") or []:
            try:
                new_mapping[int(category.get("id"))] = str(category.get("name", ""))
            except (TypeError, ValueError):
                continue
        self._categories_cache = (time.time(), new_mapping)
        return new_mapping

    async def _read(self, method, *args, **kwargs):
        """读接口：优先带配置的 API Key（可读私有类别），401 时退回匿名。"""
        credential = self._credential()
        try:
            return await method(*args, credential=credential, **kwargs)
        except DiscourseError as exc:
            if exc.status == 401 and credential:
                return await method(*args, credential=None, **kwargs)
            raise

    def _truncate(self, text: str, limit: int = 4000) -> str:
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 20)] + "\n……内容过长，已截断"

    # ------------------------------------------------------------------
    # LLM 工具：读论坛
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_list_categories")
    async def discourse_list_categories(self, event: AstrMessageEvent) -> str:
        """列出 Discourse 论坛的所有类别及 ID。发新主题时需要用类别 ID。

        Args:
        """
        try:
            data = await self._read(self.api.categories)
        except DiscourseError as exc:
            return f"读取类别失败：{exc.message}"
        names = await self._category_names(self._credential())
        self._categories_cache = (time.time(), names)
        return format_categories(data, self._categories_allowed())

    @filter.llm_tool(name="discourse_browse_topics")
    async def discourse_browse_topics(
        self,
        event: AstrMessageEvent,
        category_id: int = 0,
        sort: str = "latest",
        page: int = 0,
    ) -> str:
        """浏览 Discourse 论坛的主题列表，可按类别、最新或热门排序。

        Args:
            category_id(number): 可选，只看指定类别 ID；0 表示全部类别。
            sort(string): 可选，latest（最新）、top_weekly（本周热门）、top_monthly（本月热门）。
            page(number): 可选，页码，从 0 开始。
        """
        try:
            category_id = int(category_id or 0)
        except (TypeError, ValueError):
            category_id = 0
        try:
            page = max(0, int(page or 0))
        except (TypeError, ValueError):
            page = 0
        try:
            if category_id:
                data = await self._read(self.api.category, category_id, page=page)
            elif str(sort).startswith("top"):
                period = "weekly" if "weekly" in str(sort) else "monthly"
                data = await self._read(self.api.top, period=period, page=page)
            else:
                data = await self._read(self.api.latest, page=page)
        except DiscourseError as exc:
            return f"读取主题列表失败：{exc.message}"
        names = await self._category_names(self._credential())
        return format_topic_list(
            data, self.site_url, names, self._cfg_int("list_limit", 15, 1)
        )

    @filter.llm_tool(name="discourse_read_topic")
    async def discourse_read_topic(
        self, event: AstrMessageEvent, topic_id: str, max_posts: int = 0
    ) -> str:
        """阅读一个 Discourse 主题及其楼层内容。传入主题 ID 或主题链接。

        Args:
            topic_id(string): 主题 ID 或主题链接，例如 9 或 https://example.com/t/9。
            max_posts(number): 可选，最多读取多少楼；0 表示使用插件默认上限。
        """
        parsed_id = self._parse_topic_id(topic_id)
        if not parsed_id:
            return "无法识别主题 ID，请传入数字 ID 或完整主题链接。"
        limit = max_posts if max_posts and int(max_posts) > 0 else self._cfg_int("max_topic_posts", 20, 1)
        try:
            data = await self._read(self.api.topic, parsed_id)
            await self._load_posts(data, int(limit))
        except DiscourseError as exc:
            return f"读取主题失败：{exc.message}"
        self._drop_blocked_posts(data)
        category_names = await self._category_names(self._credential())
        return format_topic(
            data,
            self.site_url,
            category_names,
            max_posts=int(limit),
            max_post_chars=self._cfg_int("max_post_chars", 2000, 100),
        )

    async def _load_posts(self, data: dict, limit: int) -> list[dict]:
        stream = (data.get("post_stream") or {}).get("stream") or []
        existing = (data.get("post_stream") or {}).get("posts") or []
        if not stream:
            return existing
        wanted = [int(pid) for pid in stream[:limit]]
        known = {post.get("id") for post in existing}
        missing = [pid for pid in wanted if pid not in known]
        extra: list[dict] = []
        for start in range(0, len(missing), 20):
            chunk = missing[start : start + 20]
            if not chunk:
                continue
            result = await self._read(self.api.topic_posts, data.get("id"), chunk)
            extra.extend((result.get("post_stream") or {}).get("posts") or [])
        merged = merge_posts(data, extra)
        data.setdefault("post_stream", {})["posts"] = merged
        return merged

    async def _topic_preview(self, topic_id: int, reply_count: int) -> dict:
        """取主题主楼和最近几条回复，自动逛论坛推送时不能只看主楼。"""
        if topic_id <= 0 or reply_count <= 0:
            return {}
        try:
            data = await self._read(self.api.topic, topic_id)
        except DiscourseError:
            return {}

        stream_data = data.get("post_stream") or {}
        stream = [int(pid) for pid in stream_data.get("stream") or []]
        known = {
            int(item.get("id")): item
            for item in stream_data.get("posts") or []
            if item.get("id")
        }
        wanted: list[int] = []
        if stream:
            wanted.append(stream[0])
            wanted.extend(
                pid for pid in stream[-reply_count:] if pid != stream[0]
            )
        missing = [pid for pid in wanted if pid not in known]
        if missing:
            try:
                extra = await self._read(self.api.topic_posts, topic_id, missing)
                for item in (extra.get("post_stream") or {}).get("posts") or []:
                    if item.get("id"):
                        known[int(item["id"])] = item
            except DiscourseError:
                pass

        preview = {"first": "", "replies": []}
        for pid in wanted:
            post = known.get(pid)
            if not post:
                continue
            if self.store.is_blocked(
                post.get("user_id"), post.get("username") or ""
            ):
                continue
            number = int(post.get("post_number") or 0)
            text = html_to_text(str(post.get("cooked") or post.get("raw") or ""))
            text = " ".join(text.split())
            author = post.get("username") or "未知"
            if number == 1:
                if len(text) > 200:
                    text = text[:200] + "…"
                preview["first"] = f"@{author}：{text}"
            else:
                if len(text) > 120:
                    text = text[:120] + "…"
                preview["replies"].append(f"#{number} @{author}：{text}")
        return preview

    def _drop_blocked_posts(self, data: dict) -> None:
        """被拉黑用户的内容不展示给 AI。"""
        posts = (data.get("post_stream") or {}).get("posts") or []
        blocked_keys = self.store.blocked_keys()
        blocked_names = self.store.blocked_names()
        if not posts or (not blocked_keys and not blocked_names):
            return
        kept = [
            post
            for post in posts
            if str(post.get("user_id") or "").lower() not in blocked_keys
            and str(post.get("username") or "").lower() not in blocked_names
        ]
        data["post_stream"]["posts"] = kept

    @filter.llm_tool(name="discourse_search_forum")
    async def discourse_search_forum(
        self, event: AstrMessageEvent, query: str, page: int = 0
    ) -> str:
        """在 Discourse 论坛中搜索主题和楼层。

        Args:
            query(string): 搜索关键词。
            page(number): 可选，页码，从 0 开始。
        """
        text = str(query or "").strip()
        if not text:
            return "搜索关键词不能为空。"
        try:
            page = max(0, int(page or 0))
        except (TypeError, ValueError):
            page = 0
        try:
            data = await self._read(self.api.search, text, page=page)
        except DiscourseError as exc:
            return f"搜索失败：{exc.message}"
        names = await self._category_names(self._credential())
        result = format_search(data, text, self.site_url, names)
        if "没有找到" not in result:
            return result
        # Discourse 中文分词较弱，站内搜索为空时回退到最近主题的标题/摘要匹配
        try:
            latest = await self._read(self.api.latest, page=0)
        except DiscourseError:
            return result
        keyword = text.lower()
        topics = [
            topic
            for topic in (latest.get("topic_list") or {}).get("topics") or []
            if keyword in str(topic.get("title", "")).lower()
            or keyword in str(topic.get("excerpt", "")).lower()
        ]
        if not topics:
            return result
        return "站内搜索无结果，以下为最近主题中包含该关键词的内容：\n" + format_topic_list(
            {"topic_list": {"topics": topics}}, self.site_url, names, 10
        )

    # ------------------------------------------------------------------
    # LLM 工具：搜索用户（@ 之前先确认用户名存在）
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_search_user")
    async def discourse_search_user(
        self, event: AstrMessageEvent, keyword: str = ""
    ) -> str:
        """在论坛中搜索用户，确认要 @ 的用户名是否存在。@ 别人之前应先调用它。

        Args:
            keyword(string): 用户名或昵称关键词，至少 2 个字符。
        """
        term = str(keyword or "").strip().lstrip("@")
        if len(term) < 2:
            return "关键词太短，至少需要 2 个字符。"

        users: list[dict] = []
        try:
            data = await self._read(self.api.search_users, term)
            users = list(data.get("users") or [])
        except DiscourseError:
            users = []

        if not users:
            # /u/search/users 被站点关闭时，退化为精确检查该用户名
            try:
                profile = await self._read(self.api.user, term)
            except DiscourseError as exc:
                if exc.status == 404:
                    return f"没有找到用户名 {term}。"
                return f"搜索用户失败：{exc.message}"
            users = [profile.get("user") or {"username": term}]

        lines = [f"与「{term}」匹配的论坛用户："]
        for user in users[:10]:
            username = str(user.get("username") or "").strip()
            if not username:
                continue
            name = str(user.get("name") or "").strip()
            lines.append(f"- @{username}" + (f"（{name}）" if name else ""))
        if len(lines) == 1:
            return f"没有找到用户名 {term}。"
        lines.append("确认无误后，在 discourse_publish_reply 的 mentions 参数里填这些用户名。")
        return "\n".join(lines)

    async def _current_username(self) -> str:
        credential = self._credential()
        if not credential:
            return ""
        try:
            user = await self.api.current_user(credential)
        except DiscourseError:
            return ""
        return str(user.get("username") or "")

    @filter.llm_tool(name="discourse_user_profile")
    async def discourse_user_profile(
        self, event: AstrMessageEvent, username: str = ""
    ) -> str:
        """查看自己或他人的论坛资料：等级、注册时间、简介、关注数等。

        Args:
            username(string): 用户名；留空表示查看当前配置的论坛账号。
        """
        name = str(username or "").strip().lstrip("@")
        if not name:
            name = await self._current_username()
            if not name:
                return NO_KEY_HINT

        try:
            data = await self._read(self.api.user_profile, name)
        except DiscourseError as exc:
            if exc.status == 404:
                return f"论坛没有用户名 {name}。"
            return f"读取用户资料失败：{exc.message}"

        user = data.get("user") or {}
        lines = [f"论坛资料：@{user.get('username') or name}"]
        if user.get("name"):
            lines.append(f"昵称：{user['name']}")
        if user.get("title"):
            lines.append(f"头衔：{user['title']}")
        if user.get("trust_level") is not None:
            lines.append(f"信任等级：{user['trust_level']}")
        if user.get("created_at"):
            lines.append(f"注册于：{user['created_at'][:10]}")
        if user.get("last_seen_at"):
            lines.append(f"最近活跃：{user['last_seen_at'][:16].replace('T', ' ')}")
        if user.get("bio_raw") or user.get("bio_excerpt"):
            bio = html_to_text(str(user.get("bio_raw") or user.get("bio_excerpt") or ""))
            if bio:
                lines.append(f"简介：{bio[:300]}")

        credential = self._credential()
        if credential:
            for kind, label in (("following", "关注"), ("followers", "粉丝")):
                try:
                    data_list = await self.api.follow_list(credential, name, kind)
                except DiscourseError:
                    continue
                users = data_list.get("data") if isinstance(data_list.get("data"), list) else []
                if not isinstance(users, list):
                    users = []
                if users:
                    names = ", ".join(
                        "@" + str(item.get("username"))
                        for item in users[:10]
                        if item.get("username")
                    )
                    lines.append(f"{label}（{len(users)}）：{names}")
        return "\n".join(lines)

    @filter.llm_tool(name="discourse_follow_user")
    async def discourse_follow_user(
        self, event: AstrMessageEvent, username: str = "", action: str = "follow"
    ) -> str:
        """关注或取关某个论坛用户（论坛已安装 discourse-follow 插件）。

        Args:
            username(string): 要关注/取关的用户名。
            action(string): follow（默认）或 unfollow（取关）。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        name = str(username or "").strip().lstrip("@")
        if not name:
            return "请提供要关注/取关的用户名。"
        follow = str(action or "").strip().lower() not in (
            "unfollow",
            "cancel",
            "取关",
            "取消关注",
        )
        if name == await self._current_username():
            return "不能关注自己。"
        try:
            await self.api.follow_user(credential, name, follow)
        except DiscourseError as exc:
            if exc.status == 404:
                return f"论坛没有用户名 {name}，或论坛未启用关注功能。"
            return f"{'关注' if follow else '取关'}失败：{exc.message}"
        return f"已{'关注' if follow else '取关'} @{name}。"

    @filter.llm_tool(name="discourse_follow_list")
    async def discourse_follow_list(
        self, event: AstrMessageEvent, username: str = "", list_type: str = "following"
    ) -> str:
        """查看关注列表或粉丝列表。

        Args:
            username(string): 用户名；留空表示当前配置的账号。
            list_type(string): following（我关注的人，默认）或 followers（关注我的人）。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        name = str(username or "").strip().lstrip("@") or await self._current_username()
        if not name:
            return NO_KEY_HINT
        try:
            data = await self.api.follow_list(credential, name, list_type)
        except DiscourseError as exc:
            return f"读取关注列表失败：{exc.message}"
        users = data.get("data")
        if isinstance(data.get("users"), list):
            users = data["users"]
        label = "粉丝" if str(list_type).strip().lower() in ("followers", "粉丝") else "关注"
        if not isinstance(users, list) or not users:
            return f"@{name} 的{label}列表为空。"
        lines = [f"@{name} 的{label}（{len(users)}）："]
        for item in users[:30]:
            uname = str(item.get("username") or "")
            if not uname:
                continue
            title = str(item.get("name") or "")
            lines.append(f"- @{uname}" + (f"（{title}）" if title else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # LLM 工具：查询当前论坛身份
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_my_account")
    async def discourse_my_account(self, event: AstrMessageEvent) -> str:
        """查询插件当前配置的 Discourse 身份信息。

        Args:
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        try:
            user = await self.api.current_user(credential)
        except DiscourseError as exc:
            return f"读取论坛账号失败：{exc.message}"
        return (
            f"论坛账号：@{user.get('username', '')}"
            f"（ID {user.get('id')}，信任等级 {user.get('trust_level')}）\n"
            f"论坛地址：{self.site_url}"
        )

    # ------------------------------------------------------------------
    # LLM 工具：拉黑名单
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_block_user")
    async def discourse_block_user(
        self, event: AstrMessageEvent, user: str = "", reason: str = ""
    ) -> str:
        """把一个论坛用户加入黑名单：之后阅读主题会隐藏他的内容，也不会自动回复他。

        Args:
            user(string): 用户 ID（数字）或用户名（可带 @）。
            reason(string): 可选，拉黑原因，仅记录用。
        """
        text = str(user or "").strip().lstrip("@")
        if not text:
            return "请提供要拉黑的用户 ID 或用户名。"
        name = "" if text.isdigit() else text
        self.store.block(text, name)
        suffix = f"（原因：{reason}）" if str(reason or "").strip() else ""
        return f"已拉黑 {user}{suffix}。之后不会显示或自动回复该用户的内容。"

    @filter.llm_tool(name="discourse_unblock_user")
    async def discourse_unblock_user(
        self, event: AstrMessageEvent, user: str = ""
    ) -> str:
        """把论坛用户移出黑名单。

        Args:
            user(string): 用户 ID 或用户名（可带 @）。
        """
        text = str(user or "").strip().lstrip("@").lower()
        if not text:
            return "请提供要解除拉黑的用户 ID 或用户名。"
        if self.store.unblock(text):
            return f"已解除对 {user} 的拉黑。"
        for entry in self.store.blocked_entries():
            if str(entry.get("username") or "").lower() == text:
                self.store.unblock(str(entry.get("key") or ""))
                return f"已解除对 {user} 的拉黑。"
        return f"{user} 不在黑名单里。"

    @filter.llm_tool(name="discourse_block_list")
    async def discourse_block_list(self, event: AstrMessageEvent) -> str:
        """查看当前论坛黑名单。

        Args:
        """
        entries = self.store.blocked_entries()
        if not entries:
            return "黑名单为空。"
        lines = [
            f"- {item.get('key')}"
            + (f"（@{item['username']}）" if item.get("username") else "")
            for item in entries
        ]
        return "黑名单：\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # LLM 工具：写论坛
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_publish_topic")
    async def discourse_publish_topic(
        self,
        event: AstrMessageEvent,
        title: str,
        content: str,
        category_id: int = 0,
    ) -> str:
        """使用插件配置的论坛身份发布新主题。正文支持 Markdown。

        Args:
            title(string): 主题标题。
            content(string): 主题正文，Markdown 格式；请整理成论坛帖子，不要原样转发聊天指令。
            category_id(number): 可选，类别 ID；0 表示使用插件默认类别。
        """
        try:
            category_id = int(category_id or 0)
        except (TypeError, ValueError):
            category_id = 0
        return await self._publish_topic(event, str(title or ""), str(content or ""), category_id)

    @filter.llm_tool(name="discourse_publish_reply")
    async def discourse_publish_reply(
        self,
        event: AstrMessageEvent,
        topic_id: str = "",
        content: str = "",
        mentions: str = "",
    ) -> str:
        """使用插件配置的论坛身份回复某个主题。正文支持 Markdown。

        Args:
            topic_id(string): 主题 ID 或主题链接。
            content(string): 回复正文，Markdown 格式。
            mentions(string): 可选，要 @ 的用户名，多个用逗号分隔（例如 "alice,bob"）。
        """
        if not str(content or "").strip():
            return "缺少 content 参数。请重新调用 discourse_publish_reply，同时传入 topic_id 和 content。"
        parsed_id = self._parse_topic_id(topic_id)
        if not parsed_id:
            return "无法识别主题 ID，请传入数字 ID 或完整主题链接。"
        return await self._publish_reply(
            event, parsed_id, str(content or ""), mentions=str(mentions or "")
        )

    @filter.llm_tool(name="discourse_publish_comment")
    async def discourse_publish_comment(
        self,
        event: AstrMessageEvent,
        topic_id: str,
        post_number: int,
        content: str,
        mentions: str = "",
    ) -> str:
        """使用插件配置的论坛身份评论主题中的某一楼（楼中楼回复）。

        Args:
            topic_id(string): 主题 ID 或主题链接。
            post_number(number): 要评论的楼层号，例如 1 表示主楼。
            content(string): 评论正文，Markdown 格式。
            mentions(string): 可选，要 @ 的用户名，多个用逗号分隔。
        """
        parsed_id = self._parse_topic_id(topic_id)
        if not parsed_id:
            return "无法识别主题 ID，请传入数字 ID 或完整主题链接。"
        try:
            floor = int(post_number)
        except (TypeError, ValueError):
            return "楼层号必须是数字。"
        if floor < 1:
            return "楼层号必须大于 0。"
        return await self._publish_reply(
            event,
            parsed_id,
            str(content or ""),
            post_number=floor,
            mentions=str(mentions or ""),
        )

    @filter.llm_tool(name="discourse_send_chat_message")
    async def discourse_send_chat_message(
        self, event: AstrMessageEvent, channel_id: str = "", content: str = ""
    ) -> str:
        """在 Discourse 聊天频道（含直接消息）发一条消息。

        Args:
            channel_id(string): 聊天频道 ID，收到聊天通知时会给出。
            content(string): 消息正文，纯文本或 Markdown。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        try:
            cid = int(str(channel_id or "").strip())
        except (TypeError, ValueError):
            return "无法识别聊天频道 ID，请传入数字。"
        if cid <= 0:
            return "无法识别聊天频道 ID，请传入数字。"
        text = str(content or "").strip()
        if not text:
            return "缺少 content 参数。"
        if "<|" in text:
            return "消息里混入了模型特殊标记，请重新组织语言后再发。"
        maximum = max(1, self._cfg_int("max_content_chars", 8000))
        if len(text) > maximum:
            return f"消息太长，最多 {maximum} 个字符。"
        user_id = self._user_id(event)
        if not self._rate_ok(user_id):
            return "已达到每小时写操作上限，请稍后再试。"
        adapter = getattr(event, "adapter", None)
        if hasattr(adapter, "take_pending_images"):
            images = adapter.take_pending_images(f"chat:{cid}")
            if images:
                text = f"{text}\n\n{images}"
        try:
            await self.api.chat_send(credential, cid, text)
        except DiscourseError as exc:
            return f"发送失败：{exc.message}"
        self._record_write(user_id)
        event.set_extra("discourse_tool_replied", True)
        return f"已在聊天频道 {cid} 发送消息。"

    @filter.llm_tool(name="discourse_skip_reply")
    async def discourse_skip_reply(self, event: AstrMessageEvent, reason: str = "") -> str:
        """收到论坛通知后决定不回复时调用，避免把思考文字误发成帖子。

        Args:
            reason(string): 可选，不回复的原因，仅用于日志。
        """
        try:
            topic_id = int(event.get_extra("discourse_topic_id") or 0)
        except (TypeError, ValueError):
            topic_id = 0
        try:
            chat_id = int(event.get_extra("discourse_chat_channel_id") or 0)
        except (TypeError, ValueError):
            chat_id = 0
        if chat_id > 0:
            target_key = f"chat:{chat_id}"
        elif topic_id > 0:
            target_key = f"topic:{topic_id}"
        else:
            target_key = ""
        event.set_extra("discourse_skip_reply", True)
        adapter = getattr(event, "adapter", None)
        if target_key and hasattr(adapter, "mark_skipped"):
            adapter.mark_skipped(target_key)
        logger.info(
            "[Discourse] 跳过回复 %s：%s",
            target_key or "未知目标",
            str(reason or "").strip() or "未说明",
        )
        return "已跳过，本次不会在论坛发帖。请输出一句简短确认即可，系统会忽略这段文字。"

    @filter.llm_tool(name="discourse_delete_post")
    async def discourse_delete_post(
        self,
        event: AstrMessageEvent,
        post_id: str = "",
        topic_id: str = "",
        post_number: int = 0,
    ) -> str:
        """删除插件账号自己发布的帖子或楼层。删除主楼（第 1 楼）会连同整个主题一起删除。

        Args:
            post_id(string): 可选，帖子 ID（数字），也支持 /p/123 形式的短链接。
            topic_id(string): 可选，主题 ID 或主题链接；没有 post_id 时用它定位。
            post_number(number): 楼层号，配合 topic_id 使用，1 表示主楼。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT

        target_id = self._parse_post_id(post_id)
        topic_hint = ""
        if not target_id:
            parsed_topic = self._parse_topic_id(topic_id)
            if not parsed_topic:
                return "无法识别要删除的帖子，请传入帖子 ID，或 topic_id + post_number。"
            try:
                floor = int(post_number or 0)
            except (TypeError, ValueError):
                return "楼层号必须是数字。"
            if floor < 1:
                return "楼层号必须大于 0；要删除主楼请传 post_number=1。"
            try:
                found = await self.api.post_by_number(parsed_topic, floor, credential)
            except DiscourseError as exc:
                return f"找不到该楼层：{exc.message}"
            target_id = int(found.get("id") or 0)
            if not target_id:
                return f"主题 {parsed_topic} 的第 {floor} 楼不存在或已删除。"
            topic_hint = f"主题 {parsed_topic} 第 {floor} 楼"

        try:
            post = await self.api.post(target_id, credential)
        except DiscourseError as exc:
            return f"读取帖子失败：{exc.message}"
        author = str(post.get("username") or "")
        try:
            me = await self.api.current_user(credential)
        except DiscourseError as exc:
            return f"无法确认当前论坛身份，已放弃删除：{exc.message}"
        my_name = str(me.get("username") or "")
        if author and my_name and author != my_name:
            return f"只能删除自己发布的帖子：这条帖子属于 @{author}。"
        if not self._rate_ok(self._user_id(event)):
            return "已达到每小时写操作上限，请稍后再试。"

        try:
            await self.api.delete_post(credential, target_id)
        except DiscourseError as exc:
            return f"删除失败：{exc.message}"

        self._record_write(self._user_id(event))
        was_first = int(post.get("post_number") or 0) == 1
        target_topic = post.get("topic_id") or topic_hint
        suffix = "，该主题已整体删除" if was_first else ""
        return f"已删除帖子 #{target_id}（{topic_hint or f'主题 {target_topic}'}，作者 @{author}）{suffix}。"

    @filter.llm_tool(name="discourse_like_post")
    async def discourse_like_post(
        self,
        event: AstrMessageEvent,
        topic_id: str = "",
        post_number: int = 1,
        post_id: str = "",
        action: str = "like",
    ) -> str:
        """给某个楼层点赞或取消点赞。主楼和回复都可以点赞。

        Args:
            topic_id(string): 主题 ID 或主题链接；配合 post_number 定位楼层。
            post_number(number): 楼层号，1 表示主楼，默认 1。
            post_id(string): 可选，直接给帖子 ID（数字）或 /p/123 链接；优先于 topic_id。
            action(string): like（默认）或 unlike（取消点赞）。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT

        target_id = self._parse_post_id(post_id)
        floor = 0
        if not target_id:
            parsed_topic = self._parse_topic_id(topic_id)
            if not parsed_topic:
                return "无法识别要操作的楼层，请传入 post_id，或 topic_id + post_number。"
            try:
                floor = int(post_number or 1)
            except (TypeError, ValueError):
                return "楼层号必须是数字。"
            if floor < 1:
                return "楼层号必须大于 0。"
            try:
                found = await self.api.post_by_number(parsed_topic, floor, credential)
            except DiscourseError as exc:
                return f"找不到该楼层：{exc.message}"
            target_id = int(found.get("id") or 0)
            if not target_id:
                return f"主题 {parsed_topic} 的第 {floor} 楼不存在或已删除。"

        if action.strip().lower() in ("unlike", "cancel", "取消", "取消点赞"):
            try:
                await self.api.unlike_post(credential, target_id)
            except DiscourseError as exc:
                if exc.status == 404:
                    return "本来就没有点赞过这条内容。"
                return f"取消点赞失败：{exc.message}"
            return f"已取消点赞（帖子 #{target_id}）。"

        if not self._rate_ok(self._user_id(event)):
            return "已达到每小时写操作上限，请稍后再试。"
        if event.get_extra("discourse_auto_engage"):
            if not self._cfg_bool("auto_browse_like_enabled", True):
                return "本轮自动逛论坛未开启点赞。"
            liked = list(event.get_extra("discourse_auto_liked") or [])
            like_limit = max(1, self._cfg_int("auto_browse_max_likes", 2))
            if len(liked) >= like_limit:
                return "本轮点赞数量已达上限，不再点赞。"
        try:
            await self.api.like_post(credential, target_id)
        except DiscourseError as exc:
            if exc.status == 422 and (
                "already" in exc.message.lower() or "已经" in exc.message
            ):
                return "这条内容你已经点过赞了。"
            return f"点赞失败：{exc.message}"

        self._record_write(self._user_id(event))
        if event.get_extra("discourse_auto_engage"):
            liked = list(event.get_extra("discourse_auto_liked") or [])
            liked.append(target_id)
            event.set_extra("discourse_auto_liked", liked)
        count = ""
        try:
            post = await self.api.post(target_id, credential)
        except DiscourseError:
            post = {}
        for summary in post.get("actions_summary") or []:
            if int(summary.get("id") or 0) == 2:
                count = f"，当前共 {summary.get('count', 0)} 个赞"
                break
        return f"已点赞（帖子 #{target_id}{count}）。"

    @filter.llm_tool(name="discourse_check_notifications")
    async def discourse_check_notifications(
        self, event: AstrMessageEvent, limit: int = 10, mark_read: bool = False
    ) -> str:
        """查看论坛通知（@提及、回复、引用、点赞、私信等）。

        Args:
            limit(number): 最多返回几条，默认 10。
            mark_read(bool): 是否顺便把这些通知标记为已读，默认 False。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        try:
            limit = max(1, min(20, int(limit or 10)))
        except (TypeError, ValueError):
            limit = 10
        try:
            data = await self.api.notifications(credential, limit=limit)
        except DiscourseError as exc:
            return f"读取通知失败：{exc.message}"
        items = list(data.get("notifications") or [])[:limit]
        if not items:
            return "论坛没有新通知。"
        lines = [f"最近 {len(items)} 条论坛通知："]
        for item in items:
            kind = _NOTIFICATION_TYPE_NAMES.get(
                int(item.get("notification_type") or 0), "其他"
            )
            who = str((item.get("data") or {}).get("display_username") or "有人")
            title = html_to_text(str(item.get("fancy_title") or "")).strip() or "未命名主题"
            topic_id = item.get("topic_id")
            url = self.site_url + f"/t/{topic_id}" if topic_id else ""
            flag = "未读" if not item.get("read") else "已读"
            lines.append(f"- [{kind}] @{who}：《{title}》{url}（{flag}）")
        lines.append(
            "需要回复某条通知，用 discourse_publish_reply(topic_id=...)，"
            "楼中楼用 discourse_publish_comment(topic_id=..., post_number=...)。"
        )
        if mark_read:
            try:
                await self.api.mark_notifications_read(credential)
                lines.append("已把这些通知标记为已读。")
            except DiscourseError as exc:
                lines.append(f"标记已读失败：{exc.message}")
        return "\n".join(lines)

    @filter.llm_tool(name="discourse_list_private_messages")
    async def discourse_list_private_messages(
        self, event: AstrMessageEvent, page: int = 0
    ) -> str:
        """列出论坛私信会话（不含公开主题）。

        Args:
            page(number): 页码，从 0 开始。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        name = await self._current_username()
        if not name:
            return NO_KEY_HINT
        try:
            page = max(0, int(page or 0))
        except (TypeError, ValueError):
            page = 0
        try:
            data = await self.api.private_messages(name, page, credential)
        except DiscourseError as exc:
            return f"读取私信列表失败：{exc.message}"
        topics = list((data.get("topic_list") or {}).get("topics") or [])
        if not topics:
            return "没有私信会话。"
        lines = [f"最近 {len(topics)} 个私信会话："]
        for topic in topics[:15]:
            title = str(topic.get("title") or "未命名")
            topic_id = topic.get("id")
            when = str(topic.get("last_posted_at") or "")[:16].replace("T", " ")
            lines.append(f"- [{topic_id}] {title}（{when}，{topic.get('posts_count', 0)} 楼）")
        lines.append("查看某个会话用 discourse_read_topic(topic_id=...)，回复用 discourse_publish_reply。")
        return "\n".join(lines)

    @filter.llm_tool(name="discourse_send_private_message")
    async def discourse_send_private_message(
        self,
        event: AstrMessageEvent,
        content: str = "",
        recipients: str = "",
        title: str = "",
        topic_id: str = "",
    ) -> str:
        """发送论坛私信；已有会话时传 topic_id 直接回复。

        Args:
            content(string): 私信正文，Markdown 格式。
            recipients(string): 收件人用户名，多个用逗号分隔；回复已有会话时可留空。
            title(string): 新建私信时的标题；回复已有会话可留空。
            topic_id(string): 可选，已有私信会话的主题 ID 或链接。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        text = str(content or "").strip()
        error = self._validate_content(text)
        if error:
            return error
        user_id = self._user_id(event)
        if not self._rate_ok(user_id):
            return "已达到每小时写操作上限，请稍后再试。"

        reply_topic = self._parse_topic_id(topic_id)
        if reply_topic:
            payload = {
                "topic_id": reply_topic,
                "raw": text,
                "typing_duration_msecs": self._typing_duration(),
            }
        else:
            names = self._mention_tokens(recipients) or [
                item.strip().lstrip("@")
                for item in re.split(r"[,，;；\s]+", str(recipients or ""))
                if item.strip()
            ]
            if not names:
                return "请提供收件人用户名（recipients）。"
            unknown: list[str] = []
            for name in names:
                try:
                    await self._read(self.api.user, name)
                except DiscourseError as exc:
                    if exc.status == 404:
                        unknown.append(name)
            if unknown:
                return f"找不到这些用户：{'、'.join(unknown)}。可先用 discourse_search_user 搜索。"
            payload = {
                "title": str(title or "").strip() or "来自 AstrBot 的私信",
                "raw": text,
                "archetype": "private_message",
                "target_recipients": ",".join(names),
                "typing_duration_msecs": self._typing_duration(),
            }

        ok, created, message = await self._submit(credential, payload)
        if ok:
            self._record_write(user_id)
        return message

    @filter.llm_tool(name="discourse_read_replies")
    async def discourse_read_replies(
        self,
        event: AstrMessageEvent,
        topic_id: str,
        post_number: int = 1,
        limit: int = 20,
    ) -> str:
        """查看某个楼层下面的楼中楼回复。

        Args:
            topic_id(string): 主题 ID 或主题链接。
            post_number(number): 要看哪一楼的回复，1 表示主楼。
            limit(number): 最多返回几条，默认 20。
        """
        parsed_id = self._parse_topic_id(topic_id)
        if not parsed_id:
            return "无法识别主题 ID，请传入数字 ID 或完整主题链接。"
        try:
            floor = int(post_number or 1)
            limit = max(1, min(50, int(limit or 20)))
        except (TypeError, ValueError):
            return "楼层号和数量必须是数字。"
        try:
            data = await self._read(self.api.topic, parsed_id)
        except DiscourseError as exc:
            return f"读取主题失败：{exc.message}"

        stream = [int(pid) for pid in (data.get("post_stream") or {}).get("stream") or []]
        known = {
            int(item.get("id")): item
            for item in (data.get("post_stream") or {}).get("posts") or []
            if item.get("id")
        }
        wanted = stream[:60]
        missing = [pid for pid in wanted if pid not in known]
        for start in range(0, len(missing), 20):
            chunk = missing[start : start + 20]
            if not chunk:
                continue
            try:
                extra = await self._read(self.api.topic_posts, parsed_id, chunk)
            except DiscourseError:
                continue
            for item in (extra.get("post_stream") or {}).get("posts") or []:
                if item.get("id"):
                    known[int(item["id"])] = item

        replies = [
            post
            for post in known.values()
            if int(post.get("reply_to_post_number") or 0) == floor
            and not self.store.is_blocked(
                post.get("user_id"), post.get("username") or ""
            )
        ]
        replies.sort(key=lambda item: int(item.get("post_number") or 0))
        if not replies:
            return f"主题 {parsed_id} 第 {floor} 楼暂时没有楼中楼回复。"
        lines = [f"主题 {parsed_id} 第 {floor} 楼的回复（{len(replies)} 条）："]
        for post in replies[:limit]:
            text = html_to_text(str(post.get("cooked") or post.get("raw") or "")).strip()
            text = self._truncate(text, 400)
            lines.append(
                f"#{post.get('post_number')} @{post.get('username') or '未知'}：\n{text}"
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # LLM 工具：论坛日记 / 跨会话记忆
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_save_diary")
    async def discourse_save_diary(
        self, event: AstrMessageEvent, diary: str = ""
    ) -> str:
        """把这次逛论坛的见闻写成日记，之后可以在别的会话里回忆。

        Args:
            diary(string): 日记内容，例如今天看了什么、和谁聊了什么、有什么想法。
        """
        text = str(diary or "").strip()
        if len(text) < 10:
            return "日记太短，至少写 10 个字。"
        self.store.add_diary(
            self._truncate(text, 4000), source=self._user_id(event)
        )
        return "日记已保存，之后可以用 discourse_recall_memory 回忆。"

    @filter.llm_tool(name="discourse_recall_memory")
    async def discourse_recall_memory(
        self, event: AstrMessageEvent, limit: int = 5
    ) -> str:
        """回忆最近保存的论坛日记。

        Args:
            limit(number): 回忆最近几条，默认 5。
        """
        try:
            limit = max(1, min(20, int(limit or 5)))
        except (TypeError, ValueError):
            limit = 5
        entries = self.store.diary_entries(limit)
        if not entries:
            return "还没有保存过论坛日记。"
        lines = [f"最近 {len(entries)} 条论坛日记："]
        for item in entries:
            when = datetime.fromtimestamp(int(item.get("ts") or 0)).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines.append(f"- {when}\n{item.get('text')}")
        return self._truncate("\n\n".join(lines), 6000)

    # ------------------------------------------------------------------
    # LLM 工具：图片上传 / 查看
    # ------------------------------------------------------------------

    @filter.llm_tool(name="discourse_upload_image")
    async def discourse_upload_image(
        self, event: AstrMessageEvent, image_source: str = "", description: str = ""
    ) -> str:
        """把本地图片或图片链接上传到论坛图床，返回可直接插入正文的 URL。

        Args:
            image_source(string): 本地文件路径，或 http(s) 图片链接。
            description(string): 可选，图片说明（用于替代文本）。
        """
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        source = str(image_source or "").strip()
        if not source:
            return "请提供图片路径或图片链接。"

        data, filename, mime = await self._load_image(source)
        if isinstance(data, str):
            return data

        adapter = getattr(event, "adapter", None)
        if hasattr(adapter, "upload_image_bytes"):
            full_url = await adapter.upload_image_bytes(filename, data, mime)
            if not full_url:
                return "上传失败：所有图床都没有上传成功，请查看日志。"
        else:
            try:
                result = await self.api.upload(credential, filename, data, mime)
            except DiscourseError as exc:
                return f"上传失败：{exc.message}"
            url = str(result.get("url") or result.get("short_url") or "")
            if not url:
                return "上传成功但论坛没有返回图片地址。"
            full_url = self.api.absolute_url(url)
        alt = str(description or "").strip() or "图片"
        self._record_write(self._user_id(event))
        return f"上传成功，正文里可以这样插入：\n![{alt}]({full_url})\n图片地址：{full_url}"

    async def _load_image(self, source: str):
        """读取图片字节流，返回 (bytes, 文件名, mime) 或 (错误文案, "", "")。"""
        import mimetypes
        from pathlib import Path

        if source.startswith(("http://", "https://")):
            try:
                import aiohttp

                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(source) as resp:
                        if resp.status != 200:
                            return (f"下载图片失败：HTTP {resp.status}", "", "")
                        content_type = (resp.headers.get("content-type") or "").split(";")[0]
                        if not content_type.startswith("image/"):
                            return (f"链接不是图片：{content_type or '未知类型'}", "", "")
                        data = await resp.read()
            except Exception as exc:  # noqa: BLE001
                return (f"下载图片失败：{exc}", "", "")
            name = Path(source.split("?")[0]).name or "image"
            if "." not in name:
                name += mimetypes.guess_extension(content_type) or ".png"
            return (data, name, content_type or "image/png")

        path = Path(source)
        if not path.is_file():
            return (f"找不到文件：{source}", "", "")
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            return (f"不是图片文件：{path.name}", "", "")
        return (data, path.name, mime)

    @filter.llm_tool(name="discourse_view_image")
    async def discourse_view_image(self, event: AstrMessageEvent, image_url: str = ""):
        """下载帖子里的图片并返回给多模态模型查看。

        Args:
            image_url(string): 图片 URL，来自正文里的 ![说明](url)。
        """
        import base64

        url = str(image_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "图片地址必须以 http:// 或 https:// 开头。"
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return f"下载图片失败：HTTP {resp.status}"
                    content_type = (resp.headers.get("content-type") or "").split(";")[0]
                    if not content_type.startswith("image/"):
                        return f"这个链接不是图片：{content_type or '未知类型'}"
                    data = await resp.read()
        except Exception as exc:  # noqa: BLE001
            return f"下载图片失败：{exc}"
        if len(data) > 10 * 1024 * 1024:
            return "图片超过 10MB，无法查看。"

        try:
            from mcp.types import CallToolResult, ImageContent
        except Exception:  # noqa: BLE001
            return f"当前环境不支持直接查看图片，请让用户自己打开：{url}"
        mime = content_type if content_type in (
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        ) else "image/jpeg"
        return CallToolResult(
            content=[
                ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("utf-8"),
                    mimeType=mime,
                )
            ]
        )

    async def _publish_topic(
        self, event: AstrMessageEvent, title: str, content: str, category_id: int
    ) -> str:
        user_id = self._user_id(event)
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        title = title.strip()
        error = self._validate_title(title)
        if error:
            return error
        error = self._validate_content(content)
        if error:
            return error
        if not self._cfg_bool("auto_post_enabled", False):
            return "插件配置关闭了主动发新主题（auto_post_enabled=false），现在只能回帖。"

        allowed = self._categories_allowed()
        target = int(category_id) if category_id else self._default_category()
        if target not in allowed:
            return (
                f"类别 {target} 不在允许发帖的白名单中，"
                f"可用类别：{', '.join(map(str, allowed))}。"
            )
        if not self._rate_ok(user_id):
            return "已达到每小时写操作上限，请稍后再试。"
        if not self._topic_rate_ok(user_id):
            limit = max(1, self._cfg_int("auto_post_daily_limit", 2))
            return f"今天主动发新主题已达上限（{limit} 个），请先回帖。"

        payload = {
            "title": title,
            "raw": content.strip(),
            "category": target,
            "typing_duration_msecs": self._typing_duration(),
        }
        bot_tag = self._cfg_str("bot_tag", "bot").strip()
        if bot_tag:
            payload["tags"] = [bot_tag]

        ok, created, message = await self._submit(credential, payload)
        if ok:
            self._record_write(user_id)
            self._record_topic(user_id)
        return message

    async def _publish_reply(
        self,
        event: AstrMessageEvent,
        topic_id: int,
        content: str,
        post_number: int = 0,
        mentions: str = "",
    ) -> str:
        user_id = self._user_id(event)
        credential = self._credential()
        if not credential:
            return NO_KEY_HINT
        error = self._validate_content(content)
        if error:
            return error
        if not self._rate_ok(user_id):
            return "已达到每小时写操作上限，请稍后再试。"

        if not post_number and not event.get_extra("discourse_auto_engage"):
            try:
                post_number = int(event.get_extra("discourse_post_number") or 0)
            except (TypeError, ValueError):
                post_number = 0
        # 只挡「同一楼层」的重复：同一主题里回复新的楼层是正常行为
        if self.store.replied_within(topic_id, post_number):
            return "刚刚已经回复过这一楼，为避免重复发帖，本次不再重复回复。"

        if event.get_extra("discourse_auto_engage"):
            replied = list(event.get_extra("discourse_auto_replied") or [])
            limit = max(1, self._cfg_int("auto_browse_max_replies", 1))
            if len(replied) >= limit:
                return "本轮自动逛论坛的回帖数量已达上限，不再回复。"

        raw = content.strip()
        prefix = str(event.get_extra("discourse_reply_prefix") or "")
        if prefix and not event.get_extra("discourse_auto_engage"):
            if not raw.lstrip().startswith(prefix):
                raw = f"{prefix}\n\n{raw}"
        # 其它插件刚发来的图片，随这条文字回复一起发出
        adapter = getattr(event, "adapter", None)
        if hasattr(adapter, "take_pending_images"):
            images = adapter.take_pending_images(f"topic:{int(topic_id)}")
            if images:
                raw = f"{raw}\n\n{images}"
        mention_suffix, missing_users = await self._resolve_mentions(mentions, raw)
        raw = dedupe_mentions(raw + mention_suffix)

        payload = {
            "topic_id": int(topic_id),
            "raw": raw,
            "typing_duration_msecs": self._typing_duration(),
        }
        if post_number:
            payload["reply_to_post_number"] = int(post_number)

        ok, created, message = await self._submit(credential, payload, event)
        if ok:
            self._record_write(user_id)
            event.set_extra("discourse_tool_replied", True)
            # 只有明确回复了某一楼才记楼层；自动逛帖（无楼层）不能记成 topic:0，
            # 否则该主题之后所有楼层的通知都会被当成“已回过”而跳过
            if post_number:
                self.store.mark_replied(topic_id, int(post_number))
        if created and event.get_extra("discourse_auto_engage"):
            replied = list(event.get_extra("discourse_auto_replied") or [])
            replied.append(topic_id)
            event.set_extra("discourse_auto_replied", replied)
        if missing_users:
            message += (
                f"\n未找到用户 {'、'.join(missing_users)}，已跳过这些 @。"
                "可先用 discourse_search_user 搜索正确用户名。"
            )
        return message

    async def _submit(
        self, credential: dict, payload: dict, event: AstrMessageEvent | None = None
    ) -> tuple[bool, bool, str]:
        try:
            data = await self.api.create_post(credential, payload)
        except DiscourseError as exc:
            return False, False, f"发布失败：{exc.message}"

        if str(data.get("action", "")) == "enqueued":
            reason = data.get("reason") or "内容触发了站点审核规则"
            return True, False, f"已提交，等待论坛审核（{reason}）。审核通过后才会公开显示。"

        topic_id = data.get("topic_id")
        adapter = getattr(event, "adapter", None)
        if hasattr(adapter, "remember_reply_post"):
            try:
                adapter.remember_reply_post(
                    int(payload.get("topic_id") or 0), int(data.get("id") or 0)
                )
            except (TypeError, ValueError):
                pass
        post_url = self.api.absolute_url(str(data.get("post_url", "")))
        if not post_url and topic_id:
            post_url = self.api.topic_url(int(topic_id))
        username = data.get("username") or credential.get("username", "")
        return True, True, (
            f"发布成功：{post_url}\n"
            f"作者：@{username}  楼层：#{data.get('post_number', '?')}"
        )

    # ------------------------------------------------------------------
    # 定时自动逛论坛
    # ------------------------------------------------------------------

    def _auto_interval(self) -> int:
        return self._cfg_int("auto_browse_interval", 3600, 300)

    def _auto_sessions(self) -> list[str]:
        raw = self.config.get("auto_browse_sessions", [])
        if isinstance(raw, str):
            raw = [raw]
        result: list[str] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                value = str(item).strip()
                if value:
                    result.append(value)
        return result

    async def _auto_browse_loop(self):
        await asyncio.sleep(15)
        while True:
            try:
                await self._auto_browse_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("[Discourse] 自动逛论坛失败：%s", exc)
            await asyncio.sleep(self._auto_interval())

    async def _auto_browse_once(self):
        sessions = self._auto_sessions()
        autoreply = self._cfg_bool("auto_browse_autoreply", False)
        if not sessions and not autoreply:
            return

        try:
            data = await self._read(self.api.latest, page=0)
        except DiscourseError as exc:
            logger.warning("[Discourse] 自动逛论坛读取失败：%s", exc.message)
            return
        topics = (data.get("topic_list") or {}).get("topics") or []
        if not topics:
            return
        try:
            max_id = max(int(topic.get("id")) for topic in topics)
        except (TypeError, ValueError):
            return

        self.store.prune()
        state = self.store.get_state()
        last_seen = int(state.get("last_seen_topic_id") or 0)
        if last_seen <= 0:
            self.store.mark_seen([int(topic.get("id") or 0) for topic in topics])
            self.store.set_state(last_seen_topic_id=max_id, last_run=int(time.time()))
            logger.info("[Discourse] 自动逛论坛首次运行，记录基线主题 %s", max_id)
            return

        new_topics = [
            topic
            for topic in topics
            if int(topic.get("id") or 0) > last_seen
            and not self.store.is_seen(topic.get("id"))
        ]
        new_topics.sort(key=lambda item: int(item.get("id") or 0))
        if not new_topics:
            # 没有新帖就不推送、不唤醒 AI
            self.store.set_state(last_seen_topic_id=max_id, last_run=int(time.time()))
            return
        self.store.mark_seen([topic.get("id") for topic in new_topics])

        limit = self._cfg_int("auto_browse_limit", 8, 1)
        names = await self._category_names()
        reply_count = self._cfg_int("auto_browse_reply_count", 3, 0)
        previews: dict[int, dict] = {}
        if reply_count > 0:
            preview_topics = new_topics[-limit:] if len(new_topics) > limit else new_topics
            for topic in preview_topics:
                topic_id = int(topic.get("id") or 0)
                if topic_id:
                    previews[topic_id] = await self._topic_preview(topic_id, reply_count)
        digest = format_digest(
            new_topics, self.site_url, names, limit, previews=previews
        )

        comment = ""
        if sessions and self._cfg_bool("auto_browse_llm_comment", True):
            comment = await self._generate_comment(digest, sessions[0])
        message = digest + (f"\n\n{comment}" if comment else "")

        for session in sessions:
            await self._send_text(session, message)

        if autoreply:
            await self._auto_engage(new_topics, sessions, limit, previews)

        replied = list(self.store.get_state().get("replied_topics") or [])
        self.store.set_state(
            last_seen_topic_id=max_id,
            last_digest=message[:2000],
            last_run=int(time.time()),
            replied_topics=replied[-200:],
        )
        logger.info(
            "[Discourse] 自动逛论坛完成：新主题 %s 条，推送会话 %s 个",
            len(new_topics),
            len(sessions),
        )

    async def _generate_comment(self, digest: str, session: str) -> str:
        try:
            provider_id = await self.context.get_current_chat_provider_id(session)
        except Exception:  # noqa: BLE001
            provider_id = ""
        if not provider_id:
            return ""
        custom = self._cfg_str("auto_browse_prompt", "").strip()
        system_prompt = (
            "你是 Discourse 论坛的导读助手。根据给出的新主题列表，用中文写一段 50-150 字的简短导读，"
            "指出哪些帖子值得看、为什么。不要复述全部标题，不要编造列表里没有的内容。"
        )
        prompt = (custom + "\n\n" if custom else "") + digest
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            return (response.completion_text or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Discourse] 生成论坛导读失败：%s", exc)
            return ""

    async def _send_text(self, session: str, text: str) -> None:
        if not text:
            return
        for start in range(0, len(text), 1800):
            chunk = text[start : start + 1800]
            try:
                await self.context.send_message(session, MessageChain().message(chunk))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Discourse] 推送论坛动态到 %s 失败：%s", session, exc)
                return

    async def _auto_engage(
        self,
        topics: list[dict],
        sessions: list[str],
        limit: int,
        previews: dict[int, dict] | None = None,
    ) -> None:
        credential = self._credential()
        if not credential:
            logger.info("[Discourse] 已开启自动回帖但尚未配置论坛 API Key，跳过。")
            return
        if not sessions:
            logger.info("[Discourse] 自动回帖需要至少一个推送会话来获取模型，跳过。")
            return
        try:
            provider_id = await self.context.get_current_chat_provider_id(sessions[0])
        except Exception:  # noqa: BLE001
            provider_id = ""
        if not provider_id:
            logger.warning("[Discourse] 自动回帖找不到可用对话模型，跳过。")
            return
        probability = self._cfg_float("auto_browse_autoreply_probability", 0.1)
        if random.random() > probability:
            logger.info(
                "[Discourse] 自动回帖概率未命中（%.0f%%），本轮不参与讨论。",
                probability * 100,
            )
            return

        state = self.store.get_state()
        replied_before = [int(item) for item in state.get("replied_topics") or []]
        usable = [
            topic
            for topic in topics
            if not self.store.has_replied(topic.get("id"))
            and not self._is_stale(topic.get("created_at"))
            and not self.store.is_blocked(None, topic.get("last_poster_username") or "")
        ]
        if not usable:
            logger.info("[Discourse] 没有未回过的新主题，本轮跳过自动回帖。")
            return
        selected = usable[-limit:] if len(usable) > limit else usable
        lines = []
        for topic in selected:
            title = str(topic.get("title") or "")
            topic_id = int(topic.get("id") or 0)
            lines.append(f"- [{topic_id}] {title}（{topic.get('reply_count', 0)} 回复）")
            preview = (previews or {}).get(topic_id) or {}
            if preview.get("first"):
                lines.append(f"    主楼 {preview['first']}")
            for item in preview.get("replies") or []:
                lines.append(f"    {item}")
        max_replies = max(1, self._cfg_int("auto_browse_max_replies", 1))
        like_enabled = self._cfg_bool("auto_browse_like_enabled", True)
        max_likes = max(1, self._cfg_int("auto_browse_max_likes", 2))
        custom_prompt = self._cfg_str("auto_browse_engage_prompt", "").strip()
        if custom_prompt:
            prompt = custom_prompt + "\n\n当前新主题列表：\n" + "\n".join(lines)
        else:
            prompt = (
                "你现在在浏览 Discourse 论坛的新主题：\n"
                + "\n".join(lines)
                + "\n\n"
                "你可以调用 discourse_read_topic 阅读感兴趣的主题，判断是否值得参与讨论。\n"
                "规则：\n"
                f"- 最多回复 {max_replies} 个主题，宁缺毋滥；\n"
                "- 只回复你有实质内容可补充的主题，不要为了回复而回复；\n"
                "- 不要回复你自己已经参与过的主题"
                + (
                    f"（已回复：{', '.join(map(str, replied_before[-20:]))}）"
                    if replied_before
                    else ""
                )
                + "；\n"
                "- 用 discourse_publish_reply 或 discourse_publish_comment 回帖；\n"
                + (
                    "- 读到确实不错的主楼或回复，可以用 discourse_like_post 点赞，"
                    f"但要克制，本轮最多点 {max_likes} 个；不值得就一赞不点；\n"
                    if like_enabled
                    else ""
                )
                + "- 不要创建新主题；\n"
                "- 逛完后调用 discourse_save_diary 写一段简短日记，记录你看到了什么、和谁互动了。\n"
                "如果没有什么值得参与的，就什么都不做。"
            )
        event = self._make_synthetic_event("discourse", prompt)
        event.set_extra("discourse_auto_engage", True)
        try:
            tools = self._discourse_tool_set()
            await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=prompt,
                tools=tools,
                system_prompt=(
                    "你是论坛里的普通用户，正在浏览新帖子。请保持自然、有分寸，"
                    "只在确实有想法时参与讨论。"
                ),
                max_steps=8,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Discourse] 自动参与讨论失败：%s", exc)
            return

        replied_now = list(event.get_extra("discourse_auto_replied") or [])
        liked_now = list(event.get_extra("discourse_auto_liked") or [])
        if replied_now:
            merged = list(
                dict.fromkeys(replied_before + [int(item) for item in replied_now])
            )
            self.store.set_state(replied_topics=merged[-200:])
        if not replied_now and not liked_now:
            return
        summary_lines: list[str] = []
        if replied_now:
            summary_lines.append("自动参与了这些主题：")
            summary_lines.extend(
                f"- [{topic_id}] {topic_url(self.site_url, topic_id)}"
                for topic_id in replied_now
            )
        if liked_now:
            summary_lines.append("点赞了这些楼层：")
            summary_lines.extend(f"- 帖子 #{post_id}" for post_id in liked_now)
        for session in sessions:
            await self._send_text(session, "\n".join(summary_lines))

    def _make_synthetic_event(self, sender_id: str, prompt: str):
        from astrbot.core.platform.astrbot_message import (
            AstrBotMessage,
            MessageMember,
        )
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        message = AstrBotMessage()
        message.self_id = "discourse"
        message.sender = MessageMember(user_id=str(sender_id), nickname="Discourse")
        message.type = MessageType.FRIEND_MESSAGE
        message.session_id = "discourse_auto_browse"
        message.message_id = f"discourse_browse_{uuid.uuid4().hex}"
        message.message = [Plain(text=prompt)]
        message.message_str = prompt
        message.raw_message = {"type": "discourse_auto_browse"}
        message.timestamp = int(time.time())

        event = AstrMessageEvent(
            message_str=prompt,
            message_obj=message,
            platform_meta=PlatformMetadata(
                name="discourse",
                description="Discourse forum",
                id="discourse",
            ),
            session_id=message.session_id,
        )
        event.is_wake = True
        event.is_at_or_wake_command = True
        return event

    def _discourse_tool_set(self):
        allowed = {
            "discourse_browse_topics",
            "discourse_read_topic",
            "discourse_search_forum",
            "discourse_list_categories",
            "discourse_my_account",
            "discourse_publish_reply",
            "discourse_publish_comment",
            "discourse_skip_reply",
            "discourse_like_post",
            "discourse_read_replies",
            "discourse_save_diary",
            "discourse_recall_memory",
        }
        tool_set = self.context.get_llm_tool_manager().get_full_tool_set()
        for tool in list(tool_set.tools):
            if tool.name not in allowed:
                tool_set.remove_tool(tool.name)
        return tool_set
