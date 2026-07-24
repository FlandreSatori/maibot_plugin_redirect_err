"""错误输出重定向插件（MaiBot SDK 2.x）。

监听出站 Hook ``send_service.after_build_message``：当 Bot 待发送纯文本
包含配置关键词（默认 ``error`` / ``not``）时：

- ``suppress``：中止原发送（不输出）；
- ``redirect``：中止原发送，并将文本转发到配置的群聊 / 私聊 / stream_id；
  转发时可附带触发该条回复的上下文（时间、会话、发送人、原文等）。
"""

from __future__ import annotations

import contextvars
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

_IN_REDIRECT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "maibot_plugin_redirect_err_in_redirect",
    default=False,
)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class MatchConfig(PluginConfigBase):
    """匹配规则。"""

    __ui_label__ = "匹配"
    __ui_icon__ = "search"
    __ui_order__ = 1

    keywords: list[str] = Field(
        default_factory=lambda: ["error", "not"],
        description="出站文本包含任一关键词即触发（子串匹配）",
    )
    case_sensitive: bool = Field(default=False, description="是否区分大小写")
    word_boundary: bool = Field(
        default=False,
        description="是否按单词边界匹配（开启后「not」不会命中 note/nothing）",
    )


class ActionConfig(PluginConfigBase):
    """命中后的动作。"""

    __ui_label__ = "动作"
    __ui_icon__ = "zap"
    __ui_order__ = 2

    mode: str = Field(
        default="suppress",
        description="命中动作：suppress（不输出）或 redirect（重定向）",
    )


class RedirectConfig(PluginConfigBase):
    """重定向目标。"""

    __ui_label__ = "重定向"
    __ui_icon__ = "corner-up-right"
    __ui_order__ = 3

    target_type: str = Field(
        default="group",
        description="目标类型：group / private / stream_id",
    )
    group_id: str = Field(default="", description="目标群号（target_type=group）")
    user_id: str = Field(default="", description="目标私聊用户 ID（target_type=private）")
    stream_id: str = Field(default="", description="目标 stream_id（target_type=stream_id）")
    platform: str = Field(default="qq", description="解析群/私聊时的平台")
    prefix: str = Field(default="[redirect_err] ", description="重定向发送时附加的前缀")
    when_already_at_target: str = Field(
        default="continue",
        description="当前已在目标会话时：continue=照常发出，suppress=拦截",
    )
    include_trigger_context: bool = Field(
        default=True,
        description="重定向时是否附带触发上下文（时间、群聊/私聊、发送人、原文等）",
    )
    trigger_content_max_chars: int = Field(
        default=500,
        description="触发消息正文截断长度",
    )


class RedirectErrConfig(PluginConfigBase):
    """错误输出重定向插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)
    action: ActionConfig = Field(default_factory=ActionConfig)
    redirect: RedirectConfig = Field(default_factory=RedirectConfig)


class RedirectErrPlugin(MaiBotPlugin):
    """错误输出重定向插件。"""

    config_model = RedirectErrConfig

    async def on_load(self) -> None:
        mode = str(self.config.action.mode or "suppress").strip().lower()
        keywords = [str(k).strip() for k in (self.config.match.keywords or []) if str(k).strip()]
        self.ctx.logger.info(
            "redirect_err 已加载：enabled=%s mode=%s keywords=%s include_context=%s",
            bool(self.config.plugin.enabled),
            mode,
            keywords,
            bool(self.config.redirect.include_trigger_context),
        )

    async def on_unload(self) -> None:
        return

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        self.ctx.logger.info("redirect_err 配置已更新")

    def _extract_outbound_text(self, message: Any, processed_plain_text: str) -> str:
        text = str(processed_plain_text or "").strip()
        if text:
            return text
        if isinstance(message, dict):
            return str(message.get("processed_plain_text") or "").strip()
        return ""

    def _text_matches(self, text: str) -> Optional[str]:
        """若命中则返回命中的关键词，否则 None。"""

        keywords = [str(k).strip() for k in (self.config.match.keywords or []) if str(k).strip()]
        if not text or not keywords:
            return None

        case_sensitive = bool(self.config.match.case_sensitive)
        word_boundary = bool(self.config.match.word_boundary)
        haystack = text if case_sensitive else text.lower()

        for raw in keywords:
            needle = raw if case_sensitive else raw.lower()
            if not needle:
                continue
            if word_boundary:
                flags = 0 if case_sensitive else re.IGNORECASE
                pattern = rf"(?<!\w){re.escape(raw)}(?!\w)"
                if re.search(pattern, text, flags=flags):
                    return raw
            elif needle in haystack:
                return raw
        return None

    @staticmethod
    def _pick_stream_id(payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, dict):
            return ""
        for key in ("stream_id", "session_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        nested = payload.get("stream")
        if isinstance(nested, dict):
            for key in ("stream_id", "session_id"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _unwrap_message(payload: Any) -> Optional[Dict[str, Any]]:
        if isinstance(payload, dict):
            nested = payload.get("message")
            if isinstance(nested, dict):
                return nested
            if payload.get("message_id") or payload.get("processed_plain_text") or payload.get("message_info"):
                return payload
        return None

    @staticmethod
    def _unwrap_messages(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            nested = payload.get("messages")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return []

    @staticmethod
    def _format_time(value: Any) -> str:
        if value is None:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return str(value)
        text = str(value).strip()
        if not text:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ISO / "2026-07-24T08:49:00" → 更易读
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return text[:19].replace("T", " ")

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        normalized = str(text or "").strip()
        limit = max(0, int(max_chars or 0))
        if limit <= 0 or len(normalized) <= limit:
            return normalized
        return normalized[:limit] + "…"

    def _extract_reply_hint(self, message: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """从出站消息的 reply 段 / reply_to 提取触发线索。"""

        hint: Dict[str, str] = {
            "message_id": "",
            "content": "",
            "user_id": "",
            "nickname": "",
            "cardname": "",
        }
        if not isinstance(message, dict):
            return hint

        reply_to = str(message.get("reply_to") or "").strip()
        if reply_to:
            hint["message_id"] = reply_to

        raw_message = message.get("raw_message")
        segments: List[Any] = []
        if isinstance(raw_message, list):
            segments = raw_message
        elif isinstance(raw_message, dict):
            nested = raw_message.get("components") or raw_message.get("segments") or []
            if isinstance(nested, list):
                segments = nested

        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "").strip().lower()
            if seg_type != "reply":
                continue
            data = seg.get("data") if isinstance(seg.get("data"), dict) else seg
            if not isinstance(data, dict):
                continue
            hint["message_id"] = hint["message_id"] or str(data.get("target_message_id") or "").strip()
            hint["content"] = str(data.get("target_message_content") or "").strip()
            hint["user_id"] = str(data.get("target_message_sender_id") or "").strip()
            hint["nickname"] = str(data.get("target_message_sender_nickname") or "").strip()
            hint["cardname"] = str(data.get("target_message_sender_cardname") or "").strip()
            break
        return hint

    def _message_user_fields(self, message: Optional[Dict[str, Any]]) -> Dict[str, str]:
        info = self._as_dict((message or {}).get("message_info"))
        user = self._as_dict(info.get("user_info"))
        return {
            "user_id": str(user.get("user_id") or "").strip(),
            "nickname": str(user.get("user_nickname") or "").strip(),
            "cardname": str(user.get("user_cardname") or "").strip(),
            "content": str((message or {}).get("processed_plain_text") or "").strip(),
            "message_id": str((message or {}).get("message_id") or "").strip(),
            "timestamp": (message or {}).get("timestamp"),
        }

    async def _lookup_stream(self, stream_id: str, platform: str = "qq") -> Dict[str, Any]:
        sid = str(stream_id or "").strip()
        if not sid:
            return {}
        try:
            payload = await self.ctx.chat.get_all_streams(platform=platform or "qq")
        except Exception:
            try:
                payload = await self.ctx.chat.get_all_streams(platform="all_platforms")
            except Exception as exc:
                self.ctx.logger.debug("redirect_err 列举聊天流失败: %s", exc)
                return {}

        streams: List[Any]
        if isinstance(payload, list):
            streams = payload
        elif isinstance(payload, dict):
            nested = payload.get("streams")
            streams = nested if isinstance(nested, list) else []
        else:
            streams = []

        for item in streams:
            if not isinstance(item, dict):
                continue
            if self._pick_stream_id(item) == sid:
                return item
        return {}

    def _session_label_from_message_and_stream(
        self,
        message: Optional[Dict[str, Any]],
        stream: Dict[str, Any],
    ) -> Tuple[str, str, str]:
        """返回 (会话描述, group_id, user_id)。"""

        info = self._as_dict((message or {}).get("message_info"))
        group = self._as_dict(info.get("group_info"))
        user = self._as_dict(info.get("user_info"))

        group_id = str(group.get("group_id") or stream.get("group_id") or "").strip()
        group_name = str(group.get("group_name") or stream.get("group_name") or "").strip()
        peer_user_id = str(stream.get("user_id") or "").strip()
        peer_nick = str(
            stream.get("user_nickname") or stream.get("user_cardname") or ""
        ).strip()

        is_group = bool(group_id) or bool(stream.get("is_group_session"))
        if is_group and group_id:
            title = f"群聊 {group_name}({group_id})" if group_name else f"群聊 {group_id}"
            return title, group_id, ""
        if peer_user_id:
            title = f"私聊 {peer_nick}({peer_user_id})" if peer_nick else f"私聊 {peer_user_id}"
            return title, "", peer_user_id
        # 出站消息的 user_info 是 bot，私聊时用 stream 更准
        bot_or_user = str(user.get("user_id") or "").strip()
        if bot_or_user and not is_group:
            nick = str(user.get("user_nickname") or "").strip()
            title = f"私聊 {nick}({bot_or_user})" if nick else f"私聊 {bot_or_user}"
            return title, "", bot_or_user
        return "未知会话", group_id, peer_user_id

    async def _fetch_trigger_message(
        self,
        *,
        stream_id: str,
        reply_to: str,
        bot_user_id: str,
    ) -> Optional[Dict[str, Any]]:
        if reply_to:
            try:
                payload = await self.ctx.message.get_by_id(
                    message_id=reply_to,
                    chat_id=stream_id or None,
                    include_binary_data=False,
                )
                found = self._unwrap_message(payload)
                if found:
                    return found
            except Exception as exc:
                self.ctx.logger.debug("redirect_err get_by_id 失败: %s", exc)

        if not stream_id:
            return None
        try:
            payload = await self.ctx.message.get_recent(
                chat_id=stream_id,
                limit=20,
                hours=24,
                filter_mai=True,
                include_binary_data=False,
            )
        except TypeError:
            # 兼容旧 SDK 参数名差异
            try:
                payload = await self.ctx.message.get_recent(chat_id=stream_id, limit=20, hours=24)
            except Exception as exc:
                self.ctx.logger.debug("redirect_err get_recent 失败: %s", exc)
                return None
        except Exception as exc:
            self.ctx.logger.debug("redirect_err get_recent 失败: %s", exc)
            return None

        for item in self._unwrap_messages(payload):
            fields = self._message_user_fields(item)
            uid = fields["user_id"]
            if bot_user_id and uid == bot_user_id:
                continue
            if fields["content"] or fields["message_id"]:
                return item
        return None

    async def _build_trigger_context_block(
        self,
        *,
        message: Any,
        stream_id: str,
        hit_keyword: str,
        bot_reply_text: str,
    ) -> str:
        max_chars = int(self.config.redirect.trigger_content_max_chars or 500)
        outbound = message if isinstance(message, dict) else {}
        platform = str(outbound.get("platform") or self.config.redirect.platform or "qq").strip() or "qq"
        stream = await self._lookup_stream(stream_id, platform=platform)
        session_label, group_id, private_user_id = self._session_label_from_message_and_stream(outbound, stream)

        outbound_info = self._as_dict(outbound.get("message_info"))
        bot_user = self._as_dict(outbound_info.get("user_info"))
        bot_user_id = str(bot_user.get("user_id") or "").strip()

        hint = self._extract_reply_hint(outbound)
        trigger_msg = await self._fetch_trigger_message(
            stream_id=stream_id,
            reply_to=hint["message_id"],
            bot_user_id=bot_user_id,
        )
        trigger_fields = self._message_user_fields(trigger_msg)

        sender_id = trigger_fields["user_id"] or hint["user_id"]
        sender_name = (
            trigger_fields["cardname"]
            or trigger_fields["nickname"]
            or hint["cardname"]
            or hint["nickname"]
            or ""
        )
        trigger_content = trigger_fields["content"] or hint["content"]
        trigger_id = trigger_fields["message_id"] or hint["message_id"]
        trigger_time = self._format_time(
            trigger_fields.get("timestamp") if trigger_fields.get("timestamp") is not None else outbound.get("timestamp")
        )

        lines = [
            "—— 触发上下文 ——",
            f"时间: {trigger_time}",
            f"会话: {session_label}",
        ]
        if group_id and f"({group_id})" not in session_label:
            lines.append(f"群号: {group_id}")
        if private_user_id and "私聊" not in session_label:
            lines.append(f"私聊用户: {private_user_id}")
        if stream_id:
            lines.append(f"stream_id: {stream_id}")
        if sender_id or sender_name:
            if sender_name and sender_id:
                lines.append(f"发送人: {sender_name} ({sender_id})")
            elif sender_name:
                lines.append(f"发送人: {sender_name}")
            else:
                lines.append(f"发送人: {sender_id}")
        if trigger_id:
            lines.append(f"ID: {trigger_id}")
        lines.append(f"触发消息: {self._truncate(trigger_content, max_chars) or '（无文本 / 未能获取）'}")
        lines.append(f"命中: {hit_keyword}")
        # bot_reply_text 已在正文中，这里不重复整段，只给长度提示
        if bot_reply_text:
            lines.append(f"Bot回复长度: {len(bot_reply_text)} 字")
        return "\n".join(lines)

    async def _resolve_target_stream_id(self) -> str:
        cfg = self.config.redirect
        target_type = str(cfg.target_type or "group").strip().lower()
        platform = str(cfg.platform or "qq").strip() or "qq"

        if target_type == "stream_id":
            return str(cfg.stream_id or "").strip()

        if target_type == "group":
            group_id = str(cfg.group_id or "").strip()
            if not group_id:
                return ""
            found = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            opened = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="group",
                group_id=group_id,
            )
            return self._pick_stream_id(opened)

        if target_type == "private":
            user_id = str(cfg.user_id or "").strip()
            if not user_id:
                return ""
            found = await self.ctx.chat.get_stream_by_user_id(user_id=user_id, platform=platform)
            stream_id = self._pick_stream_id(found)
            if stream_id:
                return stream_id
            opened = await self.ctx.chat.open_session(
                platform=platform,
                chat_type="private",
                user_id=user_id,
            )
            return self._pick_stream_id(opened)

        self.ctx.logger.warning("redirect_err 未知 target_type=%r，已忽略", target_type)
        return ""

    @HookHandler(
        "send_service.after_build_message",
        name="redirect_err_outbound",
        description="命中 error/not 等关键词时抑制或重定向出站消息",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def handle_after_build_message(
        self,
        message: Any = None,
        stream_id: str = "",
        processed_plain_text: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not bool(self.config.plugin.enabled):
            return {"action": "continue"}
        if _IN_REDIRECT.get():
            return {"action": "continue"}

        text = self._extract_outbound_text(message, processed_plain_text)
        hit = self._text_matches(text)
        if not hit:
            return {"action": "continue"}

        mode = str(self.config.action.mode or "suppress").strip().lower()
        source_stream = str(stream_id or "").strip()
        self.ctx.logger.info(
            "redirect_err 命中关键词=%r mode=%s stream_id=%s text=%.120r",
            hit,
            mode,
            source_stream,
            text,
        )

        if mode != "redirect":
            return {"action": "abort"}

        try:
            target_stream = await self._resolve_target_stream_id()
        except Exception as exc:
            self.ctx.logger.error("redirect_err 解析目标会话失败: %s", exc, exc_info=True)
            return {"action": "abort"}

        if not target_stream:
            self.ctx.logger.warning("redirect_err 未配置有效重定向目标，已抑制原消息")
            return {"action": "abort"}

        if source_stream and source_stream == target_stream:
            policy = str(self.config.redirect.when_already_at_target or "continue").strip().lower()
            if policy == "suppress":
                return {"action": "abort"}
            return {"action": "continue"}

        prefix = str(self.config.redirect.prefix or "")
        outbound = f"{prefix}{text}" if prefix else text

        if bool(self.config.redirect.include_trigger_context):
            try:
                context_block = await self._build_trigger_context_block(
                    message=message,
                    stream_id=source_stream,
                    hit_keyword=hit,
                    bot_reply_text=text,
                )
                if context_block:
                    outbound = f"{outbound}\n\n{context_block}"
            except Exception as exc:
                self.ctx.logger.warning("redirect_err 组装触发上下文失败: %s", exc, exc_info=True)

        token = _IN_REDIRECT.set(True)
        try:
            sent = await self.ctx.send.text(outbound, target_stream)
            if not sent:
                self.ctx.logger.warning(
                    "redirect_err 转发失败（目标流不可用） target=%s，已抑制原消息",
                    target_stream,
                )
        except Exception as exc:
            self.ctx.logger.error("redirect_err 转发异常: %s", exc, exc_info=True)
        finally:
            _IN_REDIRECT.reset(token)

        return {"action": "abort"}


def create_plugin() -> RedirectErrPlugin:
    return RedirectErrPlugin()
