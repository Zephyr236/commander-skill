"""后端就绪探测 —— 由 `commander backend probe <name>` 调用。

成功打印 OK 并返回 0；失败打印原因并返回非 0。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

try:
    import commander_protocol  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入共享协议: {exc}")
    raise SystemExit(1) from None

print("OK mock 后端无需第三方依赖")
