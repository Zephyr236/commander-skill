#!/usr/bin/env python3
"""OpenAI 兼容端点直连后端。

定位：**兜底与探针**。不依赖任何 agent SDK，只用 httpx 打
`POST {base_url}/chat/completions`。凡是兼容这个协议的服务都能接
（DeepSeek / vLLM / Ollama / OpenRouter / Groq / 本地推理……）。

两个用途：
  1. 最便宜的健康检查 —— 验证某个端点和密钥到底通不通
  2. 当某个 SDK 装不上或行为诡异时，作为可靠的对照组

实测要点（DeepSeek）：
  · 支持 function calling，但本后端只做单轮，工具不在这里实现
  · deepseek-flash 是推理模型，会返回 reasoning_content，
    必须与 content 分开收集；max_tokens 给小了会被推理吃光
"""

from __future__ import annotations

import json
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


def build_messages(spec: Spec) -> list[dict]:
    """system 由 system_prompt + 技能正文 + 契约说明拼成。"""
    parts = []
    if spec.system_prompt:
        parts.append(spec.system_prompt)
    if spec.skills:
        parts.append(spec.skill_text())
    parts.append(
        f"你是一名被指挥官派发的执行者。你的工作目录是 {spec.workdir}，"
        f"只能在该目录内创建或修改文件。"
        f"最终结论直接作为回复内容给出，不要写进文件。"
    )
    return [
        {"role": "system", "content": "\n\n---\n\n".join(p for p in parts if p)},
        {"role": "user", "content": spec.prompt},
    ]


def main(spec: Spec) -> None:
    t0 = time.time()
    try:
        import httpx
    except ImportError:
        fail("需要 httpx。安装：uv sync --project bin/backends/openai_compat",
             kind="backend_missing_dep", retryable=False, backend="openai_compat")
        return

    base = (spec.base_url or "").rstrip("/")
    if not base:
        fail("规格里没有 base_url —— 该 provider 未配置 OpenAI 兼容端点",
             kind="bad_request", retryable=False, backend="openai_compat")
        return
    if not spec.api_key:
        fail("缺少 API 密钥。检查 bin/.env 中该 provider 的 api_key_env",
             kind="auth", retryable=False, backend="openai_compat")
        return

    url = f"{base}/chat/completions"
    payload = {
        "model": spec.model_id,
        "messages": build_messages(spec),
        "max_tokens": spec.max_tokens,
        "stream": True,
    }
    if spec.options.get("temperature") is not None:
        payload["temperature"] = spec.options["temperature"]
    # DeepSeek 的思考强度（low/high/max）
    if spec.options.get("effort"):
        payload["reasoning_effort"] = spec.options["effort"]

    headers = {
        "Authorization": f"Bearer {spec.api_key}",
        "Content-Type": "application/json",
    }

    emit("log", message=f"POST {url} model={spec.model_id} max_tokens={spec.max_tokens}")

    usage = Usage()
    text_buf = Buffer("text")
    reason_buf = Buffer("thought")

    try:
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=20.0)) as client, \
                client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")[:1500]
                    fail(f"HTTP {resp.status_code}: {body}",
                         backend="openai_compat", model=spec.model_id,
                         duration_s=time.time() - t0)
                    return

                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("usage"):
                        usage.add_openai(chunk["usage"])

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}

                        # 推理内容与正文必须分开 —— 混在一起会污染最终结论
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if rc:
                            reason_buf.add(rc)

                        c = delta.get("content")
                        if c:
                            text_buf.add(c)

                        for tc in delta.get("tool_calls") or []:
                            fn = tc.get("function") or {}
                            emit("tool_call", name=fn.get("name"),
                                 arguments=fn.get("arguments"), id=tc.get("id"))

    except httpx.HTTPError as exc:
        text_buf.flush()
        fail(f"HTTP 错误: {type(exc).__name__}: {exc}",
             backend="openai_compat", model=spec.model_id,
             duration_s=time.time() - t0)
        return

    reason_buf.flush()
    text_buf.flush()

    text = text_buf.value_parts()
    reasoning = reason_buf.value_parts()

    if not text and reasoning:
        # 推理吃光了 token 预算 —— 这是实测踩过的坑，要给出可诊断的信息
        fail(
            f"只有推理内容没有正文。max_tokens={spec.max_tokens} 可能被 "
            f"reasoning_content 吃光了（实测该模型是推理模型）。"
            f"建议提高 max_tokens（当前模型下限 "
            f"{spec.model.get('max_tokens_floor', 4096)}）。",
            kind="budget", retryable=False, backend="openai_compat",
            model=spec.model_id, duration_s=time.time() - t0,
        )
        return

    usage.emit()
    log(f"[openai_compat] {len(text)} 字符, {usage.output_tokens} 输出 tokens")
    ok(text, reasoning=reasoning, usage=usage,
       model=spec.model_id, backend="openai_compat", turns=1,
       duration_s=time.time() - t0)


if __name__ == "__main__":
    run_main(main, "openai_compat")
