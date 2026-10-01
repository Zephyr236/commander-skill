"""配置加载 —— models.toml / agents.toml / policy.toml 与 .env。

密钥只从环境变量取（由 .env 注入），配置文件中永远只有变量名。
"""

from __future__ import annotations

import os
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .guard import GuardPolicy
from .workspace import Workspace


class ConfigError(RuntimeError):
    pass


# 模板里常见的占位符形态。命中即视为"没填"。
_PLACEHOLDER_MARKERS = (
    "xxxx", "your_", "your-", "changeme", "placeholder",
    "todo", "fill_me", "<", ">", "api_key_here", "...",
)


def _is_placeholder(value: str) -> bool:
    """判断读到的值是不是模板占位符而非真实密钥。

    宁可误判成"没填"也不要误判成"填了" —— 前者让人去填，
    后者让人以为能用、然后在真正调用时收到难懂的 401。
    """
    v = value.strip().strip('"').strip("'")
    if len(v) < 20:
        return True                      # 真实密钥不会这么短
    low = v.lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


# ══════════════════════════════════════════════════════════════════════════
# models.toml
# ══════════════════════════════════════════════════════════════════════════


class ProviderConfig(BaseModel):
    name: str = ""
    kind: str = "openai_compatible"
    base_url: str | None = None
    anthropic_base_url: str | None = None
    api_key_env: str | None = None
    enabled: bool = True
    verified: str | None = None
    notes: str = ""

    def api_key(self) -> str | None:
        """从环境变量取密钥。绝不从文件读 —— 文件会进版本库。

        会过滤掉占位符。实测踩过：`.env.example` 里若写
        `DEEPSEEK_API_KEY=sk-xxxxxxxx`，`load_dotenv` 会把它当真实值加载，
        于是 doctor 报"密钥已设置"（假阳性），而真派发时拿着 `sk-xxxx`
        去请求，收到一个完全看不出原因的 401。
        """
        if not self.api_key_env:
            return None
        raw = (os.environ.get(self.api_key_env) or "").strip()
        if not raw or _is_placeholder(raw):
            return None
        return raw

    # ── base_url：允许用 .env 覆盖 models.toml ────────────────────────
    # 这样用户说一句「base url 换成 X」就能生效，不用改 TOML
    #（改 TOML 需要程序化重写，容易把注释和格式搞乱）。
    def effective_base_url(self) -> str | None:
        from .credentials import base_url_env
        return os.environ.get(base_url_env(self.name)) or self.base_url

    def effective_anthropic_base_url(self) -> str | None:
        from .credentials import anthropic_base_url_env
        return (os.environ.get(anthropic_base_url_env(self.name))
                or self.anthropic_base_url)


class ModelConfig(BaseModel):
    alias: str = ""
    provider: str
    model: str
    display: str = ""
    context: int = 128_000
    max_output: int = 8192
    cost_tier: str = "standard"
    supports_tools: bool = True
    supports_vision: bool = False
    is_reasoning: bool = False
    max_tokens_floor: int = 4096
    status: str = "active"
    effort_levels: list[str] = Field(default_factory=list)
    # 可选真实单价（美元 / 1M tokens）：{"input":..,"output":..,"cached_input":..}
    # 不填则用 cost_tier 折算成"成本单位"，决策照常进行
    pricing: dict = Field(default_factory=dict)
    notes: str = ""


# ══════════════════════════════════════════════════════════════════════════
# agents.toml
# ══════════════════════════════════════════════════════════════════════════

BACKENDS = ("claude", "openai", "langchain", "crewai", "autogen", "hermes",
            "browser_use", "openai_compat", "mock")

# ══════════════════════════════════════════════════════════════════════════
# 后端的两个**独立**维度 —— 不要把它们当成互斥的分类
#
#   ① 安装分组（HEAVY / LIGHT）：决定「依赖装在哪」—— 看体积
#   ② 落点标记（NEEDS_BROWSER）：  决定「跑在哪」  —— 看有没有浏览器
#
# 一个后端可以同时是 LIGHT 和 NEEDS_BROWSER（browser_use 就是）：
# 226M 本地装得下，但**没有浏览器就是跑不了**。
#
# ⚠️ 我一开始把这三个写成了互斥的集合，结果 browser_use 两边都在、
#    启动断言直接报「分类重叠」。那个断言是对的 —— 错的是模型。
# ══════════════════════════════════════════════════════════════════════════

# ① 安装分组：重依赖装不下 → 落远端（体积判据）
HEAVY_BACKENDS = {"crewai", "autogen", "hermes", "langchain"}

# ① 安装分组：轻量，装本地
LIGHT_BACKENDS = {"claude", "openai", "openai_compat", "mock", "browser_use"}

# ② 落点标记：需要宿主机上有浏览器。**可以与前两者重叠。**
#    这类后端的落点由「哪台机器有浏览器」决定，跟依赖体积无关。
NEEDS_BROWSER = {"browser_use"}
# 哪些后端可以在本地轻量运行
LIGHT_BACKENDS = {"claude", "openai", "openai_compat", "mock", "browser_use"}


def assert_backends_classified() -> None:
    """校验后端的两个维度都完整、且互不矛盾。

    ⚠️ 实测踩过两次同类问题：
      · 加了 browser_use 却忘了 BACKEND_DIRS，`backend list` 看不到它
      · 加了 browser_use 却忘了 LIGHT_BACKENDS，安装分组出现空洞
    清单散在不同文件里，靠人记必然漂移。这里在导入时断言，
    让它**启动就炸**而不是派发时才炸。
    """
    known = set(BACKENDS)

    # ① 安装分组必须是 BACKENDS 的**划分**（不重不漏）
    overlap = HEAVY_BACKENDS & LIGHT_BACKENDS
    if overlap:
        raise ValueError(
            f"安装分组重叠：{sorted(overlap)} 同时标成重依赖和轻量 —— "
            f"二选一。"
        )
    covered = HEAVY_BACKENDS | LIGHT_BACKENDS
    if covered != known:
        raise ValueError(
            f"安装分组不完整。未分组: {sorted(known - covered)}；"
            f"分组里有多余的: {sorted(covered - known)}。"
            f"新增后端时 HEAVY_BACKENDS / LIGHT_BACKENDS 要同步。"
        )

    # ② 落点标记可以重叠，但引用的后端必须存在
    if stale := NEEDS_BROWSER - known:
        raise ValueError(
            f"NEEDS_BROWSER 引用了不存在的后端: {sorted(stale)}"
        )


assert_backends_classified()


class AgentConfig(BaseModel):
    id: str = ""
    backend: str
    model: str
    target: str = "auto"                 # local | remote | auto
    skills: list[str] = Field(default_factory=list)
    max_tokens: int = 8192
    timeout: int = 1800
    max_turns: int = 20
    description: str = ""
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    # 这个 agent 的四维限额，覆盖 policy 里的 [budget.agent]
    budget: dict = Field(default_factory=dict)

    def wants_remote(self, host_available: bool) -> bool:
        """决定这次派发落本地还是远端。

        auto 的语义：重后端优先远端（本地 2Gi 装不下），轻后端留本地。
        没有可用远端时一律降级本地，绝不因为远端不可达而拒绝服务。
        """
        if self.target == "remote":
            return host_available
        if self.target == "local":
            return False
        # auto
        if self.backend in HEAVY_BACKENDS:
            return host_available
        return False


# ══════════════════════════════════════════════════════════════════════════
# policy.toml
# ══════════════════════════════════════════════════════════════════════════


class ConcurrencyPolicy(BaseModel):
    local: int = 2
    remote: int = 8


class BudgetPolicy(BaseModel):
    """三层预算 + 阈值 + 成本权重。

    三层（global / task / agent）各是一组四维限额，见 budget.Limits。
    这里保持成 dict，避免 config 依赖 budget 模块（它们是同层的）。
    """

    max_tokens_per_run: int = 200_000
    max_tokens_per_task: int = 1_000_000
    default_timeout: int = 1800

    # 四维限额，{"tokens":..., "cost_units":..., "wall_seconds":..., ...}
    # 注意 TOML 里写的是 [budget.global]，但 global 是 Python 关键字，
    # 所以在字段名上加了尾下划线。
    global_: dict = Field(default_factory=dict, alias="global")
    task: dict = Field(default_factory=dict)
    agent: dict = Field(default_factory=dict)

    thresholds: dict = Field(default_factory=dict)
    cost_weights: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class RetryPolicy(BaseModel):
    max_attempts: int = 2
    backoff_base: int = 5
    retry_on: list[str] = Field(
        default_factory=lambda: ["timeout", "rate_limit", "server_error", "connection"]
    )


class SelectionPolicy(BaseModel):
    prefer_cost_tier: str = "cheap"
    avoid_status: list[str] = Field(default_factory=lambda: ["maintenance"])
    default_parallel_angles: int = 3


class Policy(BaseModel):
    concurrency: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    selection: SelectionPolicy = Field(default_factory=SelectionPolicy)
    guard: GuardPolicy = Field(default_factory=GuardPolicy)
    approval: dict[str, Any] = Field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════
# 远端主机
# ══════════════════════════════════════════════════════════════════════════


class HostConfig(BaseModel):
    name: str = ""
    host: str
    user: str = "root"
    port: int = 22
    identity_file: str | None = None
    password_env: str | None = None
    workdir: str = "/opt/commander"
    capabilities: list[str] = Field(default_factory=list)
    allowed_backends: list[str] = Field(default_factory=list)
    max_parallel: int = 4

    def password(self) -> str | None:
        if not self.password_env:
            return None
        return os.environ.get(self.password_env) or None

    def identity_path(self, ws: Workspace) -> Path | None:
        if not self.identity_file:
            return None
        p = Path(self.identity_file)
        if not p.is_absolute():
            p = ws.root / p
        return p if p.is_file() else None

    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}"


# ══════════════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════════════


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


class Config:
    """工作区全部配置的加载入口。懒加载 + 缓存。"""

    def __init__(self, ws: Workspace | None = None) -> None:
        self.ws = ws or Workspace()
        self._load_env()

    def _load_env(self) -> None:
        """把 bin/.env 注入环境变量。

        用 override=False —— 真实环境变量优先级高于文件，
        这样 CI / 临时覆盖才能生效。
        """
        env_file = self.ws.dotenv
        if env_file.is_file():
            load_dotenv(env_file, override=False)

    @cached_property
    def _models_raw(self) -> dict[str, Any]:
        return _load_toml(self.ws.models_toml)

    @cached_property
    def _agents_raw(self) -> dict[str, Any]:
        return _load_toml(self.ws.agents_toml)

    @cached_property
    def _policy_raw(self) -> dict[str, Any]:
        return _load_toml(self.ws.policy_toml)

    @cached_property
    def _hosts_raw(self) -> dict[str, Any]:
        if not self.ws.hosts_toml.is_file():
            return {"hosts": {}}
        return _load_toml(self.ws.hosts_toml)

    # ── provider / model ──────────────────────────────────────────────
    @cached_property
    def providers(self) -> dict[str, ProviderConfig]:
        out: dict[str, ProviderConfig] = {}
        for name, data in (self._models_raw.get("providers") or {}).items():
            out[name] = ProviderConfig(name=name, **data)
        return out

    @cached_property
    def models(self) -> dict[str, ModelConfig]:
        out: dict[str, ModelConfig] = {}
        for alias, data in (self._models_raw.get("models") or {}).items():
            out[alias] = ModelConfig(alias=alias, **data)
        return out

    def model(self, alias: str) -> ModelConfig:
        if alias not in self.models:
            raise ConfigError(
                f"未知模型别名 {alias!r}。可用: {sorted(self.models)}"
            )
        return self.models[alias]

    def provider_for(self, m: ModelConfig) -> ProviderConfig:
        if m.provider not in self.providers:
            raise ConfigError(
                f"模型 {m.alias!r} 引用了未注册的 provider {m.provider!r}。"
                f"已注册: {sorted(self.providers)}"
            )
        return self.providers[m.provider]

    def available_models(self) -> list[ModelConfig]:
        """只列出真有密钥、且未停用的模型。"""
        out = []
        for m in self.models.values():
            if m.status == "disabled":
                continue
            if m.provider == "local":       # mock 之类不需要密钥
                out.append(m)
                continue
            p = self.providers.get(m.provider)
            if p and p.enabled and p.api_key():
                out.append(m)
        return out

    # ── agent ─────────────────────────────────────────────────────────
    @cached_property
    def agents(self) -> dict[str, AgentConfig]:
        out: dict[str, AgentConfig] = {}
        for aid, data in (self._agents_raw.get("agents") or {}).items():
            out[aid] = AgentConfig(id=aid, **data)
        return out

    def agent(self, agent_id: str) -> AgentConfig:
        if agent_id not in self.agents:
            raise ConfigError(
                f"未编制的 agent {agent_id!r}。已编制: {sorted(self.agents)}"
            )
        return self.agents[agent_id]

    # ── policy ────────────────────────────────────────────────────────
    @cached_property
    def policy(self) -> Policy:
        raw = dict(self._policy_raw)
        return Policy(
            concurrency=ConcurrencyPolicy(**raw.get("concurrency", {})),
            budget=BudgetPolicy(**raw.get("budget", {})),
            retry=RetryPolicy(**raw.get("retry", {})),
            selection=SelectionPolicy(**raw.get("selection", {})),
            guard=GuardPolicy.from_toml(raw),
            approval=raw.get("approval", {}),
        )

    # ── hosts ─────────────────────────────────────────────────────────
    @cached_property
    def hosts(self) -> dict[str, HostConfig]:
        out: dict[str, HostConfig] = {}
        for name, data in (self._hosts_raw.get("hosts") or {}).items():
            out[name] = HostConfig(name=name, **data)
        return out

    def default_host(self) -> HostConfig | None:
        """挑一个可用远端。没有就返回 None，调用方降级本地。"""
        for h in self.hosts.values():
            return h
        return None

    # ── 自检 ──────────────────────────────────────────────────────────
    def doctor(self) -> list[tuple[str, str, str]]:
        """返回 (检查项, 状态, 说明)。给 commander doctor 用。"""
        rows: list[tuple[str, str, str]] = []

        for name, p in self.providers.items():
            if not p.enabled:
                rows.append((f"provider:{name}", "off", "已停用"))
                continue
            if p.api_key():
                rows.append((f"provider:{name}", "ok", f"密钥已设置 ({p.api_key_env})"))
            else:
                rows.append((f"provider:{name}", "warn",
                             f"缺少环境变量 {p.api_key_env}"))

        for h in self.hosts.values():
            ident = h.identity_path(self.ws)
            if ident:
                rows.append((f"host:{h.name}", "ok", f"密钥认证 {ident.name}"))
            elif h.password():
                rows.append((f"host:{h.name}", "warn",
                             "仅密码认证（建议跑 remote bootstrap 换密钥）"))
            else:
                rows.append((f"host:{h.name}", "warn", "无可用凭据"))

        return rows
