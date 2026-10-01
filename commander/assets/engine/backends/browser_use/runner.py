#!/usr/bin/env python3
"""browser-use 后端 —— 让下属能真的打开浏览器去查东西。

包：`browser-use` 0.13.10（requires-python >=3.11）
API：`Agent(task=..., llm=...)` + `await agent.run()` + `history.final_result()`

━━ 落点：看哪台机器有浏览器 ━━
它**不下载浏览器**，而是通过 CDP 连到**已运行的 Chromium**。
所以落点跟依赖体积无关，只看机器上有没有浏览器
（`router._pick_host_for_browser()`）。

浏览器可以来自三处，任选其一：
  ① 系统装的 Chrome / Chromium / Edge / Brave
  ② playwright 缓存的 chromium（`browser up --install-browser` 下）
  ③ 远端有浏览器的机器

它拉进 57 个钉死的依赖，还要开浏览器进程 —— 本地 2 核 / 2Gi 是能跑的量级。

━━ 接非 OpenAI 端点（如 DeepSeek）需要的两个开关 ━━
① `dont_force_structured_output=True`
   browser-use 默认用 response_format 强制结构化输出，兼容端点大多不稳。
② `use_vision=False`
   默认 `use_vision=True`，会走截图 + 视觉模型。文本模型下必须关掉 ——
   关掉之后它靠 DOM 文本工作。这两点是接自定义端点的关键。

━━ 浏览器从哪来 ━━
优先级：options.cdp_url > options.chrome_path > browser-use 自己的默认行为
（默认会先找运行中的 Chrome，找不到就自己拉起一个）。连不上时会给出诊断路径。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))

from commander_protocol import (
    Spec,
    Usage,
    emit,
    fail,
    log,
    ok,
    run_main,
)


def build_llm(spec: Spec, ChatOpenAI):
    """构造 LLM 适配器。

    browser-use 的 ChatOpenAI 自称 "accepts all AsyncOpenAI parameters"，
    实测签名里确实有 `base_url` 与 `api_key` —— 所以能直接接兼容端点。
    """
    kwargs: dict = {
        "model": spec.model_id,
        "api_key": spec.api_key,
        "temperature": spec.options.get("temperature", 0.2),
        "max_completion_tokens": spec.max_tokens,
        # 重试由指挥官的 policy 统一管，别在这里偷偷重试 ——
        # 浏览器任务的重试很贵（每步都是一次 LLM 调用）
        "max_retries": 0,
        "timeout": float(spec.timeout),
    }
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    # ★ 兼容端点必需：否则它会用 response_format 强制结构化输出
    if spec.options.get("force_structured_output", False) is False:
        kwargs["dont_force_structured_output"] = True
    return ChatOpenAI(**kwargs)


def build_profile(spec: Spec, BrowserProfile):
    """按 options 决定连哪个浏览器。什么都不给就用它的默认行为。"""
    o = spec.options
    kw: dict = {}

    if cdp := o.get("cdp_url"):
        kw["cdp_url"] = str(cdp)
    if exe := o.get("chrome_path") or os.environ.get("BH_CHROME_PATH"):
        kw["executable_path"] = str(exe)
    if "headless" in o:
        kw["headless"] = bool(o["headless"])
    if "user_data_dir" in o:
        kw["user_data_dir"] = str(o["user_data_dir"])
    if "keep_alive" in o:
        kw["keep_alive"] = bool(o["keep_alive"])
    if "viewport" in o:
        kw["viewport"] = o["viewport"]

    return BrowserProfile(**kw) if kw else None


async def drive(spec: Spec, usage: Usage) -> tuple[str, dict]:
    from browser_use import Agent, BrowserProfile, ChatOpenAI

    llm = build_llm(spec, ChatOpenAI)
    profile = build_profile(spec, BrowserProfile)

    steps = 0
    last_url = ""

    async def on_step(*args, **kwargs):
        """每一步都发个进度事件。

        browser-use 的「步」很细（点一次、滚一次都算），所以这里把
        step 数映射成事件而已，**不当成完成度** —— 完成度由 agent 自己在
        最终状态块里报（见 budget.status_instruction）。
        """
        nonlocal steps
        steps += 1
        if steps % 5 == 0 or steps <= 3:
            emit("thought", text=f"[browser] 第 {steps} 步")

    agent_kwargs: dict = {
        "task": spec.prompt,
        "llm": llm,
        # ★ 文本模型必须关掉视觉，否则它会去要截图
        "use_vision": bool(spec.options.get("use_vision", False)),
        "max_failures": int(spec.options.get("max_failures", 5)),
        "generate_gif": False,
        # 每步回调 —— 为的是让指挥官看得见进展，也让预算系统有中间信号
        "register_new_step_callback": on_step,
    }
    if profile is not None:
        agent_kwargs["browser_profile"] = profile
    if spec.max_turns:
        # browser-use 的 step 粒度比「轮」细得多，给一个宽松的上限；
        # 真正的刹车是 timeout 和预算，不是这个数
        agent_kwargs["max_steps"] = max(10, spec.max_turns * 5)
    if spec.system_prompt:
        agent_kwargs["extend_system_message"] = spec.system_prompt
    # 把过程存下来，出问题时能回看它到底点了什么
    agent_kwargs["save_conversation_path"] = str(
        Path(spec.workdir) / "browser-conversation.json"
    )
    if spec.options.get("sensitive_data"):
        agent_kwargs["sensitive_data"] = spec.options["sensitive_data"]

    agent = Agent(**agent_kwargs)

    history = await agent.run()

    text = ""
    for attr in ("final_result",):
        fn = getattr(history, attr, None)
        if callable(fn):
            try:
                text = str(fn() or "")
            except Exception:
                pass
            break
    if not text:
        text = str(history)

    # ── 用量：browser-use 把 token 统计挂在 history.usage 上 ──────────────
    # 字段形状不确定（UsageSummary 是嵌套的），所以防御式地多试几个名字。
    # 拿不到就如实返回 0 —— 绝不让预算系统读到编造的数字。
    try:
        u = getattr(history, "usage", None)
        if u is not None:
            def pick(*names):
                for n in names:
                    v = getattr(u, n, None) if not isinstance(u, dict) else u.get(n)
                    if isinstance(v, (int, float)) and v:
                        return int(v)
                return 0
            pin = pick("prompt_tokens", "input_tokens", "new_prompt_tokens")
            pout = pick("completion_tokens", "output_tokens")
            ptot = pick("total_tokens")
            if not pin and not pout and ptot:
                # 只有总数时按 3:1 估（读多写少是浏览器任务的常态）
                pin, pout = int(ptot * 0.75), int(ptot * 0.25)
            usage.add(input_tokens=pin, output_tokens=pout,
                      cached_tokens=pick("prompt_read_cached_tokens",
                                         "cached_tokens"))
            meta_usage = {"input": pin, "output": pout, "raw": str(u)[:300]}
        else:
            meta_usage = {"note": "history.usage 为空"}
    except Exception as exc:
        meta_usage = {"error": f"{type(exc).__name__}: {exc}"}
        log(f"[browser_use] 取用量失败: {exc}")

    meta = {
        "steps": steps,
        "usage": meta_usage,
        "urls": getattr(history, "urls", lambda: [])() if callable(
            getattr(history, "urls", None)) else [],
        "is_successful": (
            history.is_successful() if callable(getattr(history, "is_successful", None))
            else None
        ),
    }
    return text, meta


def main(spec: Spec) -> None:
    t0 = time.time()

    try:
        import browser_use  # noqa: F401
    except ImportError as exc:
        fail(f"需要 browser-use: {exc}。"
             f"安装：./.commander/cmd backend install browser_use",
             kind="backend_missing_dep", retryable=False, backend="browser_use")
        return

    if not spec.api_key:
        fail("缺少 API 密钥", kind="auth", retryable=False,
             backend="browser_use")
        return

    usage = Usage()
    try:
        text, meta = asyncio.run(drive(spec, usage))
    except Exception as exc:
        name = type(exc).__name__
        msg = str(exc)
        log(f"[browser_use] {name}: {msg}")

        # 浏览器连不上是最常见的失败，也最难自己看懂 —— 给可操作的诊断
        hint = ""
        low = (name + msg).lower()
        if any(k in low for k in ("cdp", "connect", "browser", "chrome",
                                  "devtools", "target closed")):
            hint = (
                "\n\n这是浏览器连接问题。排查顺序：\n"
                "  1. 机器上有没有 Chrome：which google-chrome-stable\n"
                "  2. 有没有在跑并开了远程调试："
                "curl -s http://127.0.0.1:9222/json/version\n"
                "  3. 手动起一个带调试端口的（隔离 profile，不动用户的浏览器）：\n"
                "     google-chrome-stable --headless=new "
                "--remote-debugging-port=9222 --user-data-dir=/tmp/bu-profile &\n"
                "  4. 然后把 cdp_url 告诉这个 agent：\n"
                "     options.cdp_url = \"http://127.0.0.1:9222\"\n"
                "  5. 官方诊断（信息最全，先跑它）：\n"
                "     .commander/bin/backends/browser_use/.venv/bin/browser-use doctor\n"
                "\n注意：Ubuntu 上 snap 装的 Chromium 常常连不上（沙箱挡了 DevTools 端口），"
                "要装 .deb 版并设 BH_CHROME_PATH。"
            )
        fail(f"{name}: {msg}{hint}",
             model=spec.model_id, backend="browser_use",
             duration_s=time.time() - t0)
        return

    if text:
        emit("text", text=text)
    usage.emit()
    if not usage.total:
        log("[browser_use] ⚠ 没拿到 token 用量 —— 预算系统对这次派发只能计时间和算力")

    log(f"[browser_use] {meta.get('steps', 0)} 步, {len(text)} 字符")
    ok(text.strip(), usage=usage, model=spec.model_id, backend="browser_use",
       turns=int(meta.get("steps") or 0), duration_s=time.time() - t0,
       raw={"steps": meta.get("steps", 0),
            "is_successful": meta.get("is_successful"),
            "usage_detail": meta.get("usage"),
            "urls": [u for u in (meta.get("urls") or [])
                     if str(u).startswith("http")][:20]})


if __name__ == "__main__":
    run_main(main, "browser_use")
