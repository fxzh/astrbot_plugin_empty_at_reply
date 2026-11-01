"""空 @ 响应：单独 @ 机器人（不带任何内容）时让机器人正常回复。

问题根因（QQ 官方机器人 qq_official 平台）：
1. 官方适配器解析消息时会无条件追加一个 Plain 文本段（即使内容为空），
   空 @ 消息的链形态恒为 [At, Plain("")]（两段）；
2. 内置「空@等待」功能（builtin_stars/astrbot/main.py）只在消息链
   **恰好只有一个段且是 @** 时才触发，两段形态永远不满足；
3. LLM 阶段（process_stage/.../internal.py）又会因 message_str 为空、
   无媒体无引用而直接跳过请求。三层叠加导致空 @ 完全无响应。

本插件在唤醒检查之后以最高优先级（priority=maxsize，先于内置的
maxsize-1）接管这类消息，把它重摆成「@ + 一句话」的普通形状：
- message_str 非空 → 通过 LLM 阶段的空消息检查；
- 两个消息段 → 绕开内置「空@等待」的 len==1 判断，避免双重处理；
- 正常流程带着完整对话上下文回复（与普通 @ 消息完全一致）。

可选等待窗口：回复后的一段时间内，触发空 @ 的那位用户的下一条消息
（无需再 @）会作为正式内容继续对话；按发送者限定，不抓同群其他人的话。
"""

from __future__ import annotations

import asyncio
import copy
from sys import maxsize

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from astrbot.api import logger

try:  # 兼容不同版本的 AstrBot
    from astrbot.api import AstrBotConfig
except ImportError:
    try:
        from astrbot.api.config import AstrBotConfig
    except ImportError:
        AstrBotConfig = None

try:
    from astrbot.core.utils.session_waiter import (
        USER_SESSIONS,
        SessionController,
        SessionFilter,
        session_waiter,
    )
except ImportError:  # session_waiter 不可用时仅禁用等待窗口功能
    USER_SESSIONS = {}
    SessionController = None
    SessionFilter = None
    session_waiter = None


if SessionFilter is not None:

    class _SenderSessionFilter(SessionFilter):
        """等待窗口只认刚才 @ 机器人的那个人，同群其他人说话不会被抓走。"""

        def filter(self, event: AstrMessageEvent) -> str:
            return f"{event.unified_msg_origin}:{event.get_sender_id()}"


@register(
    "astrbot_plugin_empty_at_reply",
    "fengxue",
    "单独 @ 机器人（不带任何内容）时也正常回复，修复 qq_official 等平台空 @ 无响应",
    "1.0.0",
)
class EmptyAtReply(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default):
        try:
            if self.config is not None:
                value = self.config.get(key, default)
            else:
                value = default
        except Exception:
            return default
        return default if value is None else value

    def _int_cfg(
        self,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    # ------------------------------------------------------------------ 判断

    @staticmethod
    def _is_empty_at(event: AstrMessageEvent) -> bool:
        """是否为「只 @ 了机器人、没有任何实际内容」的消息。

        兼容两种链形态：
        - [At]（多数平台）
        - [At, Plain("")]（qq_official 官方适配器会无条件追加空文本段）

        出现其他任何消息段（实际文本 / 引用 / 图片 / 文件 / 语音 /
        视频 / 表情 / @别人等）一律不接管，走正常流程。
        """
        has_at_bot = False
        for message in event.get_messages():
            if isinstance(message, Comp.At):
                if str(message.qq) != str(event.get_self_id()):
                    return False  # 还 @ 了别人
                has_at_bot = True
            elif isinstance(message, Comp.Plain):
                if (getattr(message, "text", "") or "").strip():
                    return False  # 有实际文本
            else:
                return False  # 媒体/引用/表情/AtAll 等其他段
        return has_at_bot

    def _at_name(self, event: AstrMessageEvent) -> str:
        """机器人昵称：优先取 @ 段携带的名称，拿不到用兜底称呼。"""
        for message in event.get_messages():
            if isinstance(message, Comp.At) and str(message.qq) == str(
                event.get_self_id()
            ):
                name = str(getattr(message, "name", "") or "").strip()
                if name and not name.isdigit():
                    return name
                break
        fallback = str(self._cfg("bot_fallback_name", "bot") or "bot").strip()
        return fallback or "bot"

    def _placeholder(self, event: AstrMessageEvent) -> str:
        """空 @ 时交给模型的占位文本。"""
        text = str(self._cfg("placeholder_text", "") or "").strip()
        if text:
            return text
        return f"@{self._at_name(event)}"

    # ------------------------------------------------------------------ 等待

    async def _wait_followup(self, event: AstrMessageEvent) -> None:
        wait_seconds = self._int_cfg("wait_seconds", 60, 1, 600)

        @session_waiter(wait_seconds)
        async def followup_waiter(
            controller: SessionController,
            next_event: AstrMessageEvent,
        ) -> None:
            text = (next_event.message_str or "").strip()
            has_image = any(
                isinstance(m, Comp.Image) for m in next_event.get_messages()
            )
            if not text and not has_image:
                return  # 空消息不消费，继续等
            # 摆成「@ + 内容」的普通形状重新入队，走完整管线
            next_event.message_obj.message.insert(
                0,
                Comp.At(
                    qq=next_event.get_self_id(),
                    name=next_event.get_self_id(),
                ),
            )
            self.context.get_event_queue().put_nowait(copy.copy(next_event))
            next_event.stop_event()
            controller.stop()

        try:
            await followup_waiter(event, session_filter=_SenderSessionFilter())
        except TimeoutError:
            pass
        except Exception:
            logger.exception("空 @ 等待后续输入失败")

    # ------------------------------------------------------------------ 主逻辑

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def handle_empty_at(self, event: AstrMessageEvent):
        if not self._cfg("enabled", True):
            return
        if not self._is_empty_at(event):
            return

        # 已有等待窗口挂着（本插件/内置空@等待/其他插件）时不叠加
        sender_session = f"{event.unified_msg_origin}:{event.get_sender_id()}"
        if (
            event.unified_msg_origin in USER_SESSIONS
            or sender_session in USER_SESSIONS
        ):
            return

        placeholder = self._placeholder(event)

        # 重摆成「@ + 一句话」的普通消息形状：
        # - message_str 非空 → 通过 LLM 阶段的空消息检查；
        # - 两个消息段 → 绕开内置「空@等待」的 len==1 判断。
        event.message_str = placeholder
        event.message_obj.message = [
            Comp.At(qq=event.get_self_id(), name=self._at_name(event)),
            Comp.Plain(text=placeholder),
        ]
        logger.info(f"空 @ 消息已改写为普通消息: {placeholder}")

        if session_waiter is not None and self._cfg("wait_followup", True):
            asyncio.create_task(self._wait_followup(event))
            await asyncio.sleep(0)  # 让等待先注册，再交回正常流程
