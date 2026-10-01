"""消息历史留痕。

用户需求原文：「agent运行的过程必须要有详细的记录保存在对应的目录中，也就是完成的消息历史」

每次派发产出三份记录，缺一不可：

  agents/<id>/logs/<ts>-<run_id>.jsonl   逐事件，机器读（可回放、可统计）
  agents/<id>/logs/<ts>-<run_id>.md      人类可读时间线（出问题时给人看）
  agents/<id>/outbox/<task_id>.json      结构化最终结果（指挥官收集用）

设计原则：**流式写入**。agent 跑到一半崩了，已经产生的事件也必须在盘上
—— 恰恰是崩溃前的那几步最有诊断价值。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .schemas import Event, RunResult, RunSpec
from .workspace import Workspace

# 事件在人类可读时间线里的显示前缀
_EVENT_ICON = {
    "start": "▶",
    "thought": "💭",
    "text": "✎",
    "tool_call": "⚙",
    "tool_result": "↩",
    "artifact": "📦",
    "usage": "∑",
    "error": "✗",
    "log": "·",
    "result": "■",
}


class RunRecorder:
    """一次派发的记录器。用上下文管理器确保异常时也能收尾。"""

    def __init__(self, ws: Workspace, spec: RunSpec) -> None:
        self.ws = ws
        self.spec = spec
        self.started = time.time()
        self.stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(self.started))

        base = f"{self.stamp}-{spec.run_id}"
        self.log_dir = ws.agent_logs(spec.agent_id)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.jsonl_path = self.log_dir / f"{base}.jsonl"
        self.md_path = self.log_dir / f"{base}.md"

        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        self._md = self.md_path.open("a", encoding="utf-8")
        self._events: list[Event] = []
        self._closed = False

        self._write_header()

    # ── 头 ────────────────────────────────────────────────────────────
    def _write_header(self) -> None:
        self._jsonl.write(json.dumps({
            "type": "meta", "ts": self.started,
            "run_id": self.spec.run_id,
            "task_id": self.spec.task_id,
            "agent_id": self.spec.agent_id,
            "workdir": self.spec.workdir,
            "models": [m.model for m in self.spec.models],
            "skills": [s.name for s in self.spec.skills],
            "prompt_chars": len(self.spec.prompt),
        }, ensure_ascii=False) + "\n")

        self._md.write(
            f"# 派发记录 `{self.spec.run_id}`\n\n"
            f"| 项 | 值 |\n|---|---|\n"
            f"| 任务 | `{self.spec.task_id}` |\n"
            f"| Agent | `{self.spec.agent_id}` |\n"
            f"| 模型 | {', '.join(m.model for m in self.spec.models) or '(未指定)'} |\n"
            f"| 技能 | {', '.join(s.name for s in self.spec.skills) or '(无)'} |\n"
            f"| 工作目录 | `{self.spec.workdir}` |\n"
            f"| 开始 | {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))} |\n\n"
            f"## 指令\n\n```\n{self.spec.prompt.strip()}\n```\n\n"
            f"## 过程\n\n"
        )

    # ── 事件 ──────────────────────────────────────────────────────────
    def event(self, ev: Event) -> None:
        """记一个事件。流式落盘 —— 崩了也不丢。"""
        if self._closed:
            return
        self._events.append(ev)
        self._jsonl.write(ev.wire() + "\n")
        self._jsonl.flush()

        icon = _EVENT_ICON.get(ev.type, "?")
        ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
        d = ev.data or {}

        match ev.type:
            case "text":
                str(d.get("text", ""))
                # 流式正文原样写入；不补换行，避免把增量切碎后破坏 markdown
            case "thought":
                self._md.write(f"\n> 💭 {_clip(d.get('text'))}\n")
            case "tool_call":
                self._md.write(
                    f"\n{icon} **{d.get('name')}**"
                    f"({_clip(json.dumps(d.get('arguments'), ensure_ascii=False), 300)})\n"
                )
            case "tool_result":
                self._md.write(f"{icon} {_clip(d.get('result'), 300)}\n")
            case "artifact":
                self._md.write(f"\n{icon} 产物 `{d.get('path')}`\n")
            case "usage":
                self._md.write(
                    f"\n{icon} tokens: in={d.get('input_tokens')} "
                    f"out={d.get('output_tokens')} reason={d.get('reasoning_tokens')}\n"
                )
            case "error":
                self._md.write(f"\n{icon} **错误** {_clip(d.get('message'), 500)}\n")
            case _:
                self._md.write(f"\n{icon} [{ts}] {_clip(json.dumps(d, ensure_ascii=False), 500)}\n")

        self._md.flush()

    def stderr(self, line: str) -> None:
        """后端进程 stderr 原样落盘 —— 那里往往有最真实的报错。"""
        if self._closed or not line.strip():
            return
        self._jsonl.write(json.dumps(
            {"type": "stderr", "ts": time.time(), "line": line},
            ensure_ascii=False) + "\n")
        self._md.write(f"\n```\n{line.rstrip()}\n```\n")
        self._jsonl.flush()
        self._md.flush()

    # ── 收尾 ──────────────────────────────────────────────────────────
    def finalize(self, result: RunResult) -> Path:
        """写最终结果与 outbox。返回 outbox 文件路径。"""
        duration = time.time() - self.started
        result.duration_s = result.duration_s or duration

        # 后端通常已经发过 result 事件（会被 event() 转发并记下）。
        # 只有在它没发的情况下才补一条，避免日志里出现两个 result 让人困惑。
        if not self._events or self._events[-1].type != "result":
            self._jsonl.write(Event(type="result", data=result.model_dump()).wire() + "\n")
            self._jsonl.flush()

        status = "✓ 成功" if result.ok else f"✗ 失败（{result.error_kind or 'unknown'}）"
        self._md.write(
            f"\n\n## 结果\n\n**{status}** · {result.duration_s:.1f}s · "
            f"{result.model} · {result.turns} 轮\n\n"
            f"tokens: 输入 {result.usage.input_tokens} / "
            f"输出 {result.usage.output_tokens} / "
            f"推理 {result.usage.reasoning_tokens}\n\n"
        )
        if result.artifacts:
            self._md.write("产物：\n" + "\n".join(f"- `{a}`" for a in result.artifacts) + "\n\n")
        if result.error:
            self._md.write(f"错误详情：\n\n```\n{result.error}\n```\n\n")
        if result.text:
            self._md.write(f"### 输出\n\n{result.text}\n")

        outbox = self._write_outbox(result)
        self.close()
        return outbox

    def _write_outbox(self, result: RunResult) -> Path:
        """结构化结果，供指挥官程序化收集。

        文件名冲突防护：同一任务可能被派给**同一个 agent 多次**
        （`fanout` 里两个角度都给了 analyst 是常态）。
        若直接叫 <task_id>.json，第二次会把第一次覆盖掉 —— 结果静默丢失。
        实测踩过：3 个角度并发，最后只剩 2 份结果。
        """
        d = self.ws.agent_outbox(self.spec.agent_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.spec.task_id}.json"

        if path.exists():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
                if prev.get("run_id") != self.spec.run_id:
                    # 同一个 agent 的另一个角度 —— 换个不会撞的名字
                    path = d / f"{self.spec.task_id}-{self.spec.run_id}.json"
            except (OSError, json.JSONDecodeError):
                path = d / f"{self.spec.task_id}-{self.spec.run_id}.json"

        payload: dict[str, Any] = {
            "task_id": self.spec.task_id,
            "run_id": self.spec.run_id,
            "agent_id": self.spec.agent_id,
            "collected": False,          # 指挥官收走后置 true
            "ts": time.time(),
            "ts_human": time.strftime("%Y-%m-%d %H:%M:%S"),
            "result": result.model_dump(),
            "spec_digest": {
                "models": [m.model for m in self.spec.models],
                "skills": [s.name for s in self.spec.skills],
                "max_tokens": self.spec.max_tokens,
                "workdir": self.spec.workdir,
            },
            "records": {
                "jsonl": str(self.jsonl_path.relative_to(self.ws.root)),
                "timeline": str(self.md_path.relative_to(self.ws.root)),
            },
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._md.write(f"\n\n---\n\n记录文件：`{self.jsonl_path.name}`\n")
            self._jsonl.close()
            self._md.close()
        except OSError:
            pass

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _clip(v: Any, limit: int = 200) -> str:
    s = str(v) if v is not None else ""
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


# ══════════════════════════════════════════════════════════════════════════
# 读取端：指挥官收集结果
# ══════════════════════════════════════════════════════════════════════════


def list_outbox(ws: Workspace, agent_id: str | None = None, *, uncollected_only: bool = False) -> list[dict]:
    """列出 outbox 里的结果。指挥官用它收集已完成的派发。"""
    ids = [agent_id] if agent_id else ws.all_agent_ids()
    out: list[dict] = []
    for aid in ids:
        d = ws.agent_outbox(aid)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if uncollected_only and rec.get("collected"):
                continue
            rec["_file"] = str(f.relative_to(ws.root))
            out.append(rec)
    return out


def mark_collected(ws: Workspace, outbox_file: str) -> None:
    """标记某条结果已被指挥官收走，避免重复处理。"""
    p = ws.root / outbox_file
    if not p.is_file():
        return
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["collected"] = True
        rec["collected_at"] = time.time()
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def tail_events(ws: Workspace, agent_id: str, n: int = 20) -> list[dict]:
    """读某 agent 最近一次派发的事件流尾部。用于巡逻时快速看状态。"""
    d = ws.agent_logs(agent_id)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []
    lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out
