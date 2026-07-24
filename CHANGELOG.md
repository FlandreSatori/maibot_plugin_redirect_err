# 更新日志

## 1.3.0

- 沉默策略拆成两项配置：``silence_message_seconds``（只挡该触发消息的 reply）与 ``silence_session_seconds``（挡整会话 reply）；默认消息 120s、会话 0s。

## 1.2.0

- 拦截出站后默认阻止 Maisaka 继续 ``reply``：写入上下文提示，并在短时间内从 Planner 工具列表/工具调用中剥离 ``reply``（``stop_further_replies``，默认开启）。宿主无「标记已回复 / 删除历史」插件 API，故采用该组合方案。

## 1.1.0

- 重定向时可附带触发上下文：时间、会话（群/私聊）、发送人、触发消息正文与 ID、命中关键词等（``include_trigger_context``，默认开启）。

## 1.0.0

- 初版：拦截 ``send_service.after_build_message``，当出站文本包含配置关键词（默认 ``error`` / ``not``）时，支持 ``suppress``（不输出）或 ``redirect``（转发到指定群/私聊/stream_id）。
