#!/usr/bin/env python3
"""Claude Agent SDK 后端。

包名 claude-agent-sdk 0.2.160，import 名 **claude_agent_sdk**。
核心 API：`query(prompt, options=ClaudeAgentOptions(...))` → AsyncIterator[Message]
（长驻多轮用 ClaudeSDKClient；一次性派发用 query 更省事）。

几个关键事实（已查证）：
  · 这个 SDK 本质是**驱动 Claude Code CLI 子进程**，CLI 随包捆绑、无需另装
  · 正因为如此，它读的是 Claude Code 的配置体系 —— 我们用 CLAUDE_CONFIG_DIR
    把它指到 agent 自己的 HOME，避免读到指挥官的全局配置
  · 既然跑的是 CLI，它就自带工具/skill/subagent/hook 能力，
    这是其他 SDK 没有的；但也就绑定在本机环境上

接 DeepSeek：用 Anthropic 兼容端点（实测 /v1/messages 可用）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

from commander_protocol import (
    Buffer,
    Spec,
    Usage,
    emit,
    fail,
    log,
    ok,
    run_main,
)


def build_options(spec: Spec, opts_mod):
    """构造 ClaudeAgentOptions。

    只设置确定存在的字段；用 hasattr 探测，避免 SDK 小版本漂移导致报错。
    """
    kwargs: dict = {
        "cwd": spec.workdir,
        "max_turns": spec.max_turns,
    }

    system_prompt = spec.system_prompt or ""
    if spec.skills:
        system_prompt = (system_prompt + "\n\n" + spec.skill_text()).strip()
    if system_prompt:
        kwargs["system_prompt"] = system_prompt

    if spec.model_id:
        kwargs["model"] = spec.model_id

    # 权限：被派发的 agent 不应弹权限询问，否则会永久挂起
    for field, value in (
        ("permission_mode", spec.options.get("permission_mode", "bypassPermissions")),
        ("max_budget_usd", spec.options.get("max_budget_usd")),
        ("allowed_tools", spec.options.get("allowed_tools")),
        ("disallowed_tools", spec.options.get("disallowed_tools")),
        # 不继承宿主的 setting sources —— 否则会读到指挥官的配置
        ("setting_sources", []),
    ):
        if value is not None:
            kwargs[field] = value

    return opts_mod(**kwargs)


async def drive(spec: Spec, usage: Usage, text_buf: Buffer, reason_buf: Buffer) -> str:
    """跑一次 query，把消息流翻译成事件流。返回 session_id。"""
    from claude_agent_sdk import ClaudeAgentOptions, query

    options = build_options(spec, ClaudeAgentOptions)
    session_id = ""
    turns = 0

    async for message in query(prompt=spec.prompt, options=options):
        name = type(message).__name__

        if name == "AssistantMessage":
            turns += 1
            for block in getattr(message, "content", []) or []:
                bname = type(block).__name__
                if bname == "TextBlock":
                    text_buf.add(getattr(block, "text", "") or "")
                elif bname in ("ThinkingBlock", "RedactedThinkingBlock"):
                    reason_buf.add(getattr(block, "thinking", "") or "")
                elif bname == "ToolUseBlock":
                    text_buf.flush()
                    emit("tool_call",
                         name=getattr(block, "name", "?"),
                         arguments=getattr(block, "input", None),
                         id=getattr(block, "id", None))

        elif name == "UserMessage":
            # 工具结果回灌
            for block in getattr(message, "content", []) or []:
                if type(block).__name__ == "ToolResultBlock":
                    emit("tool_result", result=str(getattr(block, "content", ""))[:2000])

        elif name == "ResultMessage":
            session_id = getattr(message, "session_id", "") or ""
            u = getattr(message, "usage", None)
            if u:
                usage.add_anthropic(u)
                # claude-agent-sdk 的 usage 可能是 dict，也可能嵌在 model_usage 里
                if isinstance(u, dict):
                    for extra in (u.get("model_usage") or {}).values():
                        if isinstance(extra, dict):
                            usage.add({
                                "input_tokens": extra.get("inputTokens"),
                                "output_tokens": extra.get("outputTokens"),
                                "cached_tokens": extra.get("cacheReadInputTokens"),
                            })
            cost = getattr(message, "total_cost_usd", None)
            if cost is not None:
                emit("log", message=f"SDK 报告成本 ${cost}")

    return session_id


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as exc:
        fail(f"需要 claude-agent-sdk: {exc}。"
             f"安装：uv sync --project bin/backends/claude",
             kind="backend_missing_dep", retryable=False, backend="claude")
        return

    usage = Usage()
    text_buf = Buffer("text")
    reason_buf = Buffer("thought")

    try:
        session_id = asyncio.run(drive(spec, usage, text_buf, reason_buf))
    except Exception as exc:
        reason_buf.flush()
        text_buf.flush()
        text = text_buf.value_parts()
        log(f"[claude] 异常: {type(exc).__name__}: {exc}")
        fail(f"{type(exc).__name__}: {exc}",
             text=text, usage=usage, model=spec.model_id, backend="claude",
             duration_s=time.time() - t0)
        return

    reason_buf.flush()
    text_buf.flush()

    text = text_buf.value_parts().strip()
    reasoning = reason_buf.value_parts().strip()
    usage.emit()

    log(f"[claude] session={session_id} {len(text)} 字符")
    ok(text, reasoning=reasoning, usage=usage,
       model=spec.model_id, backend="claude", turns=1,
       duration_s=time.time() - t0,
       raw={"session_id": session_id})


if __name__ == "__main__":
    run_main(main, "claude")
