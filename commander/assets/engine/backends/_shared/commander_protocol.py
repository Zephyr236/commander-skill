"""后端 runner 的共享协议实现 —— 纯标准库，无任何第三方依赖。

为什么不用 import commander：每个后端是独立的 uv 工程，装 commander 会把
typer/rich 等无关依赖拖进来，而且 crewai/hermes 的 pydantic 钉版各不相同，
共享包会放大冲突面。用 sys.path 共享这个纯标准库文件，既 DRY 又零风险。

线协议（与 src/commander/schemas.py 严格对应）：
    stdin   一行 JSON  = RunSpec
    stdout  JSONL      = Event 流，最后一行必须是 {"type":"result","data":RunResult}
    stderr  自由文本   = 日志，原样落到 agent 的 logs/

用法：
    from commander_protocol import read_spec, emit, ok, fail, Usage, classify
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any

# ══════════════════════════════════════════════════════════════════════════
# 输入
# ══════════════════════════════════════════════════════════════════════════


class Spec(dict):
    """RunSpec 的轻量封装。用 dict 而非 pydantic —— 后端不该被版本绑架。"""

    @property
    def run_id(self) -> str:
        return self.get("run_id", "")

    @property
    def task_id(self) -> str:
        return self.get("task_id", "")

    @property
    def agent_id(self) -> str:
        return self.get("agent_id", "")

    @property
    def prompt(self) -> str:
        return self.get("prompt", "")

    @property
    def system_prompt(self) -> str:
        return self.get("system_prompt", "")

    @property
    def max_tokens(self) -> int:
        return int(self.get("max_tokens") or 8192)

    @property
    def workdir(self) -> str:
        return self.get("workdir", ".")

    @property
    def workspace_root(self) -> str:
        return self.get("workspace_root", "")

    @property
    def timeout(self) -> int:
        return int(self.get("timeout") or 1800)

    @property
    def max_turns(self) -> int:
        return int(self.get("max_turns") or 20)

    @property
    def model(self) -> dict:
        ms = self.get("models") or []
        return ms[0] if ms else {}

    @property
    def model_id(self) -> str:
        return self.model.get("model", "")

    @property
    def api_key(self) -> str:
        return self.model.get("api_key") or ""

    @property
    def base_url(self) -> str:
        return self.model.get("base_url") or ""

    @property
    def anthropic_base_url(self) -> str:
        return self.model.get("anthropic_base_url") or ""

    @property
    def is_reasoning(self) -> bool:
        return bool(self.model.get("is_reasoning"))

    @property
    def skills(self) -> list[dict]:
        return self.get("skills") or []

    @property
    def options(self) -> dict:
        return self.get("options") or {}

    def skill_text(self) -> str:
        """把技能拼成一段可注入的文字。"""
        out = []
        for s in self.skills:
            out.append(f"## 技能：{s.get('name')}\n\n{s.get('body', '')}")
        return "\n\n---\n\n".join(out)


def read_spec() -> Spec:
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("protocol: stdin 为空，期望一行 JSON RunSpec")
    return Spec(json.loads(raw))


def apply_credentials(spec: Spec) -> None:
    """把规格里的凭据注入进程环境。

    为什么走 stdin 而不是命令行环境变量：
        如果父进程写成 `ssh host 'OPENAI_API_KEY=sk-xxx uv run ...'`，
        密钥会出现在**两端的 `ps aux` 输出**里，还会进 shell 历史。
        stdin 不走 argv，是进程间传密钥最省事又不外泄的方式。

    为什么需要它：claude-agent-sdk（驱动 Claude Code CLI）与 hermes 这类
    后端是从**环境变量**读凭据的，光传参给 SDK 不够；而 openai/langchain/
    crewai/autogen 是显式构造 client，本可以不依赖环境变量。
    统一在这里设一次，两类后端都能工作。
    """
    import os

    key = spec.api_key
    if not key:
        return

    base = spec.base_url
    abase = spec.anthropic_base_url

    # 两个 provider 的环境变量都设上 —— 具体用哪套由后端决定，
    # 多余的设置无害（同一个 key 对同一个 provider 的两个端点都有效）。
    os.environ.setdefault("OPENAI_API_KEY", key)
    os.environ.setdefault("ANTHROPIC_API_KEY", key)
    os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", key)
    if base:
        os.environ.setdefault("OPENAI_BASE_URL", base)
        os.environ.setdefault("OPENAI_API_BASE", base)
    if abase:
        os.environ.setdefault("ANTHROPIC_BASE_URL", abase)

    # 别让 langchain 之类的库去连 OpenAI 的 tracing / 遥测端点
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")


# ══════════════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════════════


def emit(type_: str, **data: Any) -> None:
    """发一个事件。立即 flush —— 后端崩了也要保证已产生的事件在盘上。"""
    sys.stdout.write(json.dumps(
        {"type": type_, "ts": time.time(), "data": data}, ensure_ascii=False
    ) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    """写 stderr。会原样落到 agent 的日志里。"""
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.stderr.flush()


class Buffer:
    """流式增量的聚合器。

    为什么需要：SSE 流是逐 token 到达的，如果每个 token 都 emit 一个事件，
    一次几千 token 的回答会往 JSONL 里写几千行 —— 日志体积爆炸、可读性归零。

    按阈值或换行 flush：既能近实时观察，又不会撑爆记录。
    """

    def __init__(self, kind: str = "text", threshold: int = 240) -> None:
        self.kind = kind
        self.threshold = threshold
        self._parts: list[str] = []      # 尚未 flush 的
        self._all: list[str] = []        # 全量（含已 flush 的）
        self._n = 0
        self.total_chars = 0

    def add(self, s: str) -> None:
        if not s:
            return
        self._parts.append(s)
        self._all.append(s)
        self._n += len(s)
        self.total_chars += len(s)
        if self._n >= self.threshold or "\n" in s:
            self.flush()

    def flush(self) -> None:
        if not self._parts:
            return
        chunk = "".join(self._parts)
        self._parts.clear()
        self._n = 0
        emit(self.kind, text=chunk)

    def value(self) -> str:
        """尚未 flush 的部分。"""
        return "".join(self._parts)

    def value_parts(self) -> str:
        """完整累积文本（含已 flush 的）。收尾时用它。"""
        return "".join(self._all)


class Usage:
    """token 累计。各 SDK 字段名不同，统一到这里。"""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0

    def add(self, values: dict | None = None, **kw: int) -> Usage:
        """累加用量。同时接受 dict 位置参数和关键字参数 ——
        各 SDK 回传用量的形状千奇百怪，让调用方少写一层转换。"""
        merged: dict = {}
        if isinstance(values, dict):
            merged.update(values)
        merged.update(kw)
        self.input_tokens += int(merged.get("input_tokens") or 0)
        self.output_tokens += int(merged.get("output_tokens") or 0)
        self.reasoning_tokens += int(merged.get("reasoning_tokens") or 0)
        self.cached_tokens += int(merged.get("cached_tokens") or 0)
        return self

    def add_openai(self, u: Any) -> Usage:
        """从 OpenAI 风格的 usage 对象累加。"""
        if u is None:
            return self
        g = (lambda k: getattr(u, k, None)) if not isinstance(u, dict) else (u.get)
        self.input_tokens += int(g("prompt_tokens") or g("input_tokens") or 0)
        self.output_tokens += int(g("completion_tokens") or g("output_tokens") or 0)
        details = g("completion_tokens_details") or g("output_tokens_details")
        if details:
            d = (lambda k: getattr(details, k, None)) if not isinstance(details, dict) else (details.get)
            self.reasoning_tokens += int(d("reasoning_tokens") or 0)
        pd = g("prompt_tokens_details") or g("input_tokens_details")
        if pd:
            d = (lambda k: getattr(pd, k, None)) if not isinstance(pd, dict) else (pd.get)
            self.cached_tokens += int(d("cached_tokens") or 0)
        return self

    def add_anthropic(self, u: Any) -> Usage:
        """从 Anthropic 风格的 usage 对象累加。"""
        if u is None:
            return self
        g = (lambda k: getattr(u, k, None)) if not isinstance(u, dict) else (u.get)
        self.input_tokens += int(g("input_tokens") or 0)
        self.output_tokens += int(g("output_tokens") or 0)
        self.cached_tokens += int(g("cache_read_input_tokens") or 0)
        return self

    def emit(self) -> None:
        emit("usage", input_tokens=self.input_tokens, output_tokens=self.output_tokens,
             reasoning_tokens=self.reasoning_tokens, cached_tokens=self.cached_tokens)

    @property
    def total(self) -> int:
        """输入 + 输出。指挥官侧的 Usage 也有这个名字，两边保持一致。"""
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens, "cached_tokens": self.cached_tokens,
        }


def ok(
    text: str = "",
    *,
    reasoning: str = "",
    tool_calls: list | None = None,
    usage: Usage | None = None,
    artifacts: list[str] | None = None,
    model: str = "",
    backend: str = "",
    turns: int = 0,
    duration_s: float = 0.0,
    raw: dict | None = None,
) -> None:
    """发最终的成功结果。必须是最后一行。"""
    emit("result", ok=True, text=text, reasoning=reasoning,
         tool_calls=tool_calls or [], usage=usage.as_dict() if usage else {},
         artifacts=artifacts or [], error=None, error_kind=None, retryable=False,
         model=model, backend=backend, turns=turns, duration_s=duration_s,
         raw=raw or {})


def fail(
    error: str,
    *,
    kind: str | None = None,
    retryable: bool | None = None,
    text: str = "",
    usage: Usage | None = None,
    model: str = "",
    backend: str = "",
    duration_s: float = 0.0,
) -> None:
    """发最终的失败结果。同样必须是最后一行。"""
    if kind is None or retryable is None:
        k, r = classify(error)
        kind = kind or k
        retryable = r if retryable is None else retryable
    emit("result", ok=False, text=text, reasoning="", tool_calls=[],
         usage=usage.as_dict() if usage else {}, artifacts=[],
         error=error[:8000], error_kind=kind, retryable=bool(retryable),
         model=model, backend=backend, turns=0, duration_s=duration_s, raw={})


# ══════════════════════════════════════════════════════════════════════════
# 错误分类 —— 决定要不要重试
# ══════════════════════════════════════════════════════════════════════════


def classify(exc: Any) -> tuple[str, bool]:
    """把异常归类成 (error_kind, 是否可重试)。

    参数错误、认证失败绝不重试 —— 那只会重复烧钱。
    """
    s = str(exc).lower()
    if "timeout" in s or "timed out" in s:
        return "timeout", True
    if "429" in s or ("rate" in s and "limit" in s):
        return "rate_limit", True
    if any(c in s for c in ("500", "502", "503", "504", "server error")):
        return "server_error", True
    if any(c in s for c in ("connection", "connect", "unreachable", "refused", "dns")):
        return "connection", True
    if any(c in s for c in ("401", "403", "unauthorized", "forbidden", "invalid api key")):
        return "auth", False
    if any(c in s for c in ("400", "invalid_request", "bad request")):
        return "bad_request", False
    if "context" in s and ("length" in s or "window" in s):
        return "context_overflow", False
    return "unknown", False


# ══════════════════════════════════════════════════════════════════════════
# 入口包装
# ══════════════════════════════════════════════════════════════════════════


def run_main(fn, backend_name: str) -> None:
    """统一的后端入口。

    任何未捕获异常都必须转成合法的 result 事件 —— 否则指挥官只会看到
    「退出码非零但没有结果」，那对诊断毫无帮助。
    """
    t0 = time.time()
    spec = None
    try:
        spec = read_spec()
        # 凭据从 stdin 的规格注入环境 —— 不走命令行，避免 ps/历史泄露
        apply_credentials(spec)
        emit("start", agent_id=spec.agent_id, backend=backend_name,
             model=spec.model_id, workdir=spec.workdir,
             contract=(
                 f"你只在 {spec.workdir} 内工作。"
                 "产物写入该目录，结论通过返回值给出。"
                 "不得写 memory/ 或其他 agent 的目录。"
             ))
        fn(spec)
    except SystemExit:
        raise
    except BaseException as exc:
        log(traceback.format_exc())
        emit("error", message=f"{type(exc).__name__}: {exc}")
        fail(f"{type(exc).__name__}: {exc}",
             model=(spec.model_id if spec else ""),
             backend=backend_name,
             duration_s=time.time() - t0)
        sys.exit(0)   # 协议已给结果，退出码保持 0 避免上层误判
