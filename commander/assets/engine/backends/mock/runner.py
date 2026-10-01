#!/usr/bin/env python3
"""Mock 后端 —— 零成本端到端自测。

用途：不花一分钱验证整条链路
    派发 → workdir 隔离 → 事件流 → 日志落盘 → outbox → 任务流水 → 记忆回写

它做的事和真后端完全一样（走同一套线协议），只是不调用任何模型。
指挥官侧的任何改动都可以先用它验证，再上真模型。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

from commander_protocol import Spec, Usage, emit, log, ok, run_main


def main(spec: Spec) -> None:
    t0 = time.time()
    wd = Path(spec.workdir)
    usage = Usage()

    emit("thought", text=f"（mock）收到 {len(spec.prompt)} 字符的指令")

    # 模拟逐步工作，产生可观察的事件流
    steps = [
        ("解析指令", 0.02),
        ("规划步骤", 0.02),
        ("产出结果", 0.02),
    ]
    for i, (name, delay) in enumerate(steps, 1):
        time.sleep(delay)
        emit("tool_call", name=f"mock.{name}", arguments={"step": i},
             id=f"call_{i}")
        emit("tool_result", result=f"（mock）{name} 完成")
        usage.add(input_tokens=120 * i, output_tokens=45 * i, reasoning_tokens=20 * i)
        log(f"[mock] step {i}: {name}")

    # 真的往 workdir 写一个产物 —— 验证 agent 确实只能在自己的目录里写
    artifact = wd / "mock-output.json"
    artifact.write_text(json.dumps({
        "agent_id": spec.agent_id,
        "task_id": spec.task_id,
        "run_id": spec.run_id,
        "received_prompt_chars": len(spec.prompt),
        "skills": [s.get("name") for s in spec.skills],
        "model": spec.model_id,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("artifact", path=artifact.name, size=artifact.stat().st_size)

    # 故意试一次越界写入，验证 guard 的日志能捕获（不阻断，只记录）
    try:
        (Path(spec.workspace_root) / "memory" / "_mock_probe.md").write_text(
            "mock 越界探测", encoding="utf-8")
        log("[mock] ⚠ 越界写入竟然成功了 —— 契约未生效")
    except OSError as exc:
        log(f"[mock] ✓ 越界写入被拒: {exc}")

    usage.emit()

    text = (
        f"（mock 后端）已完成。\n\n"
        f"- agent: {spec.agent_id}\n"
        f"- 模型: {spec.model_id}\n"
        f"- 技能: {', '.join(s.get('name','') for s in spec.skills) or '(无)'}\n"
        f"- 产物: {artifact.name}\n"
        f"- 工作目录: {wd}\n\n"
        f"指令回显：{spec.prompt[:300]}"
    )
    emit("text", text=text)

    ok(text, usage=usage, artifacts=[artifact.name],
       model=spec.model_id or "mock", backend="mock",
       turns=len(steps), duration_s=time.time() - t0,
       raw={"mock": True})


if __name__ == "__main__":
    run_main(main, "mock")
