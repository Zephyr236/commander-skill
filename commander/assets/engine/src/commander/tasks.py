"""任务管理 —— 全部落在文件里。

用户需求原文：「其中还需要包含任务管理，也是记录在文件中」

两份记录，各有用途：
    tasks/registry.jsonl   append-only 流水，机器读。绝不改写历史。
    tasks/BOARD.md         看板，人读。由 registry 重新生成。

另有 tasks/active/<task_id>/task.md —— 单个任务的完整档案（目标、验收标准、
拆解出的角度、每次派发的去向）。指挥官 loop 醒来先看这里。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .workspace import Workspace

STATUSES = ("pending", "active", "blocked", "done", "failed", "abandoned")
STATUS_ICON = {
    "pending": "○", "active": "◐", "blocked": "⊘",
    "done": "●", "failed": "✗", "abandoned": "—",
}


@dataclass
class TaskInfo:
    task_id: str
    title: str = ""
    status: str = "pending"
    goal: str = ""
    acceptance: str = ""
    angles: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    parent: str | None = None
    runs: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 本任务的四维预算覆盖（留空则用 policy 里的 [budget.task]）
    budget: dict = field(default_factory=dict)

    @property
    def tokens_used(self) -> int:
        return sum(int(r.get("usage_total") or 0) for r in self.runs)

    def to_markdown(self) -> str:
        lines = [
            f"# {self.title or self.task_id}",
            "",
            f"- **task_id**: `{self.task_id}`",
            f"- **状态**: {STATUS_ICON.get(self.status,'?')} {self.status}",
            f"- **创建**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.created))}",
            f"- **更新**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.updated))}",
            f"- **累计 tokens**: {self.tokens_used:,}",
        ]
        if self.parent:
            lines.append(f"- **父任务**: `{self.parent}`")
        lines += ["", "## 目标", "", self.goal or "(未填写)", ""]
        if self.acceptance:
            lines += ["## 验收标准", "", self.acceptance, ""]
        if self.angles:
            lines += ["## 拆解的角度", ""]
            lines += [f"{i}. {a}" for i, a in enumerate(self.angles, 1)]
            lines.append("")
        if self.runs:
            lines += ["## 派发记录", "",
                      "| 时间 | Agent | 模型 | 结果 | tokens | 成本单位 | 算力 | 耗时 | 进展 |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for r in self.runs:
                t = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
                ok = "✓" if r.get("ok") else "✗"
                pr = r.get("progress") or {}
                comp = pr.get("completion")
                prog = f"{comp:.0%}" if isinstance(comp, (int, float)) else "—"
                lines.append(
                    f"| {t} | `{r.get('agent_id')}` | {r.get('model')} | {ok} "
                    f"| {r.get('usage_total',0):,} | {r.get('cost_units',0):.2f} "
                    f"| {r.get('core_seconds',0):.0f}核秒 "
                    f"| {r.get('duration_s',0):.0f}s | {prog} |"
                )
            lines.append("")
            lines += ["产物："]
            for r in self.runs:
                if r.get("outbox"):
                    lines.append(f"- `{r['outbox']}`")
            lines.append("")
        if self.notes:
            lines += ["## 笔记", ""] + [f"- {n}" for n in self.notes] + [""]
        return "\n".join(lines)


class TaskRegistry:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    # ── 单任务档案 ────────────────────────────────────────────────────
    def task_file(self, task_id: str) -> Path:
        return self.ws.task_dir(task_id) / "task.md"

    def task_json(self, task_id: str) -> Path:
        return self.ws.task_dir(task_id) / "task.json"

    def load(self, task_id: str) -> TaskInfo | None:
        p = self.task_json(task_id)
        if not p.is_file():
            return None
        try:
            return TaskInfo(**json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def save(self, t: TaskInfo) -> None:
        t.updated = time.time()
        d = self.ws.task_dir(t.task_id)
        d.mkdir(parents=True, exist_ok=True)
        self.task_json(t.task_id).write_text(
            json.dumps(t.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.task_file(t.task_id).write_text(t.to_markdown(), encoding="utf-8")

    def create(
        self,
        task_id: str,
        *,
        title: str = "",
        goal: str = "",
        acceptance: str = "",
        angles: list[str] | None = None,
        parent: str | None = None,
    ) -> TaskInfo:
        t = TaskInfo(
            task_id=task_id, title=title or task_id, goal=goal,
            acceptance=acceptance, angles=angles or [], parent=parent,
            status="active",
        )
        self.save(t)
        self.append({**t.__dict__, "event": "created"})
        return t

    def update(self, task_id: str, **fields: Any) -> TaskInfo:
        t = self.load(task_id)
        if t is None:
            t = TaskInfo(task_id=task_id)
        for k, v in fields.items():
            if hasattr(t, k):
                setattr(t, k, v)
        self.save(t)
        self.append({"task_id": task_id, "event": "updated", **fields})
        return t

    def add_note(self, task_id: str, note: str) -> None:
        t = self.load(task_id) or TaskInfo(task_id=task_id)
        t.notes.append(note)
        self.save(t)

    # ── 流水 ──────────────────────────────────────────────────────────
    def append(self, record: dict) -> None:
        """append-only。历史不可改写 —— 这是审计的基础。"""
        self.ws.tasks_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), **record}
        with self.ws.task_registry.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def record_run(
        self, *, task_id: str, agent_id: str, backend: str, model: str,
        ok: bool, usage_total: int, duration_s: float, outbox: str,
        error: str | None, cost_units: float = 0.0, cost_usd: float = 0.0,
        core_seconds: float = 0.0, priced: bool = False,
        progress: dict | None = None,
    ) -> None:
        """记一次派发。

        成本与算力字段是后来加的 —— 老记录没有这些键时，读取方一律按 0 处理，
        不让格式演进把历史数据变成错误（见 budget.BudgetStore.consumption）。
        """
        entry = {
            "ts": time.time(), "agent_id": agent_id, "backend": backend,
            "model": model, "ok": ok, "usage_total": usage_total,
            "duration_s": round(duration_s, 2), "outbox": outbox,
            "error": (error or "")[:500] or None,
            # ── 资源四维 ──
            "cost_units": round(cost_units, 6),
            "cost_usd": round(cost_usd, 6),
            "core_seconds": round(core_seconds, 2),
            "priced": priced,
            # ── 进展信号（agent 自报，没有则为 None）──
            "progress": progress,
        }
        self.append({"task_id": task_id, "event": "run", **entry})
        t = self.load(task_id) or TaskInfo(task_id=task_id, status="active")
        t.runs.append(entry)
        self.save(t)

    def read_registry(self, limit: int | None = None) -> list[dict]:
        p = self.ws.task_registry
        if not p.is_file():
            return []
        out = []
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out[-limit:] if limit else out

    def list_active(self) -> list[TaskInfo]:
        d = self.ws.task_active
        if not d.is_dir():
            return []
        out = []
        for sub in sorted(d.iterdir()):
            if sub.is_dir():
                t = self.load(sub.name)
                if t:
                    out.append(t)
        return out

    # ── 看板 ──────────────────────────────────────────────────────────
    def render_board(self) -> Path:
        tasks = self.list_active()
        by_status: dict[str, list[TaskInfo]] = {}
        for t in tasks:
            by_status.setdefault(t.status, []).append(t)

        lines = [
            "# 任务看板",
            "",
            (f"> 由 `commander task board` 从 tasks/active/ 重新生成 · "
            f"{time.strftime('%Y-%m-%d %H:%M')}"),
            "",
            (f"活动任务 {len(tasks)} 个，累计 tokens "
            f"{sum(t.tokens_used for t in tasks):,}"),
            "",
        ]
        if not tasks:
            lines += ["_暂无活动任务。_", ""]
        for status in STATUSES:
            group = by_status.get(status)
            if not group:
                continue
            lines += [f"## {STATUS_ICON[status]} {status} ({len(group)})", ""]
            lines += ["| 任务 | 目标 | 派发次数 | tokens | 更新 |",
                      "|---|---|---|---|---|"]
            for t in sorted(group, key=lambda x: -x.updated):
                goal = (t.goal or t.title or "").replace("\n", " ")[:60]
                upd = time.strftime("%m-%d %H:%M", time.localtime(t.updated))
                lines.append(
                    f"| `{t.task_id}` | {goal} | {len(t.runs)} "
                    f"| {t.tokens_used:,} | {upd} |"
                )
            lines.append("")

        self.ws.task_board.write_text("\n".join(lines), encoding="utf-8")
        return self.ws.task_board

    def archive(self, task_id: str) -> Path | None:
        src = self.ws.task_dir(task_id)
        if not src.is_dir():
            return None
        dst = self.ws.task_archive / task_id
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            return dst
        src.rename(dst)
        self.append({"task_id": task_id, "event": "archived"})
        return dst
