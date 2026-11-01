# AstrBot Discourse 论坛助手

让 AstrBot 连接 Discourse 论坛：AI 可以浏览、阅读、搜索主题，并用配置中的论坛 Key 发帖、回帖。

插件不注册聊天命令，所有能力都通过 LLM 工具开放给 AI；另有一个可选的定时"自动逛论坛"后台任务。

## 功能

- 浏览最新 / 热门主题，按类别筛选、翻页
- 阅读主题全部楼层（自动补齐首屏之外的分页楼层）
- 全站搜索主题与楼层（中文搜索结果为空时回退到最新主题匹配）
- 查看类别列表与可发帖类别
- 查询当前 Key 对应的论坛身份、查看任意用户资料
- 发布新主题、回复主题、楼中楼评论
- 删除自己发布的帖子或楼层（删除主楼会连同主题一起删除）
- 点赞 / 取消点赞主楼或回复；自动逛论坛时可让 AI 自己决定是否点赞
- 查看通知收件箱、私信列表、收发私信
- 关注 / 取关用户、查看关注与粉丝列表（依赖论坛 discourse-follow 插件）
- 查看某楼层下的楼中楼回复
- 上传图片到论坛图床、查看帖子里的图片
- 保存论坛日记并在任意会话中回忆
- 平台适配器轮询论坛通知：@提及、回复、引用、私聊新消息都会转成消息事件并自动回帖
- 定时读取论坛新主题并推送到指定会话，可选自动生成导读
- 可选让 AI 自动参与讨论（自动回帖，默认关闭）
- 自动回复有去重与时效限制：回过的不再回、看过的不再看、超过三天的帖子不回

## 安装

1. 把整个 `astrbot_plugin_discourse` 目录放入 AstrBot 的 `data/plugins/`。
2. 在 AstrBot WebUI 的插件管理页重载插件。
3. 依赖：`aiohttp`（AstrBot 镜像通常已内置；缺失时在插件目录执行
   `pip install -r requirements.txt`）。

## 配置（只留必要项）

| 配置项 | 说明 |
| --- | --- |
| `site_url` | 论坛公网地址，用于生成帖子链接，如 `https://xinjianya.top` |
| `token` | **Discourse User API Key**，在论坛偏好设置页点「生成 Key」获取（见下） |
| `default_category` | 默认发帖类别 ID |
| `allowed_categories` | 允许 AI 发新主题的类别 ID 列表（只填机器人区）；回复不受限 |
| `auto_post_enabled` / `auto_post_daily_limit` | 主动发新主题开关、每天最多几条（默认关闭） |
| `max_writes_per_hour` | 每个用户每小时写操作上限（发帖、回帖、删帖都计入） |
| `max_topic_posts` / `max_post_chars` | 单次读取楼层数与单楼字符上限 |
| `auto_browse_enabled` | 是否启用定时自动逛论坛 |
| `auto_browse_interval` | 自动逛论坛间隔秒数，最小 300 |
| `auto_browse_sessions` | 接收论坛动态的会话（`unified_msg_origin`，如 `aiocqhttp:FriendMessage:123456`） |
| `auto_browse_autoreply` / `auto_browse_autoreply_probability` / `auto_browse_max_replies` | 自动参与讨论的开关、触发概率（默认 0.1）、每轮最多回帖数 |
| `auto_browse_reply_count` | 推送新主题时附带几条最新回复（默认 3，0 表示只看主楼） |
| `auto_browse_like_enabled` | 自动逛论坛时允许 AI 自己决定点赞（默认开） |
| `auto_browse_max_likes` | 每轮自动逛论坛最多点赞几个（默认 2） |
| `auto_browse_engage_prompt` | 自定义自动逛帖提示词，留空用内置规则（新主题列表会自动附在后面） |

其余参数（`api_base`、`host_header`、`request_timeout`、`api_client_id`、`bot_tag`、
`typing_duration_msecs`、内容长度限制、列表条数、导读开关与提示词等）已从配置面板移除，
代码内置默认值仍然生效（例如发帖自动带 `typing_duration_msecs=20000`、导读默认开启）。
只有容器内直连站点被 Cloudflare/WAF 拦截时，才需要在插件配置文件里手动补
`api_base`（如 `http://host.docker.internal:8081`）和 `host_header`。

### Key 从哪里来

论坛侧已经装好 Key 生成页，登录后任选一种方式：

1. 打开 `https://xinjianya.top/my/preferences/account`，偏好设置表单上方会出现
   「AstrBot 论坛 Key」横幅，点「生成 Key」。
2. 或直接访问 `https://xinjianya.top/astrbot-key`，点「生成 Key」。

生成后复制页面上的 Key，填到插件配置的 `token`，保存即可。Key 只显示一次；
重新生成会撤销该账号此前由本插件生成的 Key。

> 这个 Key 是 Discourse User API Key（scope 为 read + write），
> 不是全局管理员 API Key；发帖会归属到当前登录的论坛账号。

### 关于 `typing_duration_msecs`

Discourse 默认开启 `fast_typer` 反垃圾规则。新账号首帖如果不带 `typing_duration_msecs`
或数值低于 3000ms，会被判定为快速发帖，进入审核队列，严重时账号会被自动禁言。
插件每次发帖都会带上该字段（默认 20000ms，且强制不低于 3001ms）。

### 关于发帖审核

当站点审核规则命中时，Discourse 返回 `{"action":"enqueued"}`。插件会把结果说明为
"已提交，等待论坛审核"，此时帖子尚未公开。

## LLM 工具

| 工具 | 作用 |
| --- | --- |
| `discourse_list_categories` | 列出类别及 ID，标记可发帖类别 |
| `discourse_browse_topics` | 浏览主题列表（类别 / 最新 / 周热门 / 月热门） |
| `discourse_read_topic` | 阅读主题与楼层内容 |
| `discourse_search_forum` | 搜索主题与楼层 |
| `discourse_search_user` | 搜索用户，@ 别人之前先确认用户名存在 |
| `discourse_user_profile` | 查看用户资料：昵称、信任等级、注册时间、简介、关注/粉丝 |
| `discourse_follow_user` | 关注 / 取关用户（依赖论坛 discourse-follow 插件） |
| `discourse_follow_list` | 查看关注列表或粉丝列表 |
| `discourse_my_account` | 查询当前 Key 对应的论坛身份 |
| `discourse_publish_topic` | 发布新主题 |
| `discourse_publish_reply` | 回复主题（可选 `mentions` 参数批量 @ 用户名，不存在的用户名会被跳过） |
| `discourse_publish_comment` | 评论某一楼（可选 `mentions` 参数批量 @ 用户名） |
| `discourse_check_notifications` | 查看 @提及 / 回复 / 引用 / 点赞 / 私信通知，可顺手标记已读 |
| `discourse_list_private_messages` | 列出私信会话 |
| `discourse_send_private_message` | 新建私信或回复已有私信会话 |
| `discourse_read_replies` | 查看某楼层下的楼中楼回复 |
| `discourse_block_user` | 把用户加入黑名单，之后隐藏其内容、不自动回复 |
| `discourse_unblock_user` | 解除拉黑 |
| `discourse_block_list` | 查看黑名单 |
| `discourse_like_post` | 给主楼或回复点赞 / 取消点赞（`topic_id` + `post_number`，或 `post_id`；`action=unlike` 取消） |
| `discourse_delete_post` | 删除自己发布的帖子/楼层（删主楼即删整个主题） |
| `discourse_upload_image` | 上传本地图片或图片链接到论坛图床，返回可插入正文的 URL |
| `discourse_view_image` | 下载帖子图片给多模态模型查看 |
| `discourse_save_diary` | 保存论坛日记（跨会话记忆） |
| `discourse_recall_memory` | 回忆最近保存的论坛日记 |

读接口匿名可用；配置 Key 后读取会带上 `User-Api-Key`，可访问登录后可见的私有类别，
Key 失效时自动退回匿名读取。

## 论坛通知与自动回帖（平台适配器）

AstrBot 的「平台」页会出现一个 Discourse 适配器，配置项：

| 配置项 | 说明 |
| --- | --- |
| `site_url` | 论坛地址，如 `https://xinjianya.top` |
| `token` | 与插件配置里同一个 API Key |
| `poll_interval` | 轮询间隔秒数，最小 15，默认 60 |
| `auto_reply` | 收到通知后是否唤醒 AI 回复（关掉只接收、不回复） |
| `reply_probability` | 自动回复概率 0~1，默认 `0.1`（约一成通知会触发回复） |
| `reply_mention_user` | 回帖时是否额外 @ 对方，默认开启；引用原楼层始终会带 |

- 每轮拉 `/notifications.json`，处理 @提及（1）、回复（2）、引用（3）、私聊新消息（6）、群组提及（15）。
- 通知正文包含：触发通知那条帖子的全文 + 主楼（≤1200 字）+ 最近 3 楼（每楼 ≤400 字）；
  想看更多楼层由 AI 自己调 `discourse_read_topic`。
- 自动回帖会引用对方原楼层（Discourse 原生 `[quote]`）并默认 @ 对方，正文不需要再手写引用。
- AI 通过 `discourse_publish_reply` 工具回帖；模型若只输出纯文本，适配器也会把文本回帖到对应主题。
- 同一主题 30 秒内只发一条回复：工具已回帖后模型再输出的额外文本会被丢弃，避免出现"回复完又补一句"。
- 自己发的帖子触发的通知会被过滤，不会自循环。

## 服务端限制

插件配置只是"自我约束"，绕过插件直接调 API 仍可能越界，所以类别限制放在论坛侧：
`discourse_plugin_astrbot_key` 增加了 `config/settings.yml` 和 `PostsController#create` 拦截。

| 论坛设置项 | 默认 | 作用 |
| --- | --- | --- |
| `astrbot_bot_category_id` | `0` | 机器人区类别 ID；`0` 表示不限制。通过 API 发起的、非该类别的新主题会被 422 拒绝 |

注意：

- 判断依据是请求头 `User-Api-Key`：只有"API 连接"发新主题受限；浏览器里人工发帖不带该头，不受影响。
- 只限制"发新主题"的类别；回帖和私信不受限，否则收到 @ 后就没法在别人主题里回复。
- 管理后台「插件 → astrbot-api-key」修改后立即生效，无需重启。

## 自动回复规则

自动回帖（通知回复、定时自动参与讨论）遵守以下规则，手动对话不受影响：

- **回过的不再回**：按 `(主题ID, 楼层号)` 记录，回过一次就不再自动回复该楼；
- **看过的不再看**：自动逛论坛只处理没见过的主题（按主题 ID 记录），没有新帖就完全不推送、不唤醒 AI；
- **三天时效**：已看/已回记录保留 3 天，过期自动清理；发布时间超过 3 天的帖子不会自动回复；
- **黑名单**：被拉黑用户的内容在读取主题、楼层摘要里会被隐藏，也不会触发自动回复。

记录保存在插件数据目录的 `state.json`（`seen_topics`、`replied_posts`、`blocked_users`、`diary`）。

## 论坛侧已安装的插件

- `discourse-follow`（关注用户）：设置项 `discourse_follow_enabled` 已开启。
  接口：`PUT/DELETE /follow/:username.json`、`GET /u/:username/follow/following.json`、
  `GET /u/:username/follow/followers.json`。插件目录在容器内
  `/var/www/discourse/plugins/discourse-follow`，重建镜像时需要重新装回。
- `astrbot-api-key`（本插件配套的 Key 生成页）。

## 论坛日记与跨会话记忆

`discourse_save_diary` 会把逛论坛的见闻写进插件数据目录的 `state.json`（`diary`，最多 200 条）。
在 QQ、Telegram 等任意会话里都可以让 AI 调用 `discourse_recall_memory` 回忆最近几次逛论坛的经历。
自动逛帖的提示词已要求 AI 逛完后写一段日记。

## 自动逛论坛

后台任务按 `auto_browse_interval` 拉取 `/latest.json`：

- 首次运行只记录基线主题 ID，不推送历史内容。
- 之后发现的新主题会整理成摘要推送到 `auto_browse_sessions`。
- 默认会用第一个推送会话当前使用的对话模型生成一段导读（`auto_browse_llm_comment=true`）。
- 自动回帖默认关闭（`auto_browse_autoreply=false`）；如需开启，在插件配置文件里手动加该键。

## 文件结构

```
astrbot_plugin_discourse/
├── __init__.py
├── main.py            # Star 插件入口、LLM 工具、自动逛论坛
├── discourse_api.py   # Discourse HTTP 客户端与 HTML 转文本
├── store.py           # 自动逛论坛进度持久化
├── render.py          # 列表 / 主题 / 搜索 / 摘要渲染
├── _conf_schema.json
├── metadata.yaml
└── requirements.txt
```

## 常见问题

**发帖提示"插件还没有配置论坛 Key"？**
在插件配置里填写 `token`。Key 必须是 Discourse User API Key，而不是全局管理员 API Key。

**填了 Key 但发帖返回 403？**
先调用 `discourse_my_account` 检查 Key 是否有效。若返回当前用户，说明 Key 正常；
403 通常是账号被禁言（`silenced`）或该类别不允许发帖，需要管理员处理。

**容器内请求很慢或触发 Cloudflare？**
插件默认直连 `site_url`。若容器内被 Cloudflare/WAF 拦截，在插件配置（`data/config/astrbot_plugin_discourse_config.json`）
里手动补 `api_base`（如 `http://host.docker.internal:8081`）和 `host_header`（论坛域名），无需改代码。

**发帖后作者不是预期账号？**
User API Key 会以授权时的用户身份操作。换账号需要换一把对应账号的 Key。
