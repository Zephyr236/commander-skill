"""后端就绪探测。"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

try:
    import commander_protocol  # noqa: F401
except Exception as exc:
    print(f"FAIL 无法导入共享协议: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    v = version("hermes-agent")
except Exception:
    v = "未安装"

cli = shutil.which("hermes") or "未找到"

acp = "可导入"
try:
    import hermes_acp_sdk  # noqa: F401
except Exception as exc:
    acp = f"不可导入 ({type(exc).__name__})"

print(f"OK hermes-agent {v} | CLI: {cli} | hermes-acp-sdk: {acp}")
