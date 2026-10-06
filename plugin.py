"""错误输出重定向插件（MaiBot SDK 2.x）。

监听出站 Hook ``send_service.after_build_message``：当 Bot 待发送纯文本
包含配置关键词（默认 ``error`` / ``not``）时：

- ``suppress``：中止原发送（不输出）；
- ``redirect``：中止原发送，并将文本转发到配置的群聊 / 私聊 / stream_id；
  转发时可附带触发该条回复的上下文（时间、会话、发送人、原文等）。

拦截后可按配置：
- 对该触发消息沉默 N 秒（只挡针对该 msg_id 的 reply）；
- 对该会话沉默 N 秒（挡该会话全部 reply）。
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
import time
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
    config_version: str = Field(default="1.3.1", description="配置版本")


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
    silence_message_seconds: int = Field(
        default=120,
        description="对该触发消息沉默秒数：期间剥离针对该 msg_id 的 reply（0=关闭）",
    )
    silence_session_seconds: int = Field(
        default=0,
        description="对该会话沉默秒数：期间移除整个会话的 reply 工具（0=关闭）",
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
        self._silence_session_until: Dict[str, float] = {}
        self._silence_message_until: Dict[str, float] = {}
        self._suppress_lock = threading.Lock()
        self._target_stream_resolved: Optional[str] = None
        self._match_keywords: List[str] = []
        self._match_needles: List[Tuple[str, Optional[re.Pattern[str]]]] = []
        self._rebuild_match_cache()
        mode = str(self.config.action.mode or "suppress").strip().lower()
        keywords = [str(k).strip() for k in (self.config.match.keywords or []) if str(k).strip()]
        self.ctx.logger.info(
            "redirect_err 已加载：enabled=%s mode=%s keywords=%s silence_message=%ss silence_session=%ss",
            bool(self.config.plugin.enabled),
            mode,
            keywords,
            int(self.config.action.silence_message_seconds or 0),
            int(self.config.action.silence_session_seconds or 0),
        )

    async def on_unload(self) -> None:
        with self._suppress_lock:
            self._silence_session_until.clear()
            self._silence_message_until.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        self._target_stream_resolved = None
        self._rebuild_match_cache()
        self.ctx.logger.info("redirect_err 配置已更新")

    # ─── 沉默窗口 ───────────────────────────────────────────────

    @staticmethod
    def _prune_expired(mapping: Dict[str, float], now: float) -> None:
        expired = [key for key, until in mapping.items() if float(until or 0) <= now]
        for key in expired:
            mapping.pop(key, None)

    def _mark_silence(self, *, stream_id: str, message_id: str) -> Tuple[int, int]:
        """写入沉默窗口，返回 (message_seconds, session_seconds)。"""

        msg_sec = max(0, int(self.config.action.silence_message_seconds or 0))
        sess_sec = max(0, int(self.config.action.silence_session_seconds or 0))
        now = time.time()
        sid = str(stream_id or "").strip()
        mid = str(message_id or "").strip()

        with self._suppress_lock:
            self._prune_expired(self._silence_session_until, now)
            self._prune_expired(self._silence_message_until, now)
            if sess_sec > 0 and sid:
                self._silence_session_until[sid] = now + sess_sec
            if msg_sec > 0 and mid:
                self._silence_message_until[mid] = now + msg_sec

        if msg_sec > 0 and mid:
            self.ctx.logger.info("redirect_err 消息沉默：msg_id=%s %ss", mid, msg_sec)
        elif msg_sec > 0 and not mid:
            self.ctx.logger.warning("redirect_err 无法解析触发 msg_id，消息级沉默未生效（可依赖会话沉默）")
        if sess_sec > 0 and sid:
            self.ctx.logger.info("redirect_err 会话沉默：stream_id=%s %ss", sid, sess_sec)
        return msg_sec, sess_sec

    def _session_silenced(self, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        now = time.time()
        with self._suppress_lock:
            self._prune_expired(self._silence_session_until, now)
            return sid in self._silence_session_until

    def _message_silenced(self, message_id: str) -> bool:
        mid = str(message_id or "").strip()
        if not mid:
            return False
        now = time.time()
        with self._suppress_lock:
            self._prune_expired(self._silence_message_until, now)
            return mid in self._silence_message_until

    def _any_message_silence_active(self) -> bool:
        now = time.time()
        with self._suppress_lock:
            self._prune_expired(self._silence_message_until, now)
            return bool(self._silence_message_until)

    async def _append_handled_context(
        self,
        stream_id: str,
        *,
        mode: str,
        hit: str,
        message_id: str,
        message_seconds: int,
        session_seconds: int,
    ) -> None:
        sid = str(stream_id or "").strip()
        if not sid:
            return
        parts = [
            f"[redirect_err] 本轮出站回复因命中关键词「{hit}」已按策略处理（mode={mode}）。",
        ]
        if message_seconds > 0 and message_id:
            parts.append(f"请勿再对消息 {message_id} 调用 reply（沉默 {message_seconds}s）。")
        if session_seconds > 0:
            parts.append(f"本会话暂时不要调用 reply（沉默 {session_seconds}s）。")
        if message_seconds <= 0 and session_seconds <= 0:
            parts.append("本条出站已拦截；若需继续可处理其它消息。")
        else:
            parts.append("视为相关触发已处理完毕。")
        note = "".join(parts)
        segments = [{"type": "text", "data": note}]
        try:
            maisaka = getattr(self.ctx, "maisaka", None)
            context_api = getattr(maisaka, "context", None) if maisaka is not None else None
            if context_api is not None and hasattr(context_api, "append"):
                await context_api.append(
                    stream_id=sid,
                    segments=segments,
                    visible_text=note,
                    source_kind="plugin:maibot_plugin.redirect_err",
                )
                return
            await self.ctx.call_capability(
                "maisaka.context.append",
                stream_id=sid,
                segments=segments,
                visible_text=note,
                source_kind="plugin:maibot_plugin.redirect_err",
            )
        except Exception as exc:
            self.ctx.logger.warning("redirect_err 写入 Maisaka 上下文失败: %s", exc)

    @staticmethod
    def _tool_call_name(tool_call: Any) -> str:
        if not isinstance(tool_call, dict):
            return ""
        function = tool_call.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "").strip().lower()
        return str(tool_call.get("name") or "").strip().lower()

    @staticmethod
    def _tool_definition_name(tool_def: Any) -> str:
        if not isinstance(tool_def, dict):
            return ""
        function = tool_def.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "").strip().lower()
        return str(tool_def.get("name") or "").strip().lower()

    @staticmethod
    def _reply_tool_msg_id(tool_call: Any) -> str:
        if not isinstance(tool_call, dict):
            return ""
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        args = function.get("arguments") if isinstance(function, dict) else tool_call.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return ""
        if not isinstance(args, dict):
            return ""
        return str(args.get("msg_id") or "").strip()

    def _strip_reply_tool_calls(
        self,
        tool_calls: Any,
        *,
        session_id: str,
    ) -> Tuple[List[Any], bool]:
        if not isinstance(tool_calls, list):
            return [], False
        session_blocked = self._session_silenced(session_id)
        kept: List[Any] = []
        removed = False
        for item in tool_calls:
            if self._tool_call_name(item) != "reply":
                kept.append(item)
                continue
            if session_blocked:
                removed = True
                continue
            target_id = self._reply_tool_msg_id(item)
            if target_id and self._message_silenced(target_id):
                removed = True
                continue
            # 无 msg_id 时：若本会话刚有消息沉默窗口，保守剥离（避免重试漏网）
            if not target_id and self._any_message_silence_active():
                removed = True
                continue
            kept.append(item)
        if removed and not kept:
            kept.append(
                {
                    "id": f"redirect_err_wait_{int(time.time() * 1000)}",
                    "function": {"name": "wait", "arguments": {"seconds": 1}},
                }
            )
        return kept, removed

    def _strip_reply_tool_definitions(self, tool_definitions: Any) -> Tuple[List[Any], bool]:
        if not isinstance(tool_definitions, list):
            return [], False
        kept: List[Any] = []
        removed = False
        for item in tool_definitions:
            if self._tool_definition_name(item) == "reply":
                removed = True
                continue
            kept.append(item)
        return kept, removed

    def _resolve_trigger_message_id(self, message: Any) -> str:
        outbound = message if isinstance(message, dict) else {}
        hint = self._extract_reply_hint(outbound)
        return str(hint.get("message_id") or "").strip()

    async def _after_intercept(self, stream_id: str, *, mode: str, hit: str, message: Any = None) -> None:
        """拦截出站后：写入消息/会话沉默窗口，并提示 Maisaka。"""

        message_id = self._resolve_trigger_message_id(message)
        msg_sec, sess_sec = self._mark_silence(stream_id=stream_id, message_id=message_id)
        if msg_sec > 0 or sess_sec > 0:
            await self._append_handled_context(
                stream_id,
                mode=mode,
                hit=hit,
                message_id=message_id,
                message_seconds=msg_sec,
                session_seconds=sess_sec,
            )

    # ─── 匹配 / 重定向辅助 ──────────────────────────────────────

    def _extract_outbound_text(self, message: Any, processed_plain_text: str) -> str:
        text = str(processed_plain_text or "").strip()
        if text:
            return text
        if isinstance(message, dict):
            return str(message.get("processed_plain_text") or "").strip()
        return ""

    def _rebuild_match_cache(self) -> None:
        """预构建关键词匹配所需数据（列表 + 边界正则），配置更新时重建。"""

        keywords = [str(k).strip() for k in (self.config.match.keywords or []) if str(k).strip()]
        case_sensitive = bool(self.config.match.case_sensitive)
        word_boundary = bool(self.config.match.word_boundary)
        needles: List[Tuple[str, Optional[re.Pattern[str]]]] = []
        for raw in keywords:
            needle = raw if case_sensitive else raw.lower()
            compiled: Optional[re.Pattern[str]] = None
            if word_boundary:
                flags = 0 if case_sensitive else re.IGNORECASE
                compiled = re.compile(rf"(?<!\w){re.escape(raw)}(?!\w)", flags)
            needles.append((needle, compiled))
        self._match_keywords = keywords
        self._match_needles = needles

    def _text_matches(self, text: str) -> Optional[str]:
        if not text or not self._match_keywords:
            return None

        case_sensitive = bool(self.config.match.case_sensitive)
        word_boundary = bool(self.config.match.word_boundary)
        haystack = text if case_sensitive else text.lower()

        for raw, (needle, compiled) in zip(self._match_keywords, self._match_needles):
            if word_boundary:
                if compiled is not None and compiled.search(text):
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

    def _message_user_fields(self, message: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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

    def _session_label_from_message_and_stream(
        self,
        message: Optional[Dict[str, Any]],
        stream: Dict[str, Any],
    ) -> Tuple[str, str, str]:
        info = self._as_dict((message or {}).get("message_info"))
        group = self._as_dict(info.get("group_info"))
        user = self._as_dict(info.get("user_info"))

        group_id = str(group.get("group_id") or stream.get("group_id") or "").strip()
        group_name = str(group.get("group_name") or stream.get("group_name") or "").strip()
        peer_user_id = str(stream.get("user_id") or "").strip()
        peer_nick = str(stream.get("user_nickname") or stream.get("user_cardname") or "").strip()

        is_group = bool(group_id) or bool(stream.get("is_group_session"))
        if is_group and group_id:
            title = f"群聊 {group_name}({group_id})" if group_name else f"群聊 {group_id}"
            return title, group_id, ""
        if peer_user_id:
            title = f"私聊 {peer_nick}({peer_user_id})" if peer_nick else f"私聊 {peer_user_id}"
            return title, "", peer_user_id
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
            uid = str(fields.get("user_id") or "")
            if bot_user_id and uid == bot_user_id:
                continue
            if fields.get("content") or fields.get("message_id"):
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
        # 会话标签优先从出站消息的 message_info 组装；不再全量拉取聊天流列表
        stream: Dict[str, Any] = {}
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

        sender_id = str(trigger_fields.get("user_id") or hint["user_id"] or "")
        sender_name = (
            str(trigger_fields.get("cardname") or "")
            or str(trigger_fields.get("nickname") or "")
            or hint["cardname"]
            or hint["nickname"]
        )
        trigger_content = str(trigger_fields.get("content") or hint["content"] or "")
        trigger_id = str(trigger_fields.get("message_id") or hint["message_id"] or "")
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
            lines.append(f"触发消息ID: {trigger_id}")
        lines.append(f"触发消息: {self._truncate(trigger_content, max_chars) or '（无文本 / 未能获取）'}")
        lines.append(f"命中关键词: {hit_keyword}")
        return "\n".join(lines)

    async def _resolve_target_stream_id(self) -> str:
        # 成功解析结果按配置缓存，避免每次命中都查询/打开会话；配置更新时失效
        if self._target_stream_resolved is not None:
            return self._target_stream_resolved
        resolved = await self._resolve_target_stream_id_uncached()
        if resolved:
            self._target_stream_resolved = resolved
        return resolved

    async def _resolve_target_stream_id_uncached(self) -> str:
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

    # ─── Hooks ──────────────────────────────────────────────────

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
            await self._after_intercept(source_stream, mode="suppress", hit=hit, message=message)
            return {"action": "abort"}

        try:
            target_stream = await self._resolve_target_stream_id()
        except Exception as exc:
            self.ctx.logger.error("redirect_err 解析目标会话失败: %s", exc, exc_info=True)
            await self._after_intercept(source_stream, mode="redirect", hit=hit, message=message)
            return {"action": "abort"}

        if not target_stream:
            self.ctx.logger.warning("redirect_err 未配置有效重定向目标，已抑制原消息")
            await self._after_intercept(source_stream, mode="redirect", hit=hit, message=message)
            return {"action": "abort"}

        if source_stream and source_stream == target_stream:
            policy = str(self.config.redirect.when_already_at_target or "continue").strip().lower()
            if policy == "suppress":
                await self._after_intercept(source_stream, mode="redirect", hit=hit, message=message)
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

        await self._after_intercept(source_stream, mode="redirect", hit=hit, message=message)
        return {"action": "abort"}

    @HookHandler(
        "maisaka.planner.before_request",
        name="redirect_err_block_reply_tools",
        description="会话沉默期间从 Planner 工具列表移除 reply",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def handle_planner_before_request(
        self,
        messages: Any = None,
        tool_definitions: Any = None,
        selected_history_count: int = 0,
        built_message_count: int = 0,
        selection_reason: str = "",
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not bool(self.config.plugin.enabled):
            return {"action": "continue"}
        # 仅会话级沉默时移除整个 reply 工具；消息级沉默保留工具，在 after_response 按 msg_id 过滤
        if not self._session_silenced(session_id):
            return {"action": "continue"}

        filtered, removed = self._strip_reply_tool_definitions(tool_definitions)
        if not removed:
            return {"action": "continue"}

        self.ctx.logger.info("redirect_err 会话沉默：已移除 reply 工具 session_id=%s", session_id)
        return {
            "action": "continue",
            "modified_kwargs": {
                "messages": messages,
                "tool_definitions": filtered,
                "selected_history_count": selected_history_count,
                "built_message_count": built_message_count,
                "selection_reason": selection_reason,
                "session_id": session_id,
            },
        }

    @HookHandler(
        "maisaka.planner.after_response",
        name="redirect_err_strip_reply_calls",
        description="按消息/会话沉默窗口剥离 Planner 的 reply 调用",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def handle_planner_after_response(
        self,
        response: str = "",
        tool_calls: Any = None,
        selected_history_count: int = 0,
        built_message_count: int = 0,
        selection_reason: str = "",
        session_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        if not bool(self.config.plugin.enabled):
            return {"action": "continue"}
        if not self._session_silenced(session_id) and not self._any_message_silence_active():
            return {"action": "continue"}

        filtered, removed = self._strip_reply_tool_calls(tool_calls, session_id=session_id)
        if not removed:
            return {"action": "continue"}

        self.ctx.logger.info("redirect_err 已剥离受限 reply 调用：session_id=%s", session_id)
        return {
            "action": "continue",
            "modified_kwargs": {
                "response": response,
                "tool_calls": filtered,
                "selected_history_count": selected_history_count,
                "built_message_count": built_message_count,
                "selection_reason": selection_reason,
                "session_id": session_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        }


def create_plugin() -> RedirectErrPlugin:
    return RedirectErrPlugin()
