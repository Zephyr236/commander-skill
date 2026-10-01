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
    from autogen_agentchat.agents import AssistantAgent  # noqa: F401
    from autogen_ext.models.openai import OpenAIChatCompletionClient  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入 autogen: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    v = version("autogen-agentchat")
except Exception:
    v = "?"

print(f"OK autogen-agentchat {v}（注意：上游已进入维护模式）")
