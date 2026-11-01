"""保存自动逛论坛进度、已看/已回帖子以及拉黑名单。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 已看/已回记录保留时长（秒），过期后自动清理
RETENTION_SECONDS = 3 * 86400


def _as_ts(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class StateStore:
    """把插件运行状态持久化到插件数据目录。

    数据结构：
    {
      "last_seen_topic_id": 0,
      "last_digest": "",
      "last_run": 0,
      "replied_topics": [],
      "seen_topics": {"12": 1730000000},
      "replied_posts": {"19:5": 1730000000},
      "blocked_users": {"13": {"name": "someone", "ts": 1730000000}}
    }
    """

    def __init__(self, data_dir: Path):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "state.json"
        self._data = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return self._defaults()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._defaults()
        if not isinstance(raw, dict):
            return self._defaults()
        data = self._defaults()
        data.update(raw)
        return data

    @staticmethod
    def _defaults() -> dict:
        return {
            "last_seen_topic_id": 0,
            "last_digest": "",
            "last_run": 0,
            "replied_topics": [],
            "seen_topics": {},
            "replied_posts": {},
            "blocked_users": {},
        }

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    def get_state(self) -> dict:
        return dict(self._data)

    def set_state(self, **kwargs) -> None:
        self._data.update(kwargs)
        self._save()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _bucket(self, key: str) -> dict:
        value = self._data.get(key)
        if not isinstance(value, dict):
            value = {}
            self._data[key] = value
        return value

    # ------------------------------------------------------------------
    # 三天过期清理
    # ------------------------------------------------------------------

    def prune(self, retention: int = RETENTION_SECONDS) -> None:
        now = time.time()
        changed = False
        for key in ("seen_topics", "replied_posts"):
            bucket = self._bucket(key)
            stale = [
                item for item, ts in bucket.items() if now - _as_ts(ts) > retention
            ]
            for item in stale:
                bucket.pop(item, None)
            changed = changed or bool(stale)
        if changed:
            self._save()

    # ------------------------------------------------------------------
    # 已看主题 / 已回帖子
    # ------------------------------------------------------------------

    def mark_seen(self, topic_ids) -> None:
        bucket = self._bucket("seen_topics")
        now = int(time.time())
        for topic_id in topic_ids:
            try:
                bucket[str(int(topic_id))] = now
            except (TypeError, ValueError):
                continue
        self._save()

    def is_seen(self, topic_id) -> bool:
        try:
            return str(int(topic_id)) in self._bucket("seen_topics")
        except (TypeError, ValueError):
            return False

    def mark_replied(self, topic_id, post_number: int = 0) -> None:
        try:
            key = f"{int(topic_id)}:{int(post_number or 0)}"
        except (TypeError, ValueError):
            return
        self._bucket("replied_posts")[key] = int(time.time())
        self._save()

    def has_replied(self, topic_id, post_number: int = 0) -> bool:
        bucket = self._bucket("replied_posts")
        try:
            exact = f"{int(topic_id)}:{int(post_number or 0)}"
            whole = f"{int(topic_id)}:0"
        except (TypeError, ValueError):
            return False
        return exact in bucket or whole in bucket

    def replied_within(self, topic_id, seconds: int = 30) -> bool:
        """主题在最近 N 秒内是否刚被回复过（用于阻断一次请求发两条回复）。"""
        try:
            prefix = f"{int(topic_id)}:"
            now = time.time()
        except (TypeError, ValueError):
            return False
        for key, ts in self._bucket("replied_posts").items():
            if str(key).startswith(prefix) and now - _as_ts(ts) < seconds:
                return True
        return False

    # ------------------------------------------------------------------
    # 拉黑名单（不参与三天清理）
    # ------------------------------------------------------------------

    def block(self, key: str, username: str = "") -> None:
        key = str(key or "").strip().lower()
        if not key:
            return
        self._bucket("blocked_users")[key] = {
            "name": str(username or "").strip(),
            "ts": int(time.time()),
        }
        self._save()

    def unblock(self, key: str) -> bool:
        bucket = self._bucket("blocked_users")
        key = str(key or "").strip().lower()
        removed = False
        for item in [key, key.lstrip("@")]:
            if item in bucket:
                bucket.pop(item)
                removed = True
        if removed:
            self._save()
        return removed

    def blocked_keys(self) -> set[str]:
        return set(self._bucket("blocked_users").keys())

    def blocked_names(self) -> set[str]:
        names: set[str] = set()
        for key, item in self._bucket("blocked_users").items():
            if not str(key).isdigit():
                names.add(str(key).lower())
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]).lower())
        return names

    def is_blocked(self, user_id=None, username: str = "") -> bool:
        if user_id is not None and str(user_id).strip().lower() in self.blocked_keys():
            return True
        text = str(username or "").strip().lstrip("@").lower()
        return bool(text) and text in self.blocked_names()

    def blocked_entries(self) -> list[dict]:
        result: list[dict] = []
        for key, item in self._bucket("blocked_users").items():
            name = item.get("name", "") if isinstance(item, dict) else ""
            result.append({"key": key, "username": name})
        return result

    # ------------------------------------------------------------------
    # 论坛日记（跨会话回忆）
    # ------------------------------------------------------------------

    def add_diary(self, content: str, source: str = "") -> None:
        text = str(content or "").strip()
        if not text:
            return
        entries = self._bucket("diary")
        stamp = time.time()
        entries[f"{int(stamp * 1000)}"] = {
            "ts": int(stamp),
            "text": text[:4000],
            "source": str(source or "")[:200],
        }
        if len(entries) > 200:
            for key in sorted(entries.keys())[: len(entries) - 200]:
                entries.pop(key, None)
        self._save()

    def diary_entries(self, limit: int = 5) -> list[dict]:
        entries = self._bucket("diary")
        result = [
            item
            for item in entries.values()
            if isinstance(item, dict) and item.get("text")
        ]
        result.sort(key=lambda item: item.get("ts", 0), reverse=True)
        return result[: max(1, int(limit))]
