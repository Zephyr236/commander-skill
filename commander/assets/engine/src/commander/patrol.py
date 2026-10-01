"""巡检 —— `/loop` 每次唤醒指挥官时跑的廉价检查。

用户需求原文：「loop指令提醒指挥官定期需要干什么需要检查干什么，需要维护什么记忆」

设计意图：把"该看什么"从指挥官的推理里挪到程序里。
loop 醒来的 prompt 就是「跑 patrol，按简报决策」—— 这样每次唤醒的
思考成本是固定的、可控的，不会随着会话变长而膨胀。

⚠️ 关于 /loop 的硬限制（实测查证）：
  · 循环任务 7 天后自动过期，最后一次触发后自删
  · 会话级作用域；自定节奏模式不随 --resume 恢复
  · 单会话最多 50 个定时任务
  · **错过的触发不补** —— 所以状态必须落在文件里，不能指望调度器
  · 仅在 Claude Code 运行且空闲时触发
  · disable-model-invocation:true 的 skill 不会作为定时任务执行
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .memory import MemoryStore
from .record import list_outbox
from .tasks import TaskRegistry
from .workspace import Workspace

# 任务多久没动静算「卡住」
STUCK_AFTER_S = 3600
# 巡检时最多列几条，避免简报本身变成负担
MAX_ITEMS = 8


@dataclass
class Finding:
    level: str          # action | warn | info
    category: str
    message: str
    hint: str = ""
    path: str = ""

    @property
    def icon(self) -> str:
        return {"action": "❗", "warn": "⚠", "info": "·"}.get(self.level, "·")


@dataclass
class PatrolReport:
    findings: list[Finding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    generated: float = field(default_factory=time.time)

    @property
    def actions(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "action"]

    def has_work(self) -> bool:
        return bool(self.actions)

    # ── 输出 ──────────────────────────────────────────────────────────
    def to_markdown(self) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.generated))
        lines = [f"# 巡检简报 · {ts}", ""]

        if not self.findings:
            lines += ["✅ 无异常。当前不需要动作。", ""]
        else:
            for level, title in (("action", "需要处理"), ("warn", "注意"), ("info", "状态")):
                group = [f for f in self.findings if f.level == level]
                if not group:
                    continue
                lines += [f"## {group[0].icon} {title} ({len(group)})", ""]
                for f in group[:MAX_ITEMS]:
                    lines.append(f"- **[{f.category}]** {f.message}")
                    if f.hint:
                        lines.append(f"  - → {f.hint}")
                    if f.path:
                        lines.append(f"  - `{f.path}`")
                if len(group) > MAX_ITEMS:
                    lines.append(f"- _…另有 {len(group)-MAX_ITEMS} 条_")
                lines.append("")

        if self.stats:
            lines += ["## 统计", ""]
            for k, v in self.stats.items():
                lines.append(f"- {k}: **{v}**")
            lines.append("")
        return "\n".join(lines)

    def to_oneline(self) -> str:
        """给 loop 用的一句话摘要。有活干时突出显示。"""
        a, w = len(self.actions), len([f for f in self.findings if f.level == "warn"])
        if a:
            head = self.actions[0].message[:80]
            return f"❗ {a} 项待处理 / {w} 项注意 — 首要：{head}"
        if w:
            return f"⚠ {w} 项注意，无阻塞"
        return "✅ 无异常"


class Patroller:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.ws: Workspace = self.cfg.ws
        self.tasks = TaskRegistry(self.ws)
        self.memory = MemoryStore(self.ws)

    def run(self, *, check_remote: bool = False) -> PatrolReport:
        rep = PatrolReport()
        self._check_tasks(rep)
        self._check_outbox(rep)
        self._check_memory(rep)
        self._check_guard(rep)
        self._check_agents(rep)
        self._check_backends(rep)
        self._check_budget(rep)
        if check_remote:
            self._check_remote(rep)
        self._fill_stats(rep)
        return rep

    # ── 各检查项 ──────────────────────────────────────────────────────
    def _check_tasks(self, rep: PatrolReport) -> None:
        now = time.time()
        active = self.tasks.list_active()

        # 看板过期检测。
        # 这个坑很隐蔽：BOARD.md 是生成物，任务变动后不会自动刷新，
        # 于是看板会显示"活动任务 0 个"而 tasks/active/ 下明明有任务 ——
        # 实测中一个 scout agent 注意到了这个矛盾，说明它会误导读者。
        board = self.ws.task_board
        if active and board.is_file():
            newest = max(
                (self.tasks.task_json(t.task_id).stat().st_mtime
                 for t in active if self.tasks.task_json(t.task_id).is_file()),
                default=0,
            )
            if newest > board.stat().st_mtime:
                rep.findings.append(Finding(
                    "warn", "任务", "看板 BOARD.md 落后于任务档案",
                    hint="看板是生成物，不会自动刷新：commander task board",
                    path="tasks/BOARD.md",
                ))
        elif active and not board.is_file():
            rep.findings.append(Finding(
                "warn", "任务", "缺少 tasks/BOARD.md",
                hint="commander task board",
            ))

        if not active:
            rep.findings.append(Finding(
                "info", "任务", "无活动任务",
                hint="若无待办可结束 loop，或新建任务：commander task new",
            ))

        for t in active:
            age = now - t.updated
            if t.status == "active" and age > STUCK_AFTER_S:
                rep.findings.append(Finding(
                    "action", "任务", f"`{t.task_id}` 已 {age/3600:.1f}h 无更新",
                    hint="检查卡在哪：commander task show " + t.task_id,
                    path=str(self.tasks.task_file(t.task_id).relative_to(self.ws.root)),
                ))
            if t.status == "blocked":
                rep.findings.append(Finding(
                    "action", "任务", f"`{t.task_id}` 处于 blocked",
                    hint="阻塞原因：" + (t.notes[-1] if t.notes else "(未记录)"),
                ))
            if not t.runs and t.status == "active":
                rep.findings.append(Finding(
                    "warn", "任务", f"`{t.task_id}` 已建但从未派发",
                    hint="拆解角度后派发，或标记为 abandoned",
                ))
            used = t.tokens_used
            cap = self.cfg.policy.budget.max_tokens_per_task
            if used > cap * 0.8:
                rep.findings.append(Finding(
                    "warn", "预算", f"`{t.task_id}` 已用 {used:,}/{cap:,} tokens",
                    hint="接近上限，考虑收敛或提高预算",
                ))

    def _check_outbox(self, rep: PatrolReport) -> None:
        recs = list_outbox(self.ws, uncollected_only=True)
        if not recs:
            return
        ok = [r for r in recs if r.get("result", {}).get("ok")]
        bad = [r for r in recs if not r.get("result", {}).get("ok")]

        if ok:
            rep.findings.append(Finding(
                "action", "结果", f"{len(ok)} 条结果待收集",
                hint="读 outbox 取回结论，然后 commander outbox collect --all",
                path=str(self.ws.agents_dir.relative_to(self.ws.root)) + "/<id>/outbox/",
            ))
        if bad:
            kinds: dict[str, int] = {}
            for r in bad:
                k = r.get("result", {}).get("error_kind") or "unknown"
                kinds[k] = kinds.get(k, 0) + 1
            detail = ", ".join(f"{k}×{v}" for k, v in kinds.items())
            rep.findings.append(Finding(
                "warn", "结果", f"{len(bad)} 条派发失败（{detail}）",
                hint="失败的尝试值得记入 memory/attempts/，避免重复踩坑",
            ))

    def _check_memory(self, rep: PatrolReport) -> None:
        if self.memory.stale_index():
            rep.findings.append(Finding(
                "action", "记忆", "记忆索引落后于记忆文件",
                hint="commander memory index",
                path="memory/INDEX.md",
            ))
        stats = self.memory.stats()
        if sum(stats.values()) == 0:
            rep.findings.append(Finding(
                "info", "记忆", "记忆库为空",
                hint="长任务开始前先派 memory-scout 查历史；有结论后及时沉淀",
            ))
        failed = self.memory.search("", kind="attempts", status="failed", limit=100)
        if len(failed) >= 5:
            rep.findings.append(Finding(
                "warn", "记忆", f"已记录 {len(failed)} 条失败尝试",
                hint="派发前让 memory-scout 先查 attempts/，避免重复",
            ))

    def _check_guard(self, rep: PatrolReport) -> None:
        log = self.ws.guard_log
        if not log.is_file():
            return
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        recent = lines[-20:]
        if recent:
            rep.findings.append(Finding(
                "warn", "契约", f"有 {len(lines)} 条目录契约违规记录",
                hint="说明有 agent 试图越界写入，检查日志确认是否配置问题",
                path="logs/guard-violations.jsonl",
            ))

    def _check_agents(self, rep: PatrolReport) -> None:
        ids = self.ws.all_agent_ids()
        if not ids:
            rep.findings.append(Finding(
                "info", "编制", "尚无 agent 工作目录",
                hint="首次派发时会自动创建；编制表见 config/agents.toml",
            ))
            return
        for aid in ids:
            if not self.ws.agent_brief(aid).is_file():
                rep.findings.append(Finding(
                    "warn", "编制", f"agent `{aid}` 缺 BRIEF.md",
                    hint="补上职责与边界说明，便于复现与审计",
                ))

    def _check_budget(self, rep: PatrolReport) -> None:
        """预算水位。

        预算不是刹车，是信号 —— 巡检的价值在于**把"该做决策了"这件事主动摆到
        指挥官面前**，而不是等某个硬上限把任务打断。
        """
        from .budget import BudgetStore

        store = BudgetStore(self.ws, self.cfg)

        # 全局
        g_lim = store.limits()["global"]
        g_cons = store.consumption()
        if not g_lim.is_empty():
            used, dim = g_cons.worst_ratio(g_lim)
            if used >= 0.85:
                rep.findings.append(Finding(
                    "action", "预算",
                    f"全局预算已用 {used:.0%}（{dim}）",
                    hint="整盘快见底了。跑 `./.commander/cmd budget show` 看是哪个任务吃掉的",
                ))
            elif used >= 0.6:
                rep.findings.append(Finding(
                    "warn", "预算", f"全局预算已用 {used:.0%}（{dim}）",
                    hint="./.commander/cmd budget show",
                ))

        # 每个活动任务
        for t in self.tasks.list_active():
            cons = store.consumption(task_id=t.task_id)
            if cons.runs == 0:
                continue
            lims = store.limits(task_id=t.task_id)
            worst, worst_used, _worst_name = None, 0.0, ""
            for name, lim in lims.items():
                if lim.is_empty():
                    continue
                u, _ = cons.worst_ratio(lim)
                if u > worst_used:
                    worst, worst_used, _worst_name = lim, u, name
            if worst is None or worst_used < 0.6:
                continue

            rep_ = store.evaluate_task(t.task_id)
            level = "action" if rep_.needs_attention else "warn"
            rep.findings.append(Finding(
                level, "预算",
                f"`{t.task_id}` {rep_.one_line()}",
                hint=(rep_.reasons[0][:110] if rep_.reasons
                      else f"看详情：./.commander/cmd budget show -t {t.task_id}"),
                path=f"tasks/active/{t.task_id}/budget/",
            ))

    def _check_backends(self, rep: PatrolReport) -> None:
        # 与 doctor 共用同一实现 —— 重后端查远端而不是本地，
        # 否则会误导成"未安装，建议本地装"（crewai 136 个包，本地装不下）
        from .backends import backend_readiness
        rows = backend_readiness(self.cfg, check_remote=True)
        # 只报「该能用却不能用」的。没配远端时重后端没装是预期状态，
        # 报警纯属噪声（而它恰好是新用户看到的第一条巡检结果）。
        missing = [r for r in rows if not r["ok"] and r.get("expected", True)]
        if missing:
            names = ", ".join(r["backend"] for r in missing)
            rep.findings.append(Finding(
                "warn", "后端", f"{len(missing)} 个后端未就绪: {names}",
                hint="；".join(f"{r['backend']}: {r['detail']}" for r in missing[:3])
                     + ("；重依赖建议 remote sync" if any(
                         r["where"] == "remote" for r in missing) else ""),
            ))

    def _check_remote(self, rep: PatrolReport) -> None:
        from .guard import Guard
        from .ssh_runner import RemoteRunner
        runner = RemoteRunner(self.cfg, Guard(self.ws, self.cfg.policy.guard))
        for h in self.cfg.hosts.values():
            ok, detail = runner.check(h)
            rep.findings.append(Finding(
                "info" if ok else "warn", "远端",
                f"{h.name} ({h.host}) {'可达' if ok else '不可达'}",
                hint="" if ok else f"{detail} —— 重后端会降级本地或失败",
            ))

    def _fill_stats(self, rep: PatrolReport) -> None:
        active = self.tasks.list_active()
        uncollected = list_outbox(self.ws, uncollected_only=True)
        mem = self.memory.stats()
        from .budget import BudgetStore
        _bs = BudgetStore(self.ws, self.cfg)
        _g = _bs.consumption()
        _gl = _bs.limits()["global"]
        _gu, _ = _g.worst_ratio(_gl) if not _gl.is_empty() else (0.0, "")
        rep.stats = {
            "活动任务": len(active),
            "全局预算水位": f"{_gu:.0%}" if _gu else "未设限",
            "累计成本单位": f"{_g.cost_units:.2f}",
            "累计算力": f"{_g.core_seconds:.0f} 核秒",
            "待收集结果": len(uncollected),
            "累计 tokens": sum(t.tokens_used for t in active),
            "记忆总数": sum(mem.values()),
            "编制 agent": len(self.cfg.agents),
            "可用模型": len(self.cfg.available_models()),
        }

    # ── 落盘 ──────────────────────────────────────────────────────────
    def write_report(self, rep: PatrolReport) -> Path:
        d = self.ws.logs_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patrol-latest.md"
        p.write_text(rep.to_markdown(), encoding="utf-8")
        hist = d / "patrol-history.jsonl"
        import json
        with hist.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": rep.generated,
                "actions": len(rep.actions),
                "warns": len([x for x in rep.findings if x.level == "warn"]),
                "summary": rep.to_oneline(),
                "stats": rep.stats,
            }, ensure_ascii=False) + "\n")
        return p
