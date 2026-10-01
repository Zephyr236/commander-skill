"""动态预算管理 —— 预算不是刹车，是决策信号。

用户需求原文：

  「预算不应只是硬限制，而应作为任务决策信号……当达到预警阈值或预算阶段节点时，
   生成结构化状态报告提交给 Commander。Commander 根据资源消耗与任务收益评估
   是否继续投入、增加预算、调整执行策略或切换新的解决路径。
   如果当前策略消耗大量资源但进展有限，应优先触发策略重规划，
   而不是简单增加预算。」

━━ 四维资源 ━━
    tokens        token 消耗（输入+输出）
    cost_units    经济成本的**代理量**（见下）
    wall_seconds  墙钟时间
    core_seconds  算力（墙钟 × 核数）

━━ 为什么用「成本单位」而不是美元 ━━
不要求用户提供精确单价，但成本必须进决策。做法是按模型的 cost_tier 折算：

    cost_units = tokens / 1M × tier 权重      (free=0, cheap=1, standard=3, premium=9)

这一维**永远可用、跨模型可比**，足以支撑"用贵的模型值不值"这类判断。
如果 models.toml 里填了真实 `pricing`，就自动换成真钱（cost_usd），
决策逻辑不变 —— 只是数字从"相对量"变成"绝对量"。

━━ 三层预算 ━━
    global  跨所有任务的总盘
    task    单个任务
    agent   单个下属

每层四个维度独立设限，取**最紧张的那一维**作为该层的利用率。

━━ 判定优先级（核心）━━
    消耗大 + 进展小   → replan（重规划）★ 而不是加预算
    进展好 + 预算紧   → increase（加预算）
    有明确障碍        → adjust（调整策略）
    信息不足          → continue（再看一轮）
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .workspace import Workspace

# ══════════════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════════════

# 建议动作。顺序即优先级 —— 判定时从上往下试，第一个命中的生效。
CONTINUE = "continue"     # 继续当前路径
ADJUST = "adjust"         # 调整执行策略（换个角度/换个 agent/改参数）
REPLAN = "replan"         # 重规划：当前路径本身有问题
INCREASE = "increase"     # 增加预算：路径是对的，只是不够花
ABORT = "abort"           # 中止

ACTIONS = (ABORT, REPLAN, ADJUST, INCREASE, CONTINUE)

ACTION_LABEL = {
    CONTINUE: "继续",
    ADJUST: "调整策略",
    REPLAN: "重规划",
    INCREASE: "增加预算",
    ABORT: "中止",
}

# 预算水位
OK, WARN, CRITICAL, EXHAUSTED = "ok", "warn", "critical", "exhausted"

LEVEL_LABEL = {OK: "充足", WARN: "偏高", CRITICAL: "接近上限", EXHAUSTED: "已耗尽"}

# cost_tier → 成本权重（每 1M token 折算成多少成本单位）
DEFAULT_COST_WEIGHTS = {
    "free": 0.0, "cheap": 1.0, "standard": 3.0, "premium": 9.0,
}

# 「烧钱效率」的容忍上限：单位进展消耗超过这个倍数就认为路径有问题
BURN_REPLAN_THRESHOLD = 2.5


# ══════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Limits:
    """一层预算的限额。None 表示这一维不限。"""

    tokens: int | None = None
    cost_units: float | None = None
    cost_usd: float | None = None
    wall_seconds: float | None = None
    core_seconds: float | None = None
    runs: int | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in asdict(self).values())

    @classmethod
    def from_dict(cls, d: dict | None) -> Limits:
        d = d or {}
        known = {k: d.get(k) for k in cls.__dataclass_fields__}
        return cls(**known)

    def scaled(self, factor: float) -> Limits:
        """按倍数放大 —— 用于「增加预算」时给出建议值。"""
        out = {}
        for k, v in asdict(self).items():
            out[k] = None if v is None else (int(v * factor) if isinstance(v, int)
                                             else v * factor)
        return Limits(**out)


@dataclass
class Consumption:
    """资源消耗。四维 + 两个成本口径。"""

    tokens: int = 0
    cost_units: float = 0.0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    core_seconds: float = 0.0
    runs: int = 0
    # 有没有真实定价。没有时 cost_usd 无意义，决策只看 cost_units
    priced: bool = False

    def merge(self, other: Consumption) -> Consumption:
        return Consumption(
            tokens=self.tokens + other.tokens,
            cost_units=self.cost_units + other.cost_units,
            cost_usd=self.cost_usd + other.cost_usd,
            wall_seconds=self.wall_seconds + other.wall_seconds,
            core_seconds=self.core_seconds + other.core_seconds,
            runs=self.runs + other.runs,
            priced=self.priced or other.priced,
        )

    def ratios(self, lim: Limits) -> dict[str, float | None]:
        """各维度的利用率。None 表示该维未设限。"""
        return {
            "tokens": _ratio(self.tokens, lim.tokens),
            "cost_units": _ratio(self.cost_units, lim.cost_units),
            # 有真定价时才看美元维度
            "cost_usd": _ratio(self.cost_usd, lim.cost_usd) if self.priced else None,
            "wall_seconds": _ratio(self.wall_seconds, lim.wall_seconds),
            "core_seconds": _ratio(self.core_seconds, lim.core_seconds),
            "runs": _ratio(self.runs, lim.runs),
        }

    def worst_ratio(self, lim: Limits) -> tuple[float, str]:
        """取最紧张的那一维。返回 (利用率, 维度名)。"""
        rs = {k: v for k, v in self.ratios(lim).items() if v is not None}
        if not rs:
            return 0.0, "none"
        k = max(rs, key=lambda x: rs[x])
        return rs[k], k

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> Consumption:
        d = d or {}
        return cls(**{k: d.get(k, 0) for k in cls.__dataclass_fields__
                      if k in d})


def _ratio(used: float, limit: float | None) -> float | None:
    if not limit or limit <= 0:
        return None
    return used / limit


@dataclass
class Progress:
    """进展信号。

    机械信号（turns / 产出量）**总是有**；声明信号（完成度、置信度、障碍）
    来自 agent 自己 —— 只有它知道自己在原地打转还是在收敛。
    """

    turns: int = 0
    output_chars: int = 0
    artifacts: int = 0
    # ↓ agent 自报（可选）
    completion: float | None = None
    confidence: float | None = None
    blockers: list[str] = field(default_factory=list)
    summary: str = ""
    declared: bool = False

    @property
    def effective(self) -> float | None:
        """折算成一个 0-1 的进展估计。

        有自报就用自报（agent 最清楚）；没有就退化为机械估计。
        机械估计只能从产出量猜，**粗但比没有强** —— 它至少能区分
        "跑了 5 轮吐了 200 字"和"跑了 5 轮吐了 20000 字"。
        """
        if self.completion is not None:
            return max(0.0, min(1.0, self.completion))
        if self.turns <= 0 and self.output_chars <= 0:
            return None
        # 产出量的对数刻度：500 字 ≈ 0.2，5000 字 ≈ 0.5，20000 字 ≈ 0.8
        import math
        by_text = min(1.0, math.log10(max(self.output_chars, 1) / 50) / 2.6)
        by_turns = min(1.0, self.turns / 15)
        return max(0.0, min(1.0, 0.6 * by_text + 0.4 * by_turns))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StatusReport:
    """结构化状态报告 —— 提交给指挥官决策用。"""

    scope: str                    # global | task:<id> | agent:<id> | run:<id>
    ts: float = field(default_factory=time.time)
    level: str = OK
    recommendation: str = CONTINUE
    reasons: list[str] = field(default_factory=list)

    consumption: Consumption = field(default_factory=Consumption)
    limits: Limits = field(default_factory=Limits)
    used_ratio: float = 0.0
    binding_dim: str = "none"     # 最紧张的是哪一维

    progress: Progress = field(default_factory=Progress)
    burn_ratio: float | None = None   # 单位进展消耗了多少预算

    task_id: str = ""
    agent_id: str = ""

    @property
    def level_label(self) -> str:
        return LEVEL_LABEL.get(self.level, self.level)

    @property
    def action_label(self) -> str:
        return ACTION_LABEL.get(self.recommendation, self.recommendation)

    @property
    def needs_attention(self) -> bool:
        return self.recommendation != CONTINUE or self.level in (CRITICAL, EXHAUSTED)

    def one_line(self) -> str:
        p = self.progress.effective
        p_s = f"{p:.0%}" if p is not None else "?"
        return (
            f"[{self.level_label}] 用掉 {self.used_ratio:.0%}"
            f"（{self.binding_dim}）· 进展 {p_s}"
            + (f" · 烧钱效率 {self.burn_ratio:.1f}x" if self.burn_ratio else "")
            + f" → 建议 {self.action_label}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level_label"] = self.level_label
        d["action_label"] = self.action_label
        d["needs_attention"] = self.needs_attention
        d["one_line"] = self.one_line()
        return d


# ══════════════════════════════════════════════════════════════════════════
# 成本模型
# ══════════════════════════════════════════════════════════════════════════


class CostModel:
    """把 token 用量换算成成本。

    两种口径同时维护：
      · cost_units —— 按 cost_tier 折算的代理量，**永远可用**
      · cost_usd   —— 只有配了 pricing 的模型才算得出来
    决策优先用 cost_usd（真钱），没有就退回 cost_units（相对量）。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        raw = getattr(cfg.policy, "cost_weights", None) or {}
        self.weights = {**DEFAULT_COST_WEIGHTS, **raw}

    def of(self, model_alias: str, usage) -> tuple[float, float, bool]:
        """返回 (cost_units, cost_usd, priced)。"""
        m = self.cfg.models.get(model_alias)
        tier = getattr(m, "cost_tier", "standard") if m else "standard"
        weight = self.weights.get(tier, 1.0)

        total = (getattr(usage, "input_tokens", 0)
                 + getattr(usage, "output_tokens", 0))
        units = total / 1_000_000 * weight

        pricing = getattr(m, "pricing", None) or {} if m else {}
        if not pricing:
            return units, 0.0, False

        usd = (
            getattr(usage, "input_tokens", 0) / 1_000_000 * pricing.get("input", 0)
            + getattr(usage, "output_tokens", 0) / 1_000_000 * pricing.get("output", 0)
            + getattr(usage, "cached_tokens", 0) / 1_000_000
            * pricing.get("cached_input", pricing.get("input", 0))
        )
        return units, usd, True


# ══════════════════════════════════════════════════════════════════════════
# 判定 —— 本模块的核心
# ══════════════════════════════════════════════════════════════════════════


def evaluate(
    consumption: Consumption,
    limits: Limits,
    progress: Progress,
    *,
    scope: str = "",
    task_id: str = "",
    agent_id: str = "",
    warn_at: float = 0.6,
    critical_at: float = 0.85,
) -> StatusReport:
    """把「花了多少」和「做成了多少」放在一起，给出下一步建议。

    ★ 最重要的一条：**消耗大而进展小时，建议重规划而不是加预算。**
      给一个走错路的策略加预算，只是让它错得更贵。
      只有当路径被证明是对的（进展良好）而只是不够花时，加预算才是对的。
    """
    used, dim = consumption.worst_ratio(limits)
    p = progress.effective

    level = OK
    if used >= 1.0:
        level = EXHAUSTED
    elif used >= critical_at:
        level = CRITICAL
    elif used >= warn_at:
        level = WARN

    reasons: list[str] = []
    burn: float | None = None

    # ── 烧钱效率：单位进展消耗了多少比例的资源 ────────────────────────
    # 只在「有进展信号」且「已经花掉一些」时才有意义。
    if p is not None and p > 0.02 and used > 0.05:
        burn = used / p

    # ══ 判定顺序即优先级 ══════════════════════════════════════════════

    # ① 已耗尽 —— 必须停，但要先判断"是路径问题还是预算问题"
    if level == EXHAUSTED:
        if p is None or p < 0.5:
            rec = REPLAN
            reasons.append(
                f"预算已耗尽（{dim} 用满 {used:.0%}）而进展只有 "
                f"{'未知' if p is None else f'{p:.0%}'} —— "
                f"这是路径问题，加预算只会让它错得更贵"
            )
        else:
            rec = INCREASE
            reasons.append(
                f"预算耗尽但进展已达 {p:.0%}，接近完成 —— 值得追加"
            )
        return StatusReport(scope=scope, level=level, recommendation=rec,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ② 信息不足 —— 头一两轮不下结论
    if p is None and consumption.runs <= 1:
        reasons.append("样本不足（首轮），先继续再评估")
        return StatusReport(scope=scope, level=level, recommendation=CONTINUE,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ③ ★ 烧得多、进展少 → 重规划（用户明确要求优先于加预算）
    if used >= 0.5 and (p is None or p < 0.25):
        rec = REPLAN
        reasons.append(
            f"已消耗 {used:.0%} 的预算但进展仅 {p:.0%} —— "
            f"继续加预算只是把同一个错误做得更贵。先重规划："
            f"换角度、换 agent、或换解法"
        )
        return StatusReport(scope=scope, level=level, recommendation=rec,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ④ 烧钱效率过高 → 同样是路径问题
    if burn is not None and burn > BURN_REPLAN_THRESHOLD and used > 0.35:
        rec = REPLAN
        reasons.append(
            f"烧钱效率 {burn:.1f}x（{used:.0%} 预算换 {p:.0%} 进展，"
            f"阈值 {BURN_REPLAN_THRESHOLD}x）—— 单位产出的代价太高，路径该换了"
        )
        return StatusReport(scope=scope, level=level, recommendation=rec,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ⑤ agent 自己都没把握 → 先别投入了，它需要的是换个思路
    if (progress.declared and progress.confidence is not None
            and progress.confidence < 0.35 and used > 0.4):
        rec = REPLAN
        reasons.append(
            f"agent 自报置信度仅 {progress.confidence:.0%}（已花 {used:.0%}）—— "
            f"它自己都没把握，继续投入是在赌"
        )
        return StatusReport(scope=scope, level=level, recommendation=rec,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ⑥ 遇到明确障碍 → 调整策略，而不是重规划（还没到换路径的程度）
    if progress.blockers and used >= 0.3:
        rec = ADJUST
        reasons.append(
            f"agent 报告 {len(progress.blockers)} 个障碍："
            f"{progress.blockers[0][:60]} —— 先针对障碍调整，不必推倒重来"
        )
        return StatusReport(scope=scope, level=level, recommendation=rec,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ⑦ 进展好但预算快见底 → 这才是该加预算的场景
    if p >= 0.6 and used >= 0.8:
        sug = limits.scaled(1.5)
        rec = INCREASE
        reasons.append(
            f"进展 {p:.0%} 且预算用到 {used:.0%} —— 路径是对的，只是不够花。"
            f"建议加到 {_fmt_limits(sug)}"
        )
        return StatusReport(scope=scope, level=level, recommendation=rec,
                            reasons=reasons, consumption=consumption,
                            limits=limits, used_ratio=used, binding_dim=dim,
                            progress=progress, burn_ratio=burn,
                            task_id=task_id, agent_id=agent_id)

    # ⑧ 水位偏高但一切正常
    if level in (WARN, CRITICAL):
        reasons.append(
            f"预算用到 {used:.0%}（{dim}），进展 {p:.0%}，"
            f"比率正常 —— 继续观察，接近上限时再评估"
        )

    return StatusReport(scope=scope, level=level, recommendation=CONTINUE,
                        reasons=reasons, consumption=consumption, limits=limits,
                        used_ratio=used, binding_dim=dim, progress=progress,
                        burn_ratio=burn, task_id=task_id, agent_id=agent_id)


def _fmt_limits(lim: Limits) -> str:
    parts = []
    if lim.tokens:
        parts.append(f"tokens={lim.tokens:,}")
    if lim.cost_units:
        parts.append(f"成本单位={lim.cost_units:.0f}")
    if lim.wall_seconds:
        parts.append(f"时间={lim.wall_seconds / 60:.0f}min")
    if lim.core_seconds:
        parts.append(f"算力={lim.core_seconds:.0f}核秒")
    if lim.runs:
        parts.append(f"轮次={lim.runs}")
    return "、".join(parts) or "(无限制)"


# ══════════════════════════════════════════════════════════════════════════
# 预算与消耗的存取
# ══════════════════════════════════════════════════════════════════════════


class BudgetStore:
    """三层预算的解析，以及消耗的累计。

    消耗**不单独维护计数器**，而是从 append-only 的 run 流水里派生 ——
    少一份会漂移的状态，且天然可审计。
    """

    def __init__(self, ws: Workspace, cfg) -> None:
        self.ws = ws
        self.cfg = cfg

    # ── 限额 ──────────────────────────────────────────────────────────
    def limits(self, *, task_id: str | None = None,
               agent_id: str | None = None) -> dict[str, Limits]:
        """解析出该场景下生效的三层限额。任务/agent 级可覆盖全局默认。"""
        pol = getattr(self.cfg.policy, "budget", None)
        base = {
            "global": Limits.from_dict(getattr(pol, "global_", None)),
            "task": Limits.from_dict(getattr(pol, "task", None)),
            "agent": Limits.from_dict(getattr(pol, "agent", None)),
        }

        if task_id:
            t = self._task_budget(task_id)
            if t:
                base["task"] = t
        if agent_id:
            a = self.cfg.agents.get(agent_id)
            if a and getattr(a, "budget", None):
                base["agent"] = Limits.from_dict(a.budget)
        return base

    def _task_budget(self, task_id: str) -> Limits | None:
        from .tasks import TaskRegistry
        t = TaskRegistry(self.ws).load(task_id)
        b = getattr(t, "budget", None) if t else None
        return Limits.from_dict(b) if b else None

    # ── 消耗 ──────────────────────────────────────────────────────────
    def _runs(self, *, task_id: str | None = None,
              agent_id: str | None = None) -> list[dict]:
        from .tasks import TaskRegistry
        recs = TaskRegistry(self.ws).read_registry()
        out = []
        for r in recs:
            if r.get("event") != "run":
                continue
            if task_id and r.get("task_id") != task_id:
                continue
            if agent_id and r.get("agent_id") != agent_id:
                continue
            out.append(r)
        return out

    def consumption(self, *, task_id: str | None = None,
                    agent_id: str | None = None) -> Consumption:
        """累计消耗。

        流水里存了 cost_units / core_seconds 就直接加；老记录没有这些字段
        时按 0 计 —— 不让格式演进把历史数据变成错误。
        """
        c = Consumption()
        for r in self._runs(task_id=task_id, agent_id=agent_id):
            c = c.merge(Consumption(
                tokens=int(r.get("usage_total") or 0),
                cost_units=float(r.get("cost_units") or 0.0),
                cost_usd=float(r.get("cost_usd") or 0.0),
                wall_seconds=float(r.get("duration_s") or 0.0),
                core_seconds=float(r.get("core_seconds") or 0.0),
                runs=1,
                priced=bool(r.get("priced")),
            ))
        return c

    # ── 评估 ──────────────────────────────────────────────────────────
    def evaluate_task(self, task_id: str, *, agent_id: str | None = None,
                      progress: Progress | None = None) -> StatusReport:
        lims = self.limits(task_id=task_id, agent_id=agent_id)
        cons = self.consumption(task_id=task_id)
        # 取最紧的一层：任何一层逼近上限都要报警
        worst_lim, worst_used, _worst_name = None, 0.0, "global"
        for name, lim in lims.items():
            if lim.is_empty():
                continue
            u, _ = cons.worst_ratio(lim)
            if u > worst_used:
                worst_lim, worst_used, _worst_name = lim, u, name

        return evaluate(
            cons, worst_lim or Limits(), progress or self._task_progress(task_id),
            scope=f"task:{task_id}", task_id=task_id, agent_id=agent_id or "",
            warn_at=self._threshold("warn", 0.6),
            critical_at=self._threshold("critical", 0.85),
        )

    def _task_progress(self, task_id: str) -> Progress:
        """从该任务最近一次派发的记录里取进展。"""
        recs = self._runs(task_id=task_id)
        for r in reversed(recs):
            pr = r.get("progress")
            if pr:
                return Progress(**{k: v for k, v in pr.items()
                                   if k in Progress.__dataclass_fields__})
        if recs:
            return Progress(turns=len(recs))
        return Progress()

    def _threshold(self, name: str, default: float) -> float:
        pol = getattr(self.cfg.policy, "budget", None)
        th = getattr(pol, "thresholds", None) or {}
        return float(th.get(name, default))


# ══════════════════════════════════════════════════════════════════════════
# 决策历史
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class Decision:
    """指挥官的一次预算决策。记下来是为了以后能问：
    「当时为什么加预算？值吗？」"""

    task_id: str
    action: str
    reason: str
    ts: float = field(default_factory=time.time)
    # 决策时的现场
    snapshot: dict = field(default_factory=dict)
    # 决策之后实际发生了什么（事后回填，用于评估决策质量）
    outcome: str = ""
    outcome_ts: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action_label"] = ACTION_LABEL.get(self.action, self.action)
        return d


class DecisionLog:
    """决策历史，append-only。

    存两处：
      · 全局 streams  logs/budget-decisions.jsonl   —— 跨任务复盘用
      · 任务档案里也追加一条                          —— 单任务回溯用
    """

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    @property
    def path(self) -> Path:
        return self.ws.logs_dir / "budget-decisions.jsonl"

    def record(self, d: Decision) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(d.to_dict(), ensure_ascii=False) + "\n")
        try:
            from .tasks import TaskRegistry
            TaskRegistry(self.ws).add_note(
                d.task_id,
                f"[预算决策] {ACTION_LABEL.get(d.action, d.action)} —— {d.reason}",
            )
        except Exception:
            pass

    def history(self, task_id: str | None = None,
                limit: int = 50) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for ln in self.path.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if task_id and rec.get("task_id") != task_id:
                continue
            out.append(rec)
        return out[-limit:]

    def annotate_outcome(self, task_id: str, outcome: str) -> bool:
        """回填"决策之后发生了什么"。用于事后评估决策质量。"""
        if not self.path.is_file():
            return False
        lines = self.path.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
        changed = False
        for i in range(len(lines) - 1, -1, -1):
            try:
                rec = json.loads(lines[i])
            except json.JSONDecodeError:
                continue
            if rec.get("task_id") == task_id and not rec.get("outcome"):
                rec["outcome"] = outcome
                rec["outcome_ts"] = time.time()
                lines[i] = json.dumps(rec, ensure_ascii=False)
                changed = True
                break
        if changed:
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return changed


# ══════════════════════════════════════════════════════════════════════════
# 状态报告的落盘
# ══════════════════════════════════════════════════════════════════════════


def write_report(ws: Workspace, rep: StatusReport) -> Path:
    """状态报告落盘。任务级的写进任务目录，全局的写进 logs/。"""
    d = ws.task_dir(rep.task_id) / "budget" if rep.task_id else ws.logs_dir / "budget"
    d.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(rep.ts))
    p = d / f"{stamp}-{rep.level}.json"
    p.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                 encoding="utf-8")

    hist = d / "history.jsonl"
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": rep.ts, "scope": rep.scope, "level": rep.level,
            "action": rep.recommendation, "used_ratio": round(rep.used_ratio, 4),
            "binding_dim": rep.binding_dim,
            "progress": rep.progress.effective,
            "burn_ratio": rep.burn_ratio,
            "one_line": rep.one_line(),
        }, ensure_ascii=False) + "\n")
    return p


def latest_report(ws: Workspace, task_id: str | None = None) -> dict | None:
    d = (ws.task_dir(task_id) / "budget") if task_id else (ws.logs_dir / "budget")
    hist = d / "history.jsonl"
    if not hist.is_file():
        return None
    lines = [ln for ln in hist.read_text(encoding="utf-8",
                                         errors="replace").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def parse_status_block(text: str) -> tuple[dict | None, str]:
    """从 agent 的回复里剥出状态块，返回 (解析结果, 剥掉后的正文)。

    agent 在回复末尾附一段：

        <<<COMMANDER_STATUS
        {"completion": 0.6, "confidence": 0.7,
         "blockers": ["缺少压测环境"], "summary": "已完成接口梳理"}
        >>>

    解析后**从正文里删掉** —— 它是给指挥官看的元信息，不该混进交付物。
    """
    import re

    m = re.search(r"<<<COMMANDER_STATUS\s*(\{.*?\})\s*>>>", text, re.S)
    if not m:
        return None, text

    clean = (text[:m.start()] + text[m.end():]).strip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None, clean
    return (data if isinstance(data, dict) else None), clean


def progress_from_status(data: dict | None) -> Progress:
    """把 agent 自报的状态块转成 Progress。"""
    if not data:
        return Progress()
    def _f(key, lo=0.0, hi=1.0):
        v = data.get(key)
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return None
    blockers = data.get("blockers") or []
    if isinstance(blockers, str):
        blockers = [blockers]
    return Progress(
        completion=_f("completion"),
        confidence=_f("confidence"),
        blockers=[str(b) for b in blockers][:5],
        summary=str(data.get("summary") or "")[:300],
        declared=True,
    )


def status_instruction() -> str:
    """要 agent 附状态块的那段指令。放进 system prompt。"""
    return """
## 结束前，附一段状态块

在你回复的**最后**（正文之后）附上这段，供指挥官判断是否继续投入：

<<<COMMANDER_STATUS
{"completion": 0.0到1.0的完成度, "confidence": 0.0到1.0的置信度,
 "blockers": ["遇到的具体障碍，没有就留空数组"],
 "summary": "一句话说清做完了什么、还差什么"}
>>>

要求：
- `completion` 是你对**整个任务**完成度的估计，不是这一轮的进度
- `confidence` 是你对自己结论的把握。**不知道就说低**，不要为了好看而报高 ——
  指挥官会拿这个数字决定要不要继续投钱
- `blockers` 只写真正卡住你的东西，写不出就别编
- 这段是给机器读的，正文该写什么还写什么
""".strip()
