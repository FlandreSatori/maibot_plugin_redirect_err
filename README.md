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
| `[redirect].target_type` | `group` / `private` / `stream_id` |
| `[redirect].group_id` / `user_id` / `stream_id` | 对应目标 |
| `[redirect].prefix` | 转发时前缀，默认 `[redirect_err] ` |

重定向目标会话需可达（机器人已进群 / 可私聊）。

## 行为说明

- 挂载 Hook：`send_service.after_build_message`（消息构建完成后、真正发出前）
- 命中关键词后对**原目标**返回 `abort`，避免原群看到错误文案
- 转发使用 `ctx.send.text`，并用 ContextVar 防止递归再次拦截
- 若当前已在目标会话：默认 `when_already_at_target = continue`（错误频道照常显示）
