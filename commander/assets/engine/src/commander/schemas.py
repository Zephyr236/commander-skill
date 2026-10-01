"""子进程线协议 —— 指挥官与所有 SDK 后端之间唯一的接口。

设计要点：**所有 SDK 的差异必须止步于此**。

指挥官不该知道 CrewAI 的 kickoff 和 LangChain 的 invoke 有什么区别。
后端进程（bin/backends/<sdk>/runner.py）负责把自家 SDK 翻译成这套结构。

线协议：
    父进程 → 子进程 stdin   : 一行 JSON（RunSpec）
    子进程 → 父进程 stdout  : JSONL 事件流，最后一行必须是 result 事件
    子进程 → 父进程 stderr  : 自由文本日志（原样落到 agent 的 logs/）

这样任何新 SDK 只要实现这个契约就能插进来，指挥官侧零改动。
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════════════════════════
# 父 → 子：派发规格
# ══════════════════════════════════════════════════════════════════════════


class ModelSpec(BaseModel):
    """本次派发用哪个模型、怎么连。后端据此构造自己的 client。"""

    alias: str                      # models.toml 里的键，如 "flash"
    model: str                      # 真实模型 id，如 "deepseek-flash"
    provider_kind: str = "openai_compatible"
    base_url: str | None = None             # OpenAI 兼容端点
    anthropic_base_url: str | None = None   # Anthropic 兼容端点
    api_key: str | None = None              # 由父进程从 .env 注入，子进程不读 .env
    is_reasoning: bool = False
    supports_tools: bool = True
    max_tokens_floor: int = 4096
    effort: str | None = None


class SkillSpec(BaseModel):
    """一个赋予本次派发的技能。内容已由父进程读好并剥掉 frontmatter。"""

    name: str
    description: str = ""
    body: str = ""
    path: str = ""


class RunSpec(BaseModel):
    """一次派发的完整规格。父进程发给后端进程的就是这个。"""

    run_id: str
    task_id: str
    agent_id: str

    # 指令
    prompt: str
    system_prompt: str = ""
    skills: list[SkillSpec] = Field(default_factory=list)

    # 模型
    models: list[ModelSpec] = Field(default_factory=list)   # 主 + 可选降级
    max_tokens: int = 8192
    temperature: float | None = None

    # 执行环境
    workdir: str                    # 钉死的 cwd，agent 只能在这里写
    workspace_root: str
    timeout: int = 1800
    max_turns: int = 20

    # 后端专属参数（各 SDK 自己去解释，指挥官不关心）
    options: dict[str, Any] = Field(default_factory=dict)

    def wire(self) -> str:
        return self.model_dump_json(exclude_none=True)


# ══════════════════════════════════════════════════════════════════════════
# 子 → 父：事件流与结果
# ══════════════════════════════════════════════════════════════════════════

EventType = Literal[
    "start",        # 后端已就绪，开始干活
    "thought",      # 推理过程（reasoning_content / thinking）
    "text",         # 正文增量
    "tool_call",    # 调用工具
    "tool_result",  # 工具返回
    "artifact",     # 产出了一个文件
    "usage",        # token 用量
    "error",        # 可恢复的错误
    "result",       # 最终结果（必须是最后一行）
    "log",          # 自由日志
]


class Event(BaseModel):
    """JSONL 流里的一行。留痕就是把这些原样存下来。"""

    type: EventType
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)

    def wire(self) -> str:
        return self.model_dump_json(exclude_none=True)


class ToolCall(BaseModel):
    name: str
    arguments: Any = None
    id: str | None = None
    result: Any = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class RunResult(BaseModel):
    """归一化后的结果。指挥官只读这个，不读任何 SDK 的原始对象。"""

    ok: bool
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    artifacts: list[str] = Field(default_factory=list)   # 相对 workdir 的产物路径
    error: str | None = None
    error_kind: str | None = None    # timeout | rate_limit | server_error | connection | unknown
    retryable: bool = False
    model: str = ""
    backend: str = ""
    turns: int = 0
    duration_s: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)

    def brief(self, limit: int = 400) -> str:
        """给指挥官看的一行摘要。"""
        if not self.ok:
            return f"✗ {self.error_kind or 'error'}: {(self.error or '')[:limit]}"
        head = self.text.strip().replace("\n", " ")
        if len(head) > limit:
            head = head[:limit] + "…"
        return f"✓ [{self.model} {self.backend} {self.duration_s:.1f}s] {head}"


# ══════════════════════════════════════════════════════════════════════════
# 解析辅助
# ══════════════════════════════════════════════════════════════════════════

RETRYABLE_KINDS = {"timeout", "rate_limit", "server_error", "connection"}


def classify_error(exc: BaseException | str) -> tuple[str, bool]:
    """把任意异常归类成 error_kind + 是否可重试。

    用于决定要不要重试 —— 但绝不能重试参数错误，那只会重复烧钱。
    """
    s = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in s or "timed out" in s:
        return "timeout", True
    if ("rate" in s and "limit" in s) or "429" in s:
        return "rate_limit", True
    if any(c in s for c in ("500", "502", "503", "504", "server error")):
        return "server_error", True
    if any(c in s for c in ("connection", "connect", "unreachable", "refused", "dns")):
        return "connection", True
    if any(c in s for c in ("401", "403", "unauthorized", "forbidden", "invalid api key")):
        return "auth", False
    if any(c in s for c in ("400", "invalid_request", "validation", "bad request")):
        return "bad_request", False
    if "context" in s and ("length" in s or "window" in s):
        return "context_overflow", False
    if "budget" in s or "cost" in s:
        return "budget", False
    return "unknown", False


def parse_result_line(line: str) -> RunResult | None:
    """从后端 stdout 的一行里解析出最终结果。"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "result":
        return None
    payload = obj.get("data") or obj.get("result") or {}
    try:
        return RunResult.model_validate(payload)
    except Exception:
        return None
