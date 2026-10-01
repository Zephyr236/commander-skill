#!/usr/bin/env python3
"""AutoGen 后端 —— 多智能体对话，适合对抗式评审。

⚠️ 重要现状（查证结果，选型时必须知道）：
    **AutoGen 已进入维护模式** —— 微软只收 bug/安全修复与文档，官方指向
    后继者 Microsoft Agent Framework (MAF)。旧包 `pyautogen`（0.2.x API）
    已废弃，且 0.2.x 与 0.4+ API **互不兼容**。
    另有原班人马 fork 的 AG2 接管了 `autogen`/`pyautogen` 包名。

    本后端用当前官方线 `autogen-agentchat` 0.7.5 + `autogen-ext[openai]`。
    config/models.toml 里把它的 status 标为 active 但语义上属"夕阳技术"，
    指挥官的选型策略会优先选 reasoner / crew，只在需要观点碰撞时用它。
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

# ⚠️ AutoGen 对 agent 名有硬约束：只允许字母、数字、下划线、连字符。
# 用中文名会抛 ValueError: Invalid name。
# 所以内部用 ASCII 名，中文角色说明写进 system_message。
DEFAULT_PANEL = ["pro", "con", "judge"]

ROLE_BRIEF = {
    "pro": "支持并论证一个立场，给出最强论据。",
    "con": "挑战正方，指出其漏洞、边界条件与反例。",
    "judge": "综合双方论点，给出平衡的结论与不确定性说明。",
}

ROLE_LABEL = {"pro": "正方", "con": "反方", "judge": "裁判"}

# 把任意角色名映射到 AutoGen 能接受的 ASCII 名
_ASCII_MAP = {"正方": "pro", "反方": "con", "裁判": "judge",
              "支持": "pro", "反对": "con", "中立": "judge"}


def _safe_name(name: str) -> str:
    """AutoGen 只接受 [A-Za-z0-9_-]。中文名映射成对应英文，其余做转换。"""
    if name in _ASCII_MAP:
        return _ASCII_MAP[name]
    cleaned = "".join(c if (c.isascii() and (c.isalnum() or c in "_-")) else "-"
                      for c in name).strip("-")
    return cleaned or "agent"


async def drive(spec: Spec, usage: Usage) -> tuple[str, int]:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.conditions import MaxMessageTermination
    from autogen_agentchat.teams import RoundRobinGroupChat
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    client = OpenAIChatCompletionClient(
        model=spec.model_id,
        api_key=spec.api_key,
        base_url=(spec.base_url or "").rstrip("/") or None,
        timeout=spec.timeout,
        max_retries=0,
        # 第三方端点的模型能力元数据要显式给出，否则 AutoGen 会去查内置表
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": False,
            "structured_output": False,
            "family": "unknown",
        },
    )

    context = []
    if spec.system_prompt:
        context.append(spec.system_prompt)
    if spec.skills:
        context.append(spec.skill_text())

    contract = (
        f"工作目录是 {spec.workdir}，不得在该目录外创建或修改文件。"
    )

    panel = spec.options.get("panel") or DEFAULT_PANEL
    # 名字必须过 AutoGen 的 ASCII 校验，中文只能出现在 system_message 里
    panel = [_safe_name(n) for n in panel]

    agents = []
    for name in panel:
        agents.append(AssistantAgent(
            name=name,
            model_client=client,
            system_message=(
                f"{'  '.join(context)}\n\n{contract}\n\n"
                f"你在本次讨论中的角色是「{ROLE_LABEL.get(name, name)}」："
                f"{ROLE_BRIEF.get(name, '贡献你的专业判断。')}"
            ),
        ))

    team = RoundRobinGroupChat(
        agents,
        termination_condition=MaxMessageTermination(
            max_messages=max(3, spec.max_turns)
        ),
    )

    result = await team.run(task=spec.prompt)

    messages = getattr(result, "messages", None) or []
    transcript_parts = []
    for m in messages:
        src = getattr(m, "source", "?")
        content = str(getattr(m, "content", "") or "")
        if content:
            transcript_parts.append(f"### {src}\n\n{content}")
            emit("text", text=f"\n\n### {src}\n\n{content}")
        u = getattr(m, "models_usage", None) or getattr(m, "usage", None)
        if u:
            usage.add_openai(u)

    return "\n\n".join(transcript_parts), len(messages)


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        import autogen_agentchat  # noqa: F401
    except ImportError as exc:
        fail(f"需要 autogen-agentchat / autogen-ext: {exc}。"
             f"安装：uv sync --project bin/backends/autogen",
             kind="backend_missing_dep", retryable=False, backend="autogen")
        return

    if not spec.api_key:
        fail("缺少 API 密钥", kind="auth", retryable=False, backend="autogen")
        return

    usage = Usage()
    try:
        text, turns = asyncio.run(drive(spec, usage))
    except Exception as exc:
        log(f"[autogen] 异常: {type(exc).__name__}: {exc}")
        fail(f"{type(exc).__name__}: {exc}",
             model=spec.model_id, backend="autogen", duration_s=time.time() - t0)
        return

    usage.emit()
    log(f"[autogen] {turns} 条消息, {len(text)} 字符")
    ok(text, usage=usage, model=spec.model_id, backend="autogen",
       turns=turns, duration_s=time.time() - t0,
       raw={"maintenance_mode": True,
            "note": "AutoGen 已进入维护模式，微软转向 MAF"})


if __name__ == "__main__":
    run_main(main, "autogen")
