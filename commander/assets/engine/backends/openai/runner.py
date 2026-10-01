#!/usr/bin/env python3
"""OpenAI Agents SDK 后端。

包名 openai-agents 0.22.3。核心：`Agent(...)` + `Runner.run(agent, input)`。

接非 OpenAI 端点的三个**必须**（都是查证到的坑，踩了会 401 或连错端点）：
  1. 必须走 Chat Completions，不能用 Responses API
     → 用 `OpenAIChatCompletionsModel`，它是"几乎所有 provider 都支持的最低公分母"
  2. **必须 `set_tracing_disabled(True)`** —— 外部模型不支持 OpenAI tracing，
     不关会去连 OpenAI 的 tracing 端点然后 401
  3. base_url 要挂在 `AsyncOpenAI` 实例上，不能传给 `OpenAIProvider` 参数
     （0.22.0 起 `openai_client` 与 `organization`/`project` 同传会抛 UserError）

版本策略提醒：openai-agents 是 0.Y.Z，**minor 递增带破坏性变更**。
升级时不要只看 patch。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

from commander_protocol import (
    Spec,
    Usage,
    emit,
    fail,
    log,
    ok,
    run_main,
)


def build_instructions(spec: Spec) -> str:
    parts = []
    if spec.system_prompt:
        parts.append(spec.system_prompt)
    if spec.skills:
        parts.append(spec.skill_text())
    parts.append(
        f"你的工作目录是 {spec.workdir}，只能在该目录内创建或修改文件。"
        f"最终结论直接作为回复给出。"
    )
    return "\n\n---\n\n".join(parts)


async def drive(spec: Spec) -> tuple[str, Usage, list]:
    """跑一次 Runner.run，返回 (文本, 用量, 工具调用列表)。"""
    from agents import (
        Agent,
        ModelSettings,
        OpenAIChatCompletionsModel,
        Runner,
        set_default_openai_api,
        set_tracing_disabled,
    )
    from openai import AsyncOpenAI

    # 坑 2：不关 tracing 会 401
    set_tracing_disabled(True)
    # 坑 1：强制走 Chat Completions
    set_default_openai_api("chat_completions")

    # 坑 3：base_url 挂在 client 上
    client = AsyncOpenAI(api_key=spec.api_key, base_url=spec.base_url or None)

    model = OpenAIChatCompletionsModel(model=spec.model_id, openai_client=client)

    agent = Agent(
        name=spec.agent_id or "worker",
        instructions=build_instructions(spec),
        model=model,
        model_settings=ModelSettings(
            max_tokens=spec.max_tokens,
            # 部分第三方后端不上报 usage，需要显式要求
            include_usage=True,
        ),
    )

    result = await Runner.run(agent, input=spec.prompt, max_turns=spec.max_turns)

    text = str(getattr(result, "final_output", "") or "")

    usage = Usage()
    tool_calls: list = []
    for item in (getattr(result, "new_items", None) or []):
        if type(item).__name__ == "ToolCallItem":
            raw = getattr(item, "raw_item", None)
            tool_calls.append({
                "name": getattr(raw, "name", "?"),
                "arguments": str(getattr(raw, "arguments", "")),
            })

    # usage 的取法有好几处，都要试 —— openai-agents 各版本放的位置不同。
    # ⚠️ 之前这里写了个 `not isinstance(v, list)` 把 raw_responses 整个排除了，
    #    而那恰恰是 usage 最常在的地方，导致 token 统计恒为 0（且不报错）。
    #    教训：提取外部结构的字段时，不要用"排除某些形状"的过滤，要逐个试。
    collected = False

    # ① 逐条 ModelResponse 取（最常见）
    for resp in (getattr(result, "raw_responses", None) or []):
        u = getattr(resp, "usage", None)
        if u:
            usage.add_openai(u)
            collected = True

    # ② 聚合 usage 对象
    for holder in (getattr(result, "context_wrapper", None), result):
        u = getattr(holder, "usage", None) if holder is not None else None
        if u and not isinstance(u, list):
            usage.add_openai(u)
            collected = True

    # ③ 最后一条 ModelResponse 兜底
    if not collected:
        resps = getattr(result, "raw_responses", None) or []
        if resps:
            usage.add_openai(getattr(resps[-1], "usage", None))

    return text, usage, tool_calls


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        import agents  # noqa: F401
    except ImportError as exc:
        fail(f"需要 openai-agents: {exc}。"
             f"安装：uv sync --project bin/backends/openai",
             kind="backend_missing_dep", retryable=False, backend="openai")
        return

    if not spec.api_key:
        fail("缺少 API 密钥", kind="auth", retryable=False, backend="openai")
        return

    try:
        text, usage, tool_calls = asyncio.run(drive(spec))
    except Exception as exc:
        log(f"[openai] 异常: {type(exc).__name__}: {exc}")
        fail(f"{type(exc).__name__}: {exc}",
             model=spec.model_id, backend="openai", duration_s=time.time() - t0)
        return

    for tc in tool_calls:
        emit("tool_call", name=tc["name"], arguments=tc["arguments"])
    usage.emit()

    if text:
        emit("text", text=text)

    log(f"[openai] {len(text)} 字符")
    ok(text, tool_calls=tool_calls, usage=usage,
       model=spec.model_id, backend="openai", turns=1,
       duration_s=time.time() - t0)


if __name__ == "__main__":
    run_main(main, "openai")
