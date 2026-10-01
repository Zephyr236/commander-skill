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
    import claude_agent_sdk
    names = [n for n in ("query", "ClaudeSDKClient", "ClaudeAgentOptions")
             if hasattr(claude_agent_sdk, n)]
except Exception as exc:
    print(f"FAIL 无法导入 claude_agent_sdk: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    v = version("claude-agent-sdk")
except Exception:
    v = "?"

print(f"OK claude-agent-sdk {v} 可用符号: {', '.join(names)}")
