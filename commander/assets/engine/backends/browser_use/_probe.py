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
    import browser_use
    names = [n for n in ("Agent", "BrowserProfile", "ChatOpenAI", "Browser")
             if hasattr(browser_use, n)]
except Exception as exc:
    print(f"FAIL 无法导入 browser_use: {exc}")
    raise SystemExit(1) from None

try:
    from importlib.metadata import version
    v = version("browser-use")
except Exception:
    v = "?"

import shutil

chrome = next((shutil.which(b) for b in
               ("google-chrome-stable", "google-chrome", "chromium",
                "chromium-browser") if shutil.which(b)), None)

print(f"OK browser-use {v} | 符号: {', '.join(names)}")
print(f"   浏览器: {chrome or '✗ 未找到 —— 这个后端需要 Chrome 才能跑'}")
