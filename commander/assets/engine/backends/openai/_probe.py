"""后端就绪探测。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

try:
    import commander_protocol  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入共享协议: {exc}")
    raise SystemExit(1) from None

try:
    import agents
    names = [n for n in ("Agent", "Runner", "OpenAIChatCompletionsModel",
                         "set_tracing_disabled", "set_default_openai_api")
             if hasattr(agents, n)]
except Exception as exc:
    print(f"FAIL 无法导入 agents: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    v = version("openai-agents")
except Exception:
    v = "?"

print(f"OK openai-agents {v} 可用符号: {', '.join(names)}")
