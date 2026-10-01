#!/usr/bin/env python3
"""LangChain / LangGraph agent 后端。

⚠️ API 已变（查证结果，很多教程还是旧的）：
    `create_react_agent` **已废弃**，从 `langgraph.prebuilt` 迁到 `langchain.agents`，
    并被 `create_agent` 取代。官方原文：
      "This function is deprecated in favor of create_agent from the langchain
       package, which provides an equivalent agent factory with a flexible
       middleware system."
    所以这里用 `from langchain.agents import create_agent`。
    版本：langchain 1.4.x + langgraph 1.2.x

接 DeepSeek 的坑：
  · `use_responses_api` 走 Chat Completions 还是 Responses **部分由模型名推断，
    与 base_url 无关** —— 接兼容端点必须**显式**设 False，否则可能选错端点
  · base_url 指向服务器**根**，工具会自动补 /chat/completions，不要自己带
  · 警惕 OPENAI_API_KEY 环境变量**静默覆盖**你显式传的 api_key
"""

from __future__ import annotations

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


def build_prompt(spec: Spec) -> str:
    parts = []
    if spec.system_prompt:
        parts.append(spec.system_prompt)
    if spec.skills:
        parts.append(spec.skill_text())
    parts.append(spec.prompt)
    return "\n\n---\n\n".join(p for p in parts if p)


def usage_from_messages(messages: list, usage: Usage) -> None:
    """从 LangChain 的 AIMessage 里抠 usage_metadata。"""
    for m in messages or []:
        meta = getattr(m, "usage_metadata", None)
        if not meta:
            continue
        if isinstance(meta, dict):
            usage.add({
                "input_tokens": meta.get("input_tokens"),
                "output_tokens": meta.get("output_tokens"),
            })
        else:
            usage.add({
                "input_tokens": getattr(meta, "input_tokens", 0),
                "output_tokens": getattr(meta, "output_tokens", 0),
            })
        rmeta = getattr(m, "response_metadata", None) or {}
        if isinstance(rmeta, dict):
            tu = rmeta.get("token_usage") or {}
            if isinstance(tu, dict) and tu.get("completion_tokens_details"):
                d = tu["completion_tokens_details"]
                if isinstance(d, dict):
                    usage.add(reasoning_tokens=d.get("reasoning_tokens"))


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        from langchain.agents import create_agent
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        fail(f"需要 langchain/langgraph/langchain-openai: {exc}。"
             f"安装：uv sync --project bin/backends/langchain",
             kind="backend_missing_dep", retryable=False, backend="langchain")
        return

    if not spec.api_key:
        fail("缺少 API 密钥", kind="auth", retryable=False, backend="langchain")
        return

    # 坑：显式关掉 Responses API —— 兼容端点基本都不支持
    llm = ChatOpenAI(
        model=spec.model_id,
        api_key=spec.api_key,
        base_url=(spec.base_url or "").rstrip("/") or None,
        use_responses_api=False,
        temperature=spec.options.get("temperature", 0.0),
        max_tokens=spec.max_tokens,
        timeout=spec.timeout,
        max_retries=0,      # 重试交给指挥官的 policy 统一管，别在这里偷偷重试
    )

    agent = create_agent(
        model=llm,
        tools=spec.options.get("tools", []),
        system_prompt=(
            (spec.system_prompt + "\n\n" + spec.skill_text()).strip()
            if spec.skills else spec.system_prompt
        ) or None,
    )

    usage = Usage()
    text_buf = Buffer("text")

    try:
        state = agent.invoke(
            {"messages": [{"role": "user", "content": build_prompt(spec)}]},
            config={"recursion_limit": max(4, spec.max_turns * 2)},
        )
    except Exception as exc:
        text_buf.flush()
        log(f"[langchain] 异常: {type(exc).__name__}: {exc}")
        fail(f"{type(exc).__name__}: {exc}",
             text=text_buf.value_parts(), model=spec.model_id,
             backend="langchain", duration_s=time.time() - t0)
        return

    messages = (state or {}).get("messages") or []

    # 中间步骤作为事件留痕，让指挥官看得见 agent 干了什么
    for m in messages[:-1]:
        mname = type(m).__name__
        if mname == "AIMessage" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                emit("tool_call", name=tc.get("name"),
                     arguments=tc.get("args"), id=tc.get("id"))
        elif mname == "ToolMessage":
            emit("tool_result", result=str(getattr(m, "content", ""))[:2000])

    usage_from_messages(messages, usage)

    final = messages[-1] if messages else None
    text = str(getattr(final, "content", "") or "") if final else ""
    if isinstance(getattr(final, "content", None), list):
        # 多模态 content 是块列表，拼出其中的文本
        text = "".join(
            b.get("text", "") for b in final.content if isinstance(b, dict)
        )

    reasoning = ""
    if final is not None:
        reasoning = str(getattr(final, "additional_kwargs", {}).get(
            "reasoning_content", "") or "")

    if reasoning:
        emit("thought", text=reasoning)
    if text:
        emit("text", text=text)
    usage.emit()

    log(f"[langchain] {len(text)} 字符, {len(messages)} 条消息")
    ok(text, reasoning=reasoning, usage=usage,
       model=spec.model_id, backend="langchain", turns=len(messages),
       duration_s=time.time() - t0)


if __name__ == "__main__":
    run_main(main, "langchain")
