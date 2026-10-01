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
    from crewai import LLM, Agent, Crew, Process, Task  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入 crewai: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    v = version("crewai")
    pv = version("pydantic")
except Exception:
    v = pv = "?"

print(f"OK crewai {v} (pydantic {pv})，Agent/Task/Crew/LLM 可用")
