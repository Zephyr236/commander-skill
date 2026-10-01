#!/usr/bin/env python3
"""CrewAI 后端 —— 角色分工式协作。

⚠️ 依赖体积：实测 136 个包（pydantic 被钉在 2.12.5）。
   本地实测只有 2Gi 可用内存，装不下也不该装 —— 这个后端默认落远端执行。

价值主张：CrewAI 让一个任务在内部再分角色（研究/写作/审校）走流水线，
适合产出型任务（长文档、调研报告），与单 agent 的 reasoner 形成互补。

API：Agent / Task / Crew / Process，异步用 `akickoff()`（原生 async，推荐）。
CrewAI 1.15.x 官方正在把重心转向 Crews + Flows 与声明式 JSONC 定义，
经典 Agent/Task/Crew 写法仍受支持。
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

# 派发时若 options 没指定角色，用这套默认编制
DEFAULT_ROLES = [
    ("研究员", "把问题拆开，找出关键事实与不确定性", "要点清单，每条附依据"),
    ("执行者", "基于要点给出可落地的结论或方案", "结构化的最终答复"),
]


def make_llm(spec: Spec, LLM):
    """构造 CrewAI 的 LLM。

    CrewAI 经 litellm 走路由，OpenAI 兼容端点要写成 `openai/<model>`
    并把 base_url 一起给出。
    """
    return LLM(
        model=f"openai/{spec.model_id}",
        base_url=(spec.base_url or "").rstrip("/") or None,
        api_key=spec.api_key,
        temperature=spec.options.get("temperature", 0.0),
        max_tokens=spec.max_tokens,
        timeout=spec.timeout,
    )


async def drive(spec: Spec, usage: Usage, text_buf: Buffer) -> str:
    from crewai import LLM, Agent, Crew, Process, Task

    llm = make_llm(spec, LLM)

    context = []
    if spec.system_prompt:
        context.append(spec.system_prompt)
    if spec.skills:
        context.append(spec.skill_text())
    background = "\n\n".join(context) or "你是被指挥官派发的执行者。"
    contract = (
        f"工作目录是 {spec.workdir}，只能在该目录内创建或修改文件。"
        f"最终结论直接返回，不要写进文件。"
    )

    roles = spec.options.get("roles") or DEFAULT_ROLES
    agents, tasks = [], []

    for i, role in enumerate(roles):
        name, goal, expected = ((*role, ""))[:3] if isinstance(role, (list, tuple)) \
            else (str(role), "", "")
        a = Agent(
            role=name, goal=goal or name,
            backstory=f"{background}\n\n{contract}",
            llm=llm, verbose=False, allow_delegation=False,
        )
        agents.append(a)
        tasks.append(Task(
            description=(
                f"{spec.prompt}\n\n你的角色是「{name}」：{goal}"
                if i < len(roles) - 1 else
                f"{spec.prompt}\n\n综合前序产出，给出最终结论。"
            ),
            expected_output=expected or "完成该角色的职责并给出结论",
            agent=a,
        ))

    crew = Crew(
        agents=agents, tasks=tasks, process=Process.sequential,
        verbose=False, memory=False,
    )

    result = await crew.akickoff()

    # CrewAI 的 usage 在各处放法不一，尽力抠出来
    for a in agents:
        m = getattr(a, "usage_metrics", None)
        if m:
            usage.add_openai(m)

    return str(getattr(result, "raw", None) or result or "")


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        import crewai  # noqa: F401
    except ImportError as exc:
        fail(f"需要 crewai: {exc}。"
             f"安装：uv sync --project bin/backends/crewai（136 个包，建议远端）",
             kind="backend_missing_dep", retryable=False, backend="crewai")
        return

    if not spec.api_key:
        fail("缺少 API 密钥", kind="auth", retryable=False, backend="crewai")
        return

    usage = Usage()
    text_buf = Buffer("text")

    try:
        text = asyncio.run(drive(spec, usage, text_buf))
    except Exception as exc:
        log(f"[crewai] 异常: {type(exc).__name__}: {exc}")
        fail(f"{type(exc).__name__}: {exc}",
             model=spec.model_id, backend="crewai", duration_s=time.time() - t0)
        return

    if text:
        emit("text", text=text)
    usage.emit()

    log(f"[crewai] {len(text)} 字符")
    ok(text.strip(), usage=usage, model=spec.model_id, backend="crewai",
       turns=1, duration_s=time.time() - t0)


if __name__ == "__main__":
    run_main(main, "crewai")
