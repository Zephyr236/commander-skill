#!/usr/bin/env python3
"""Hermes 后端 —— 经 Agent Client Protocol (ACP) 驱动。

查证结论（这条比较绕，先记清楚）：
  · `hermes-agent`（Nous Research，0.19.0）本体**只暴露 CLI（`hermes`），
    没有文档化的 Python import API**。它自带技能自生成、SQLite FTS5 持久记忆、
    定时自动化、子代理、多种终端后端。
  · 官方给的程序化驱动路径是 `hermes-acp-sdk`（0.2.0）：通过 ACP 协议以
    子进程方式启动 `hermes acp`，返回 typed 事件流
    （AgentText / AgentThought / ToolCall / PlanUpdated / Usage /
      PermissionDenied / Finished）。
  · 需要额外装 `hermes-agent[acp]`。

⚠️ 诚实标注：这是 6 个后端里**唯一没能实测跑通**的 —— 本机与远端都没装
   hermes，且其 ACP 事件类型的确切 Python 类名未能从公开文档完整核实。
   所以这里写成**分层降级**：
       ① 先试 hermes-acp-sdk 的 typed 事件流
       ② 不行就退到直接驱动 `hermes` CLI
       ③ 再不行给出可诊断的失败信息（而不是静默报错）
   第一次真机上跑时，看 outbox 里的失败信息就知道该走哪条路。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
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
    parts.append(
        f"工作目录是 {spec.workdir}，只能在该目录内创建或修改文件。"
    )
    parts.append(spec.prompt)
    return "\n\n---\n\n".join(p for p in parts if p)


# ── 路线 ①：ACP SDK typed 事件流 ──────────────────────────────────────────
async def via_acp_sdk(spec: Spec, usage: Usage, text_buf: Buffer,
                      reason_buf: Buffer) -> str | None:
    """尝试用 hermes-acp-sdk。API 不匹配时返回 None 让上层降级。"""
    try:
        import hermes_acp_sdk as acp
    except ImportError:
        return None

    # 事件类名按 ACP 语义探测，不硬编码某个具体导出名
    def cls(*candidates):
        for c in candidates:
            if hasattr(acp, c):
                return getattr(acp, c)
        return None

    AgentText = cls("AgentText", "TextEvent")
    AgentThought = cls("AgentThought", "ThoughtEvent")
    ToolCallEv = cls("ToolCall", "ToolCallEvent")
    UsageEv = cls("Usage", "UsageEvent")
    Finished = cls("Finished", "FinishedEvent", "Result")

    session_factory = cls("Session", "Client", "ACPSession", "connect")
    if session_factory is None:
        log("[hermes] acp sdk 里找不到可用的会话入口，降级到 CLI 路线")
        return None

    env = dict(os.environ)
    env.update({
        "HERMES_WORKDIR": spec.workdir,
        "COMMANDER_AGENT_ID": spec.agent_id,
    })

    try:
        session = session_factory(cwd=spec.workdir, env=env)
    except TypeError:
        try:
            session = session_factory(spec.workdir)
        except Exception as exc:
            log(f"[hermes] 会话构造失败，降级到 CLI: {exc}")
            return None

    collected: list[str] = []

    async def consume():
        async for ev in session.run(build_prompt(spec)):
            t = type(ev)
            if AgentText and t is AgentText:
                text_buf.add(getattr(ev, "text", "") or "")
                collected.append(getattr(ev, "text", "") or "")
            elif AgentThought and t is AgentThought:
                reason_buf.add(getattr(ev, "text", "") or "")
            elif ToolCallEv and t is ToolCallEv:
                text_buf.flush()
                emit("tool_call", name=getattr(ev, "name", "?"),
                     arguments=getattr(ev, "arguments", None))
            elif UsageEv and t is UsageEv:
                usage.add_anthropic(ev)
            elif Finished and t is Finished:
                break

    try:
        if hasattr(session, "__aenter__"):
            async with session:
                await consume()
        else:
            await consume()
    except Exception as exc:
        log(f"[hermes] ACP 会话执行失败，降级到 CLI: {exc}")
        return None

    return "".join(collected)


# ── 路线 ②：直接驱动 hermes CLI ──────────────────────────────────────────
def via_cli(spec: Spec, usage: Usage, text_buf: Buffer) -> str | None:
    hermes = shutil.which("hermes")
    if not hermes:
        return None

    log("[hermes] 使用 CLI 路线：hermes -p <prompt>")

    cmd = [hermes, "-p", build_prompt(spec)]
    for flag, val in (
        ("--model", spec.model_id),
        ("--max-tokens", str(spec.max_tokens)),
    ):
        if val:
            cmd += [flag, str(val)]

    env = dict(os.environ)
    env["HERMES_WORKDIR"] = spec.workdir
    if spec.api_key:
        env.setdefault("OPENAI_API_KEY", spec.api_key)
        env.setdefault("ANTHROPIC_API_KEY", spec.api_key)
    if spec.base_url:
        env.setdefault("OPENAI_BASE_URL", spec.base_url)

    try:
        proc = subprocess.run(
            cmd, cwd=spec.workdir, env=env, capture_output=True,
            text=True, timeout=spec.timeout, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"[hermes] CLI 执行失败: {exc}")
        return None

    for line in (proc.stdout or "").splitlines():
        text_buf.add(line + "\n")
    for line in (proc.stderr or "").splitlines():
        log(f"[hermes] {line}")

    if proc.returncode != 0:
        return None
    return text_buf.value_parts()


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        import hermes_agent  # noqa: F401
        have_agent = True
    except ImportError:
        have_agent = False

    usage = Usage()
    text_buf = Buffer("text")
    reason_buf = Buffer("thought")

    # ① ACP SDK
    text = None
    try:
        text = asyncio.run(via_acp_sdk(spec, usage, text_buf, reason_buf))
    except Exception as exc:
        log(f"[hermes] ACP 路线异常: {type(exc).__name__}: {exc}")

    # ② CLI
    if text is None:
        try:
            text = via_cli(spec, usage, text_buf)
        except Exception as exc:
            log(f"[hermes] CLI 路线异常: {type(exc).__name__}: {exc}")

    reason_buf.flush()
    text_buf.flush()

    if text is None:
        fail(
            "Hermes 后端两条路线都没走通。诊断信息：\n"
            f"  · hermes-agent 已安装: {have_agent}\n"
            f"  · hermes 可执行文件: {shutil.which('hermes') or '未找到'}\n"
            f"  · hermes-acp-sdk 可导入: "
            f"{_can_import('hermes_acp_sdk')}\n"
            "排查建议：先手动跑一次 `hermes acp` 确认它本身能起来；"
            "若 CLI 可用而 ACP 不行，说明 acp-sdk 的 API 与预期不同，"
            "看上面的 stderr 日志确认实际事件类型。",
            kind="backend_unavailable", retryable=False,
            model=spec.model_id, backend="hermes",
            duration_s=time.time() - t0,
        )
        return

    usage.emit()
    log(f"[hermes] {len(text)} 字符")
    ok(text.strip(), reasoning=reason_buf.value_parts().strip(), usage=usage,
       model=spec.model_id, backend="hermes", turns=1,
       duration_s=time.time() - t0)


def _can_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    run_main(main, "hermes")
