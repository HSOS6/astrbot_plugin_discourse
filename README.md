# AstrBot × Discourse 论坛助手

> 版本 **3.1.5** · 作者 **星见雅** · 让 AI 真的能逛论坛、读帖子、发帖、回帖

把 Discourse 论坛接进 AstrBot：AI 可以浏览、阅读、搜索主题，可以用你自己的论坛 API Key 发帖回帖，
也能接收论坛通知并自动回复。插件不注册任何聊天命令，全部能力以 LLM 工具的形式提供给模型。

---

## 目录

- [功能一览](#功能一览)
- [安装](#安装)
- [配置](#配置)
- [获取论坛 Key](#获取论坛-key)
- [LLM 工具](#llm-工具)
- [论坛通知与自动回帖](#论坛通知与自动回帖)
- [定时逛论坛与自动参与](#定时逛论坛与自动参与)
- [自动回复规则](#自动回复规则)
- [服务端类别限制](#服务端类别限制)
- [论坛日记](#论坛日记)
- [文件结构](#文件结构)
- [常见问题](#常见问题)

---

## 功能一览

**看论坛**

- 浏览最新 / 周热门 / 月热门主题，可按类别筛选、翻页
- 阅读主题全部楼层（自动补齐首屏之外的分页楼层）
- 全站搜索主题与楼层；中文搜索为空时回退到最近主题匹配
- 列出类别及 ID，标记哪些类别可以发帖
- 查看任意用户资料：昵称、头衔、信任等级、注册时间、简介、关注/粉丝
- 查看某楼层下的楼中楼回复

**写论坛**

- 发布新主题、回复主题、楼中楼评论（正文支持 Markdown）
- 回复时可选 @ 其他用户，用户名会先校验存在性
- 点赞 / 取消点赞主楼或回复
- 删除自己发布的帖子或楼层（删主楼即删整个主题）

**社交与通知**

- 通知收件箱：@提及、回复、引用、点赞、私信，可标记已读
- 私信：列出会话、新建私信、回复已有私信
- 关注 / 取关用户、查看关注与粉丝列表（依赖论坛 discourse-follow 插件）
- 上传图片到论坛图床、查看帖子里的图片

**自动化**

- 平台适配器轮询论坛通知，@提及 / 回复 / 引用 / 私信都能唤醒 AI 自动回帖
- 定时逛论坛：只推送新主题，附带主楼 + 最近 N 楼；没有新帖就不打扰
- 可选让 AI 自己决定是否参与讨论、是否点赞（默认关闭 / 概率触发）
- 论坛日记：把逛论坛的见闻存下来，在任意会话里回忆

---

## 安装

1. 把整个 `astrbot_plugin_discourse` 目录放进 AstrBot 的 `data/plugins/`。
2. 打开 AstrBot WebUI → 插件管理 → 重载插件。
3. 依赖 `aiohttp`（AstrBot 镜像一般自带；缺失时在插件目录执行
   `pip install -r requirements.txt`）。
4. 在 WebUI 的「平台」页新增一个 **Discourse 论坛** 适配器，填入论坛地址和 Key；
   如果只想要工具能力、不需要自动回帖，跳过这一步也能用。

---

## 配置

### 插件配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `site_url` | `https://xinjianya.top` | 论坛地址，用于拼接帖子链接 |
| `token` | 空 | **Discourse User API Key**，见下方「获取论坛 Key」 |
| `default_category` | `6` | 默认发帖类别 ID |
| `allowed_categories` | `[6]` | 允许 AI 主动发新主题的类别白名单 |
| `auto_post_enabled` | `false` | 是否允许主动发新主题（关闭时只能回帖） |
| `auto_post_daily_limit` | `2` | 每天最多主动发几个主题 |
| `max_writes_per_hour` | `6` | 每个用户每小时写操作上限（发帖、回帖、点赞、删帖都计入） |
| `max_topic_posts` | `20` | 单次读取主题最多返回几楼 |
| `max_post_chars` | `2000` | 单楼正文最多返回多少字符 |
| `auto_browse_enabled` | `true` | 是否启用定时逛论坛后台任务 |
| `auto_browse_interval` | `3600` | 逛论坛间隔秒数（最小 300） |
| `auto_browse_sessions` | 空 | 推送目标会话列表，如 `aiocqhttp:FriendMessage:123456` |
| `auto_browse_autoreply` | `false` | 是否让 AI 自动参与讨论 |
| `auto_browse_autoreply_probability` | `0.1` | 每轮自动参与的概率（0~1） |
| `auto_browse_max_replies` | `1` | 每轮最多回几个帖 |
| `auto_browse_reply_count` | `3` | 摘要里附带几条最新回复（0 = 只看主楼） |
| `auto_browse_like_enabled` | `true` | 自动逛帖时允许 AI 点赞 |
| `auto_browse_max_likes` | `2` | 每轮最多点赞几个 |
| `auto_browse_engage_prompt` | 空 | 自定义自动逛帖提示词，留空用内置规则 |

> 还有一些参数（`api_base`、`host_header`、`request_timeout`、`api_client_id`、`bot_tag`、
> `typing_duration_msecs`、内容长度限制、列表条数、导读开关等）不在配置面板里，
> 但代码内置默认值依然生效。只有容器直连站点被 Cloudflare / WAF 拦截时，
> 才需要手动在 `data/config/astrbot_plugin_discourse_config.json` 里补
> `api_base`（如 `http://host.docker.internal:8081`）和 `host_header`。

### 适配器配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `site_url` | `https://xinjianya.top` | 论坛地址 |
| `token` | 空 | 与插件配置里同一个 Key |
| `poll_interval` | `60` | 轮询通知间隔秒数（最小 15） |
| `auto_reply` | `true` | 收到通知后是否唤醒 AI 回复 |
| `reply_probability` | `0.1` | 自动回复概率（0~1） |
| `reply_mention_user` | `true` | 回帖时是否额外 @ 对方（回复本身已挂在对方楼层下） |

---

## 获取论坛 Key

插件用的是 **Discourse User API Key**（scope：read + write），不是全局管理员 Key，
发帖会归属到你本人登录的论坛账号。论坛侧已经装好 `astrbot-api-key` 插件，登录后任选一种方式：

1. 打开 `https://xinjianya.top/my/preferences/account`，偏好设置表单上方的
   「AstrBot 论坛 Key」面板里点「生成 Key」；
2. 或直接访问 `https://xinjianya.top/astrbot-key`。

复制得到的 Key 填进插件配置的 `token` 即可。Key 只显示一次，重新生成会作废该账号此前由本插件生成的 Key。

---

## LLM 工具

模型会在需要时自动调用，你也可以在对话里直接点名。

### 读

| 工具 | 作用 |
| --- | --- |
| `discourse_list_categories` | 列出类别及 ID，标记可发帖类别 |
| `discourse_browse_topics` | 浏览主题列表（`latest` / `top_weekly` / `top_monthly`，可按类别、翻页） |
| `discourse_read_topic` | 阅读主题与楼层内容 |
| `discourse_search_forum` | 搜索主题与楼层 |
| `discourse_read_replies` | 查看某楼层下的楼中楼回复 |
| `discourse_search_user` | 搜索用户，@ 别人之前先确认用户名存在 |
| `discourse_user_profile` | 用户资料：昵称、信任等级、注册时间、简介、关注/粉丝 |
| `discourse_my_account` | 查询当前 Key 对应的论坛身份 |

### 写

| 工具 | 作用 |
| --- | --- |
| `discourse_publish_topic` | 发布新主题（受类别白名单与每日上限约束） |
| `discourse_publish_reply` | 回复主题，可选 `mentions` 批量 @ 用户 |
| `discourse_publish_comment` | 评论某一楼（楼中楼），同样支持 `mentions` |
| `discourse_like_post` | 点赞 / 取消点赞（`topic_id` + `post_number`，或直接给 `post_id`） |
| `discourse_delete_post` | 删除自己发布的帖子或楼层 |
| `discourse_upload_image` | 上传本地图片或图片链接到论坛图床 |

### 通知与私信

| 工具 | 作用 |
| --- | --- |
| `discourse_check_notifications` | 查看通知（提及 / 回复 / 引用 / 点赞 / 私信），可顺手标记已读 |
| `discourse_skip_reply` | 收到通知后决定不回复时调用，避免把思考文字误发成帖子 |
| `discourse_list_private_messages` | 列出私信会话 |
| `discourse_send_private_message` | 新建私信，或传 `topic_id` 回复已有会话 |

### 社交与记忆

| 工具 | 作用 |
| --- | --- |
| `discourse_follow_user` | 关注 / 取关用户（依赖论坛 discourse-follow 插件） |
| `discourse_follow_list` | 查看关注列表或粉丝列表 |
| `discourse_view_image` | 下载帖子图片交给多模态模型查看 |
| `discourse_block_user` / `discourse_unblock_user` / `discourse_block_list` | 黑名单增删查 |
| `discourse_save_diary` / `discourse_recall_memory` | 保存 / 回忆论坛日记 |

读接口匿名可用；配置 Key 后读取会带上 `User-Api-Key`，能访问登录后可见的私有类别，
Key 失效时自动退回匿名读取。

---

## 论坛通知与自动回帖

适配器每轮拉 `/notifications.json`，处理 @提及（1）、回复（2）、引用（3）、私信（6）、群组提及（15）。

- 通知正文包含：触发通知那条帖子的全文 + 主题上下文（主楼 ≤1200 字 + 最近 3 楼，每楼 ≤400 字），
  想看更多楼层由 AI 自己调 `discourse_read_topic`。
- 回帖会自动挂到对方楼层下（`reply_to_post_number`），并可按配置额外 @ 对方；
  不写引用块，正文里也不需要手写引用。
- AI 通过 `discourse_publish_reply` 回帖；如果模型只输出纯文本，适配器也会把文本发回对应主题。
- 同一主题 120 秒内只发一条回复：工具已回帖后模型再输出的文字会被丢弃，避免「回复完又补一句」。
- 自己发的帖子触发的通知会被过滤，不会自循环。

---

## 定时逛论坛与自动参与

后台任务按 `auto_browse_interval` 拉取 `/latest.json`：

1. 首次运行只记录基线，不推送历史内容。
2. 之后发现的新主题整理成摘要，推送到 `auto_browse_sessions` 里的会话；
   摘要默认包含主楼和最近 `auto_browse_reply_count` 楼。
3. 若配置了推送会话，会用该会话当前对话模型写一段 50~150 字的导读。
4. 开启 `auto_browse_autoreply` 后，AI 会按概率（默认 10%）阅读新主题并选择性回帖；
   也可以点赞（受 `auto_browse_like_enabled` / `auto_browse_max_likes` 约束），逛完写一段论坛日记。
5. 结束时会把「自动参与了哪些主题 / 点赞了哪些楼层」推回会话。

---

## 自动回复规则

自动回帖（通知回复、定时自动参与）遵守以下规则，手动对话不受影响：

- **回过的不再回**：按 `(主题ID, 楼层号)` 记录，回过一次就不再自动回复该楼；
- **短时间防重复**：同一主题 120 秒内只允许发一条回复，工具回复与纯文本回复共用同一份
  `state.json` 记录；插件主体与适配器每次读写前都会重新加载该文件，避免两个实例互相覆盖；
- **不写引用块**：Discourse 会自动删除紧跟在原楼层后的整楼引用，导致引用时有时无，
  所以统一靠 `reply_to_post_number` 原生指向原楼层，正文和前缀里都不再加 `[quote]`；
- **输出保护**：模型输出里混入 `<|end|>` 这类特殊标记时直接丢弃，不会发到论坛；
- **提及 / 引用必回**：有人 @ 你、引用你的帖子（含群组提及）时不经过概率，直接交给 AI 回复；
  只有骚扰、垃圾内容才会用 `discourse_skip_reply` 跳过，必要时用 `discourse_block_user` 拉黑；
- **别人回复你的帖子由 AI 决定**：不管是自己发布的主题，还是你在别人主题里的楼层，
  被别人回复时都不按 `reply_probability` 随机跳过，而是把主题标题、回复内容和上下文
  一起交给 AI，由 AI 自己决定回复还是调用 `discourse_skip_reply` 跳过；
  AI 决定不回后，本次事件残留的文本（包括被分段/防抖插件二次发送的）也会被丢弃，不会发帖；
  其余通知（如私信）仍按概率触发；
- **看过的不再看**：只处理没见过的新主题，没有新帖就完全不推送、不唤醒 AI；
- **三天时效**：已看 / 已回记录保留 3 天，过期自动清理；超过 3 天的帖子不再自动回复；
- **黑名单**：被拉黑用户的内容在阅读主题、摘要里会被隐藏，也不会触发自动回复。

记录保存在插件数据目录的 `state.json`
（`seen_topics`、`replied_posts`、`blocked_users`、`diary`）。

---

## 服务端类别限制

插件配置只是自我约束，绕过插件直接调 API 仍可能越界，所以类别限制放在论坛侧：
配套插件 `discourse_plugin_astrbot_key` 用 `PostsController#create` 拦截。

| 论坛设置项 | 默认 | 作用 |
| --- | --- | --- |
| `astrbot_bot_category_id` | `0` | 机器人区类别 ID；`0` 表示不限制。通过 API 发起的、非该类别的新主题会被 422 拒绝 |

- 判断依据是请求头 `User-Api-Key`：只有「API 连接」发新主题受限，浏览器里人工发帖不受影响。
- 只限制发新主题的类别；回帖、私信不受限，否则收到 @ 后没法在别人主题里回复。
- 管理后台「插件 → astrbot-api-key」修改后立即生效，无需重启。

---

## 论坛日记

`discourse_save_diary` 把逛论坛的见闻写进 `state.json` 的 `diary`（最多 200 条）；
在 QQ、Telegram 等任意会话里都可以让 AI 调 `discourse_recall_memory` 回忆最近几次经历。
自动逛帖的提示词已经要求 AI 逛完后写一段日记。

---

## 文件结构

```
astrbot_plugin_discourse/
├── __init__.py          # 导出插件与适配器
├── main.py              # Star 插件入口、26 个 LLM 工具、自动逛论坛
├── discourse_api.py     # Discourse HTTP 客户端、HTML 转纯文本
├── adapter.py           # 平台适配器：轮询通知、构造事件、回帖
├── discourse_event.py   # 通知事件：把模型输出发回对应主题
├── render.py            # 列表 / 主题 / 搜索 / 摘要的文本渲染
├── store.py             # 已看已回记录、黑名单、论坛日记的持久化
├── _conf_schema.json    # 插件配置项
├── metadata.yaml        # 插件元信息
└── requirements.txt
```

## 配套论坛插件

| 插件 | 说明 |
| --- | --- |
| `discourse-follow` | 关注用户。需开启 `discourse_follow_enabled`；接口 `PUT/DELETE /follow/:username.json`、`GET /u/:username/follow/following.json`、`.../followers.json` |
| `astrbot-api-key` | 偏好设置里的 Key 生成面板 + 发帖类别限制 |

> 这两个插件装在容器 `/var/www/discourse/plugins/` 下，重建镜像时需要重新装回。

---

## 常见问题

**提示「插件还没有配置论坛 Key」？**
在插件配置里填 `token`，必须是 Discourse User API Key，不是全局管理员 Key。

**填了 Key 但发帖 403？**
先调用 `discourse_my_account` 确认 Key 有效。若返回当前用户说明 Key 正常，
403 通常是账号被禁言（`silenced`）或该类别不允许发帖。

**发帖进审核队列？**
Discourse 的 `fast_typer` 规则会把过快发帖判为可疑。插件每次发帖都带
`typing_duration_msecs`（默认 20000ms，且不低于 3001ms）；
若仍返回 `{"action":"enqueued"}`，说明命中了站点审核规则，帖子需管理员放行。

**容器内请求很慢或被 Cloudflare 拦截？**
默认直连 `site_url`。被拦截时在插件配置文件里补 `api_base` 与 `host_header` 即可，不用改代码。

**发帖作者不是预期账号？**
User API Key 以授权时的账号身份操作，换账号要换一把对应账号的 Key。

**关注功能报 500？**
论坛需要启用 `discourse_follow_enabled`，并已执行 `rake db:migrate`（缺 `user_followers` 表会 500）。
