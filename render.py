"""把 Discourse API 返回的数据渲染成适合 LLM / 聊天阅读的纯文本。"""

from __future__ import annotations

import html
from datetime import datetime

from .discourse_api import html_to_text


def tag_names(tags) -> list[str]:
    names: list[str] = []
    for tag in tags or []:
        if isinstance(tag, dict):
            name = tag.get("name") or tag.get("slug") or ""
        else:
            name = tag
        if name:
            names.append(str(name))
    return names


def category_label(category_id, category_names: dict[int, str]) -> str:
    try:
        key = int(category_id)
    except (TypeError, ValueError):
        return str(category_id or "未知")
    name = category_names.get(key)
    return f"{name}({key})" if name else str(key)


def time_text(value: str) -> str:
    if not value:
        return ""
    return str(value).replace("T", " ")[:16]


def topic_url(site_url: str, topic_id) -> str:
    try:
        return f"{site_url.rstrip('/')}/t/{int(topic_id)}"
    except (TypeError, ValueError):
        return site_url


def format_topic_list(
    data: dict,
    site_url: str,
    category_names: dict[int, str],
    limit: int = 15,
    include_excerpt: bool = True,
) -> str:
    topics = (data.get("topic_list") or {}).get("topics") or []
    if not topics:
        return "没有找到主题。"
    lines: list[str] = []
    for topic in topics[:limit]:
        topic_id = topic.get("id")
        title = html.unescape(str(topic.get("title") or topic.get("fancy_title") or ""))
        tags = tag_names(topic.get("tags"))
        lines.append(f"[{topic_id}] {title}")
        meta = (
            f"    分类：{category_label(topic.get('category_id'), category_names)}"
            f"  回复：{topic.get('reply_count', 0)}  浏览：{topic.get('views', 0)}"
        )
        poster = topic.get("last_poster_username")
        if poster:
            meta += f"  最后回复：@{poster}"
        last_active = time_text(str(topic.get("last_posted_at") or topic.get("created_at") or ""))
        if last_active:
            meta += f"  最后活动：{last_active}"
        if tags:
            meta += f"  标签：{', '.join(tags)}"
        lines.append(meta)
        if include_excerpt:
            excerpt = html.unescape(str(topic.get("excerpt") or "")).strip()
            if excerpt:
                excerpt = " ".join(excerpt.split())
                if len(excerpt) > 120:
                    excerpt = excerpt[:120] + "…"
                lines.append(f"    摘要：{excerpt}")
        lines.append(f"    链接：{topic_url(site_url, topic_id)}")
    remaining = len(topics) - limit
    if remaining > 0:
        lines.append(f"……还有 {remaining} 个主题未显示，可以用 page 参数翻页。")
    return "\n".join(lines)


def merge_posts(topic_data: dict, extra_posts: list[dict]) -> list[dict]:
    posts = list((topic_data.get("post_stream") or {}).get("posts") or [])
    known = {post.get("id") for post in posts}
    for post in extra_posts:
        if post.get("id") not in known:
            posts.append(post)
            known.add(post.get("id"))
    posts.sort(key=lambda item: int(item.get("post_number") or 0))
    return posts


def format_topic(
    data: dict,
    site_url: str,
    category_names: dict[int, str],
    max_posts: int = 20,
    max_post_chars: int = 2000,
) -> str:
    title = html.unescape(str(data.get("title") or ""))
    topic_id = data.get("id")
    tags = tag_names(data.get("tags"))
    total = int(data.get("posts_count") or 0)
    lines = [
        f"主题：{title}",
        (
            f"主题ID：{topic_id}  分类：{category_label(data.get('category_id'), category_names)}"
            f"  楼层数：{total}  浏览：{data.get('views', 0)}  点赞：{data.get('like_count', 0)}"
        ),
    ]
    if tags:
        lines.append(f"标签：{', '.join(tags)}")
    lines.append(f"链接：{topic_url(site_url, topic_id)}")
    if data.get("created_at"):
        lines.append(f"创建时间：{time_text(str(data.get('created_at')))})")
    lines.append("")

    posts = (data.get("post_stream") or {}).get("posts") or []
    shown = posts[:max_posts] if max_posts > 0 else posts
    if total > len(shown):
        lines.append(f"（本次显示前 {len(shown)} 楼，剩余 {total - len(shown)} 楼未显示）\n")

    for post in shown:
        text = html_to_text(str(post.get("cooked") or ""))
        if max_post_chars > 0 and len(text) > max_post_chars:
            text = text[:max_post_chars] + "……（已截断）"
        author = post.get("username") or "未知"
        display_name = post.get("name")
        author_text = f"@{author}"
        if display_name and display_name != author:
            author_text += f"（{display_name}）"
        floor = post.get("post_number", "?")
        reply_to = post.get("reply_to_post_number")
        reply_text = f" 回复#{reply_to}" if reply_to else ""
        lines.append(
            f"#{floor} {author_text} {time_text(str(post.get('created_at') or ''))}{reply_text}"
        )
        lines.append(text if text else "（空）")
        lines.append("")
    return "\n".join(lines).strip()


def format_categories(data: dict, allowed: list[int]) -> str:
    categories = (data.get("category_list") or {}).get("categories") or []
    if not categories:
        return "论坛没有可用的类别。"
    lines: list[str] = []
    for category in categories:
        category_id = category.get("id")
        description = html.unescape(str(category.get("description_text") or ""))
        description = " ".join(description.split())
        if len(description) > 100:
            description = description[:100] + "…"
        marker = "（可发主题）" if category_id in allowed else ""
        lines.append(
            f"[{category_id}] {category.get('name')}（slug: {category.get('slug')}）{marker}"
        )
        lines.append(
            f"    主题数：{category.get('topic_count', 0)}"
            + (f"  简介：{description}" if description else "")
        )
    return "\n".join(lines)


def format_search(data: dict, query: str, site_url: str, category_names: dict[int, str]) -> str:
    topics = data.get("topics") or []
    posts = data.get("posts") or []
    if not topics and not posts:
        return f"没有找到与「{query}」相关的主题或帖子。"
    sections: list[str] = [f"搜索「{query}」的结果："]
    if topics:
        sections.append("相关主题：\n" + format_topic_list(
            {"topic_list": {"topics": topics}}, site_url, category_names, 10
        ))
    if posts:
        lines: list[str] = []
        for post in posts[:10]:
            topic_id = post.get("topic_id")
            blurb = html_to_text(str(post.get("blurb") or ""))
            blurb = " ".join(blurb.split())
            if len(blurb) > 150:
                blurb = blurb[:150] + "…"
            lines.append(
                f"主题 [{topic_id}] 第 {post.get('post_number')} 楼 "
                f"@{post.get('username', '未知')}：{blurb}\n"
                f"    链接：{topic_url(site_url, topic_id)}"
            )
        sections.append("相关楼层：\n" + "\n".join(lines))
    return "\n\n".join(sections)


def format_digest(
    topics: list[dict],
    site_url: str,
    category_names: dict[int, str],
    limit: int,
    previews: dict[int, dict] | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    selected = topics[-limit:] if len(topics) > limit else topics
    selected = list(reversed(selected))
    lines = [f"Discourse 论坛新动态（{len(selected)} 条，{now}）", ""]
    for topic in selected:
        title = html.unescape(str(topic.get("title") or ""))
        lines.append(f"[{topic.get('id')}] {title}")
        lines.append(
            f"    分类：{category_label(topic.get('category_id'), category_names)}"
            f"  回复：{topic.get('reply_count', 0)}"
            f"  作者：@{topic.get('last_poster_username') or '未知'}"
        )
        excerpt = html.unescape(str(topic.get("excerpt") or "")).strip()
        if excerpt:
            excerpt = " ".join(excerpt.split())
            if len(excerpt) > 100:
                excerpt = excerpt[:100] + "…"
            lines.append(f"    摘要：{excerpt}")
        preview = (previews or {}).get(topic.get("id")) or {}
        if preview.get("first"):
            lines.append(f"    主楼 {preview['first']}")
        for item in preview.get("replies") or []:
            lines.append(f"    {item}")
        lines.append(f"    链接：{topic_url(site_url, topic.get('id'))}")
    return "\n".join(lines)
