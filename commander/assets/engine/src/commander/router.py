"""路由 —— 决定「谁去干、用什么模型、在哪台机器上干」。

用户需求原文：「指挥官就可以按需定制完成任务的agent」
「不仅仅是sdk不同，api也不同」

指挥官不该每次都手工指定模型。这里把选择逻辑收敛成一处，可解释、可覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import AgentConfig, Config, ModelConfig
from .schemas import ModelSpec


class RoutingError(RuntimeError):
    pass


@dataclass
class Route:
    """一次派发的路由决策。带理由，方便指挥官向用户解释为什么这么选。"""

    agent: AgentConfig
    model: ModelConfig
    remote: bool
    host_name: str | None = None
    reasons: list[str] = field(default_factory=list)

    def explain(self) -> str:
        where = f"远端 {self.host_name}" if self.remote else "本地"
        return (
            f"{self.agent.id} → {self.model.alias}({self.model.model}) @ {where}"
            f"  [{' / '.join(self.reasons)}]"
        )


class Router:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ── 入口 ──────────────────────────────────────────────────────────
    def route(
        self,
        agent_id: str,
        *,
        model_alias: str | None = None,
        target: str | None = None,
        host_name: str | None = None,
    ) -> Route:
        agent = self.cfg.agent(agent_id)
        if not agent.enabled:
            raise RoutingError(f"agent {agent_id!r} 已被停用（enabled=false）")

        reasons: list[str] = []

        # ① 模型
        if model_alias:
            model = self.cfg.model(model_alias)
            reasons.append(f"模型由调用方指定={model_alias}")
        else:
            model = self._pick_model(agent, reasons)

        # ② 落点
        host = self._pick_host(agent, target, host_name, reasons)
        remote = host is not None

        # ③ 后端可用性校验
        self._validate_backend(agent, remote, reasons)

        return Route(agent=agent, model=model, remote=remote,
                     host_name=host.name if host else None, reasons=reasons)

    # ── 模型选择 ──────────────────────────────────────────────────────
    # 哪些后端能跑哪些模型。
    # ⚠️ 实测踩过的坑：早期版本降级时只换模型、不动后端，于是出现
    #    「claude 后端 + mock 模型」这种无意义组合 —— Claude Agent SDK 拿着
    #    model=mock 去请求 Anthropic 端点，必然失败，而报错完全看不出原因。
    #    模型与后端必须相容才有意义。
    _LOCAL_PROVIDERS = frozenset({"local"})   # mock 这类不联网的本地模型

    def _compatible(self, model: ModelConfig, backend: str) -> bool:
        """这个模型能不能配这个后端跑。"""
        if backend == "mock":
            return True                       # mock 后端不真的调模型，配什么都行
        # 真后端不能跑 mock 模型
        return model.provider not in self._LOCAL_PROVIDERS

    def _pick_model(self, agent: AgentConfig, reasons: list[str]) -> ModelConfig:
        try:
            wanted = self.cfg.model(agent.model)
        except Exception as exc:
            raise RoutingError(f"agent {agent.id!r} 配置的模型不可用: {exc}") from exc

        if (wanted.status != "disabled" and self._has_key(wanted)
                and self._compatible(wanted, agent.backend)):
            reasons.append(f"用编制表指定的 {wanted.alias}")
            return wanted

        # 编制表里的模型不可用 → 在**与后端相容**的模型里降级
        avail = self._fallback_order(wanted, agent.backend)
        if avail:
            reasons.append(
                f"编制模型 {wanted.alias} 不可用（{wanted.status}/无密钥），"
                f"降级到 {avail[0].alias}"
            )
            return avail[0]

        # 降级也降不了 —— 必须明确失败，不能硬凑一个跑不通的组合
        raise RoutingError(self._no_model_help(agent, wanted))

    def _no_model_help(self, agent: AgentConfig, wanted: ModelConfig) -> str:
        """没有可用模型时，给一条能照着做的错误信息。

        「降级到 mock」看着像"还能跑"，实际是拿 mock 模型去调真 SDK，
        报错完全指不到病根。不如直接说清楚缺什么、怎么补。
        """
        cfg = self.cfg
        missing = []
        for m in cfg.models.values():
            if m.provider in self._LOCAL_PROVIDERS or m.status == "disabled":
                continue
            p = cfg.providers.get(m.provider)
            if p and p.enabled and not p.api_key():
                missing.append(f"{p.api_key_env}（provider {m.provider}）")

        head = (
            f"agent {agent.id!r} 需要模型 {wanted.alias}（{wanted.provider}），"
            f"但它不可用，且没有与后端 {agent.backend!r} 相容的替代模型。"
        )
        lines = [head, ""]
        if missing:
            lines.append(
                f"缺这些密钥（在 {cfg.ws.display_path(cfg.ws.dotenv)} 里填）："
            )
            lines += [f"  · {x}" for x in dict.fromkeys(missing)]
            lines.append("")
        lines += [
            "或者改用不需要密钥的后端自测：",
            "  ./.commander/cmd dispatch smoke -p '自测'",
        ]
        return "\n".join(lines)

    def _fallback_order(self, wanted: ModelConfig, backend: str) -> list[ModelConfig]:
        """降级顺序：同成本档 → 更便宜 → 其余可用的。

        两个硬过滤：
          · 排除与后端不相容的模型（见 _compatible）
          · 排除 policy 里标记为 avoid_status 的（如维护中的 AutoGen）
        """
        sel = self.cfg.policy.selection
        avail = [
            m for m in self.cfg.available_models()
            if m.status not in sel.avoid_status
            and m.alias != wanted.alias
            and self._compatible(m, backend)
        ]
        tiers = ["cheap", "standard", "premium"]
        try:
            idx = tiers.index(wanted.cost_tier)
        except ValueError:
            idx = 1
        rank = {t: abs(i - idx) for i, t in enumerate(tiers)}
        avail.sort(key=lambda m: (rank.get(m.cost_tier, 9), m.alias))
        return avail

    def _has_key(self, m: ModelConfig) -> bool:
        if m.provider == "local":
            return True
        p = self.cfg.providers.get(m.provider)
        if p is None:
            return False
        return bool(p.api_key())

    # ── 落点选择 ──────────────────────────────────────────────────────
    def _pick_host(
        self,
        agent: AgentConfig,
        target: str | None,
        host_name: str | None,
        reasons: list[str],
    ):
        from .config import NEEDS_BROWSER

        # 需要浏览器的后端（browser_use）：落点由「哪台机器有 Chrome」决定，
        # 而不是依赖体积。没有 Chrome 就是跑不了，装再多依赖也没用。
        if agent.backend in NEEDS_BROWSER and target != "local":
            return self._pick_host_for_browser(agent, target, host_name, reasons)

        if target == "local":
            reasons.append("调用方强制本地")
            return None

        host = self.cfg.hosts.get(host_name) if host_name else self.cfg.default_host()

        if target == "remote" and host is None:
            raise RoutingError(
                f"agent {agent.id!r} 要求远端执行，但没有配置任何可用主机。"
                f"请检查 remote/hosts.toml。"
            )

        if host is None:
            reasons.append("无可用远端，本地执行")
            return None

        if agent.wants_remote(host_available=True):
            if host.allowed_backends and agent.backend not in host.allowed_backends:
                # 远端不放行这个后端。
                #
                # ⚠️ 实测踩过：这里原本是无条件「改本地」，结果 agent 明确写了
                #    target="remote" 的 browser_use 被静默丢到本地跑 —— 而本地
                #    根本没有 Chrome，报出来的是 FileNotFoundError，
                #    完全看不出真实原因是"远端没放行"。
                #
                # 分两种情况：
                #   target="remote" 是**显式意图** → 冲突必须报错，不能替用户决定
                #   target="auto"   是**偏好**     → 降级本地可以，但理由要说清楚
                if agent.target == "remote":
                    raise RoutingError(
                        f"agent {agent.id!r} 指定 target='remote'，但远端 "
                        f"{host.name} 的 allowed_backends 里没有 {agent.backend!r}。\n"
                        f"  远端放行的: {', '.join(host.allowed_backends)}\n"
                        f"  二选一：把 {agent.backend} 加进 remote/hosts.toml 的 "
                        f"allowed_backends，或把该 agent 的 target 改成 local/auto。"
                    )
                reasons.append(
                    f"远端 {host.name} 未放行后端 {agent.backend}，按 auto 降级本地"
                )
                return None
            reasons.append(f"后端 {agent.backend} 属重依赖 → 远端 {host.name}")
            return host

        reasons.append(f"后端 {agent.backend} 轻量 → 本地")
        return None

    def _pick_host_for_browser(self, agent, target, host_name, reasons):
        """给需要 Chrome 的后端选落点。"""
        from .backends import _find_chrome

        host = self.cfg.hosts.get(host_name) if host_name else self.cfg.default_host()
        local_chrome = bool(_find_chrome())

        remote_chrome = None
        if host is not None and host.allowed_backends and \
                agent.backend not in host.allowed_backends:
            reasons.append(f"远端 {host.name} 未放行 {agent.backend}")
            host = None

        if host is not None:
            from .backends import _remote_has_chrome
            from .guard import Guard
            from .ssh_runner import RemoteRunner
            try:
                rr = RemoteRunner(self.cfg, Guard(self.cfg.ws, self.cfg.policy.guard))
                remote_chrome = _remote_has_chrome(self.cfg.ws, host, rr)
            except Exception:
                remote_chrome = None

        # 本地有浏览器 → 本地最省事（不用跨机器传数据）
        if local_chrome:
            reasons.append("本机有 Chrome → 本地执行")
            return None
        # 本地没有、远端有 → 远端
        if remote_chrome:
            reasons.append(f"本机无 Chrome，远端 {host.name} 有 → 远端执行")
            return host
        # 都没有 → 明确报错，别让它跑到一半才炸
        where = f"本机和远端 {host.name} " if host else "本机"
        raise RoutingError(
            f"agent {agent.id!r} 用 {agent.backend}，但{where}都没有 Chrome。\n"
            f"  browser_use 不下载浏览器，它连一个**已运行的 Chrome**。\n"
            f"  二选一：\n"
            f"    · 本机装 Chrome 后跑 `./.commander/cmd browser up`\n"
            f"    · 或在有 Chrome 的机器上 `remote bootstrap` + `remote sync {agent.backend}`\n"
            f"  装完用 `./.commander/cmd backend list` 确认（它会检查 Chrome，"
            f"不只看依赖）。"
        )

    # ── 后端校验 ──────────────────────────────────────────────────────
    def _validate_backend(self, agent: AgentConfig, remote: bool, reasons: list[str]) -> None:
        from .config import HEAVY_BACKENDS
        if agent.backend in HEAVY_BACKENDS and not remote:
            reasons.append(
                f"⚠ 重后端 {agent.backend} 在本地跑（本地可能内存不足）"
            )

    # ── ModelSpec 构造 ────────────────────────────────────────────────
    def to_spec(self, model: ModelConfig) -> ModelSpec:
        """把模型配置转成能穿过进程边界的规格。

        密钥在这里注入一次 —— 子进程收到的是最终值，不去读 .env，
        避免每个后端各自实现一遍配置加载。
        """
        p = self.cfg.providers.get(model.provider)
        key = p.api_key() if p else None
        return ModelSpec(
            alias=model.alias,
            model=model.model,
            provider_kind=p.kind if p else "openai_compatible",
            # 用 effective_* 而不是原始字段 —— 后者允许 .env 覆盖，
            # 用户说一句「base url 换成 X」就能生效
            base_url=p.effective_base_url() if p else None,
            anthropic_base_url=p.effective_anthropic_base_url() if p else None,
            api_key=key,
            is_reasoning=model.is_reasoning,
            supports_tools=model.supports_tools,
            max_tokens_floor=model.max_tokens_floor,
        )

    def resolve_max_tokens(self, agent: AgentConfig, model: ModelConfig,
                           requested: int | None) -> int:
        """算出这次派发真正该给的 max_tokens。

        这是实测踩过的坑：deepseek-flash 是推理模型，给 16 个 token
        会被 reasoning_content 吃光，返回空 content + finish_reason=length。
        所以无论调用方要多少，都不能低于该模型的 floor。
        """
        want = requested or agent.max_tokens
        floor = model.max_tokens_floor if model.is_reasoning else 0
        val = max(want, floor, 1024)
        if model.max_output:
            val = min(val, model.max_output)
        return val
