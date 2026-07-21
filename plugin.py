"""错误输出重定向插件（MaiBot SDK 2.x）。

监听出站 Hook ``send_service.after_build_message``：当 Bot 待发送纯文本
包含配置关键词（默认 ``error`` / ``not``）时：

- ``suppress``：中止原发送（不输出）；
- ``redirect``：中止原发送，并将文本转发到配置的群聊 / 私聊 / stream_id。
"""

from __future__ import annotations

import contextvars
import re
from typing import Any, Dict, List, Optional

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
    config_version: str = Field(default="1.0.0", description="配置版本")


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
            "redirect_err 已加载：enabled=%s mode=%s keywords=%s",
            bool(self.config.plugin.enabled),
            mode,
            keywords,
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
