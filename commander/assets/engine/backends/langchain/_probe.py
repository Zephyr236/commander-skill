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
    from langchain.agents import create_agent  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入 langchain: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    lc = version("langchain")
    lg = version("langgraph")
except Exception:
    lc = lg = "?"

print(f"OK langchain {lc} / langgraph {lg}，create_agent 可用")
