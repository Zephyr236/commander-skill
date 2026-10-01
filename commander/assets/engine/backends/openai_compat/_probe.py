"""后端就绪探测。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

try:
    import httpx
except Exception as exc:
    print(f"FAIL 无法导入 httpx: {exc}")
    raise SystemExit(1) from None

try:
    import commander_protocol  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入共享协议: {exc}")
    raise SystemExit(1) from None

print(f"OK httpx {httpx.__version__}")
