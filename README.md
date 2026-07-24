# 错误输出重定向（maibot_plugin.redirect_err）

![](https://count.getloli.com/@FlandreSatori-redirect-err?name=FlandreSatori-redirect-err&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

当 Bot **出站**文本包含配置关键词（默认 `error`、`not`）时：

- **suppress**：不发送到原会话  
- **redirect**：不发送到原会话，改为发到指定群 / 私聊 / `stream_id`

## 安装

将本目录放到 MaiBot 的 `plugins/` 下（或你的插件目录），重启 MaiBot，在 WebUI 启用插件并填写配置。

插件 ID：`maibot_plugin.redirect_err`

## 配置

 `config.toml`：

| 段 | 说明 |
|---|---|
| `[match].keywords` | 命中子串列表，默认 `error` / `not` |
| `[match].word_boundary` | `true` 时按单词匹配，避免 `not` 误伤 `note``notice`等 |
| `[action].mode` | `suppress` 或 `redirect` |
| `[action].silence_message_seconds` | 对该触发消息沉默秒数（默认 120） |
| `[action].silence_session_seconds` | 对该会话沉默秒数（默认 0） |
| `[redirect].target_type` | `group` / `private` / `stream_id` |
| `[redirect].group_id` / `user_id` / `stream_id` | 对应目标 |
| `[redirect].prefix` | 转发时前缀，默认 `[redirect_err] ` |
| `[redirect].include_trigger_context` | 转发时附带触发上下文（默认 `true`） |
| `[redirect].trigger_content_max_chars` | 触发消息正文截断长度 |

重定向目标会话需可达（机器人已进群 / 可私聊）。

开启 `include_trigger_context` 后，转发末尾会附加例如：

```text
—— 触发上下文 ——
时间: 2026-07-24 08:49:00
会话: 群聊 测试群(123456)
发送人: 昵称 (789)
ID: ...
触发消息: 用户原话...
命中: error
```

上下文优先取出站消息的引用（`reply_to` / reply 段），否则回退查询该会话近期非 Bot 消息。
