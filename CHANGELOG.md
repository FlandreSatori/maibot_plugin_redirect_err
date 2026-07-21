# 更新日志

## 1.0.0

- 初版：拦截 ``send_service.after_build_message``，当出站文本包含配置关键词（默认 ``error`` / ``not``）时，支持 ``suppress``（不输出）或 ``redirect``（转发到指定群/私聊/stream_id）。
