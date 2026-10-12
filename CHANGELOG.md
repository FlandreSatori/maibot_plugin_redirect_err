# 更新日志

## 1.3.3

- 修复 `maisaka.planner.after_response` Hook 完全失效：宿主已改用 Context Item 协议（传 `output_items`），旧版读取的 `response` / `tool_calls` 参数不再存在，导致消息级沉默期间剥离 reply 调用从不生效。现按 `FunctionCallItem` + `func_name == "reply"` 过滤 `output_items` 并回填 `modified_kwargs`，`msg_id` 从 `tool_call.args` 读取；全部调用被剥离时注入 `wait` 调用兜底（复用被移除 Item 的 meta 保证 `logical_turn_id` 合法）。
- 修复触发上下文回退查询必然失败：`message.get_recent` 按 SDK 实际签名调用（`chat_id` + `limit`），移除不存在的 `hours` / `filter_mai` / `include_binary_data` 参数及其降级分支。
- `maisaka.planner.before_request` 参数名 `messages` 改为 `items`，`modified_kwargs` 回填 `items` + `item_schema_version`（与宿主整体替换语义对齐；工具移除功能此前已正常）。
- 消息级沉默剥离现依赖 MaiBot 的 Context Item 协议（`maisaka.planner.after_response` 传 `output_items`），需较新版本 MaiBot。

## 1.3.2

- 重定向触发上下文的会话标签改为从出站消息 `message_info` 组装，不再全量拉取聊天流列表；重定向命中后直接转发目标会话，转发失败仅记录日志（移除 `chat.get_all_streams` capability 声明）。
- 重定向目标会话解析结果按配置缓存，避免每次命中重复查询/打开会话；配置更新时失效，解析失败不缓存以便下次重试。
- 出站关键词匹配列表与词边界正则预构建缓存，配置更新时重建，不再每条出站消息重复编译。
