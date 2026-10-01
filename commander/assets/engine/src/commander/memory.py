"""指挥官的长期记忆。

用户需求原文：「其中还需要包含长期的记忆系统，记忆系统就是一个文件夹，
主要是给指挥官使用，里面存放了指挥官自己维护的记忆」
「获取记忆也是由一个独立的agent完成，在记忆文件夹中寻找指挥官需要的内容，
也寻找已经尝试过的方案」

结构：一条记忆 = 一个 markdown 文件，YAML frontmatter + 正文。
       memory/INDEX.md 是自动生成的总索引。

检索走 rg（结构化标签 + 全文），不走向量 —— 零依赖、快、结果可解释，
指挥官能直接读懂命中的原文，这一点对"避免重复踩坑"比召回率更重要。

五类记忆，用途不同：
    facts/      已验证的事实（结论 + 证据路径）
    attempts/   ★ 试过的方案（成功/失败/为什么/什么条件下可再试）
    decisions/  决策记录（选了什么、放弃了什么、理由）
    entities/   项目/系统/人 的实体卡
    journal/    按日期的指挥官日志
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .workspace import Workspace

KINDS = ("facts", "attempts", "decisions", "entities", "journal")
KIND_ICON = {
    "facts": "📌", "attempts": "🧪", "decisions": "⚖",
    "entities": "🏷", "journal": "📓",
}
STATUS_ICON = {"success": "✓", "failed": "✗", "partial": "◐", "open": "?"}


def slugify(s: str) -> str:
    """把标题转成文件名。中文保留，只清理文件系统不友好的字符。"""
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s.strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or f"note-{int(time.time())}"


@dataclass
class Memory:
    name: str
    kind: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = "open"
    confidence: str = "medium"
    related: list[str] = field(default_factory=list)
    date: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))
    path: Path | None = None
    meta: dict = field(default_factory=dict)

    def frontmatter(self) -> dict:
        return {
            "name": self.name,
            "type": self.kind,
            "tags": self.tags,
            "status": self.status,
            "confidence": self.confidence,
            "related": self.related,
            "date": self.date,
        }

    def render(self) -> str:
        fm = yaml.safe_dump(
            self.frontmatter(), allow_unicode=True, sort_keys=False, default_flow_style=False
        ).strip()
        return f"---\n{fm}\n---\n\n# {self.name}\n\n{self.body.strip()}\n"

    def one_line(self) -> str:
        """索引里的一行摘要。

        必须把内容里会破坏 markdown 表格的字符处理掉 ——
        正文里的 `|`（表格、或含竖线的代码）和换行会让整张索引表错位。
        这个坑很隐蔽：索引看上去生成成功了，实际渲染是乱的。
        """
        first = ""
        for raw in self.body.strip().splitlines():
            line = raw.strip()
            # 跳过标题、引用、以及 markdown 表格行（它们在单元格里没法看）
            if not line or line.startswith(("#", ">", "|", "```", "---")):
                continue
            first = line
            break
        if not first:
            # 正文全是表格/标题时，退而取第一行可读内容
            first = next(
                (s.strip() for s in self.body.strip().splitlines() if s.strip()), ""
            )

        def esc(s: str) -> str:
            return (s.replace("\\", "\\\\")
                     .replace("|", "\\|")
                     .replace("\r", " ")
                     .replace("\n", " "))

        icon = STATUS_ICON.get(self.status, "·")
        tags = " ".join(f"`{esc(t)}`" for t in self.tags[:4])
        return (f"| {icon} | `{esc(self.name)}` | {esc(first)[:80]} "
                f"| {tags} | {self.date} |")


class MemoryStore:
    """memory/ 目录的读写与检索。"""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    # ── 写 ────────────────────────────────────────────────────────────
    def write(self, mem: Memory) -> Path:
        if mem.kind not in KINDS:
            raise ValueError(f"未知记忆类型 {mem.kind!r}，应为 {KINDS}")
        d = self.ws.memory_kind(mem.kind)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{slugify(mem.name)}.md"
        if path.exists():
            # 同名不覆盖 —— 追加，保留历史
            path = d / f"{slugify(mem.name)}-{int(time.time()) % 100000}.md"
        path.write_text(mem.render(), encoding="utf-8")
        mem.path = path
        return path

    def note_attempt(
        self, name: str, *, approach: str, outcome: str,
        status: str = "failed", why: str = "", retry_when: str = "",
        tags: list[str] | None = None, related: list[str] | None = None,
    ) -> Path:
        """记一次尝试。这是最容易被忽略但最有价值的一类记忆。

        `retry_when` 字段是关键：记下"什么条件下可以再试"，
        否则指挥官会对同一个坑反复尝试。
        """
        body = [
            "## 尝试的做法", "", approach.strip(), "",
            "## 结果", "", outcome.strip(), "",
        ]
        if why:
            body += ["## 为什么（不）成立", "", why.strip(), ""]
        if retry_when:
            body += ["## 什么条件下可以再试", "", retry_when.strip(), ""]
        return self.write(Memory(
            name=name, kind="attempts", body="\n".join(body),
            status=status, tags=tags or [], related=related or [],
        ))

    # ── 读 ────────────────────────────────────────────────────────────
    def load(self, path: Path) -> Memory | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        meta: dict = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    meta = {}
                body = parts[2].strip()
        if not isinstance(meta, dict):
            meta = {}
        heading = next(
            (ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("# ")),
            path.stem,
        )
        return Memory(
            name=str(meta.get("name") or heading),
            kind=str(meta.get("type") or path.parent.name),
            body=body, tags=list(meta.get("tags") or []),
            status=str(meta.get("status") or "open"),
            confidence=str(meta.get("confidence") or "medium"),
            related=list(meta.get("related") or []),
            date=str(meta.get("date") or ""), path=path, meta=meta,
        )

    def all(self, kind: str | None = None) -> list[Memory]:
        kinds = [kind] if kind else list(KINDS)
        out: list[Memory] = []
        for k in kinds:
            d = self.ws.memory_kind(k)
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                m = self.load(f)
                if m:
                    out.append(m)
        out.sort(key=lambda m: m.date, reverse=True)
        return out

    # ── 检索 ──────────────────────────────────────────────────────────
    def has_rg(self) -> bool:
        return shutil.which("rg") is not None

    def search(
        self,
        query: str,
        *,
        kind: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        limit: int = 30,
    ) -> list[Memory]:
        """关键词 + 标签检索。

        先用 rg 缩小候选（快），再回读文件做结构化过滤（准）。
        rg 不可用时退化为纯 Python 扫描 —— 功能不减，只是慢一点。
        """
        kinds = [kind] if kind else list(KINDS)
        candidates: set[Path] = set()

        if self.has_rg() and query:
            for k in kinds:
                d = self.ws.memory_kind(k)
                if not d.is_dir():
                    continue
                try:
                    proc = subprocess.run(
                        ["rg", "--files-with-matches", "--ignore-case",
                         "--max-count", "1", query, str(d)],
                        capture_output=True, text=True, timeout=20,
                    )
                    for ln in proc.stdout.splitlines():
                        if ln.strip():
                            candidates.add(Path(ln.strip()))
                except (subprocess.SubprocessError, OSError):
                    pass
        if not candidates:
            for k in kinds:
                d = self.ws.memory_kind(k)
                if d.is_dir():
                    candidates.update(d.glob("*.md"))

        out: list[Memory] = []
        q = query.lower()
        for p in candidates:
            m = self.load(p)
            if m is None:
                continue
            if kind and m.kind != kind:
                continue
            if tag and tag not in m.tags:
                continue
            if status and m.status != status:
                continue
            if query and not self.has_rg():
                text = p.read_text(encoding="utf-8", errors="replace").lower()
                if q not in text:
                    continue
            out.append(m)

        # 排序：状态命中优先（失败的尝试最该被看到），再看日期
        prio = {"failed": 0, "partial": 1, "success": 2, "open": 3}
        out.sort(key=lambda m: (prio.get(m.status, 9), m.date), reverse=False)
        return out[:limit]

    # ── 索引 ──────────────────────────────────────────────────────────
    def render_index(self) -> Path:
        """重生 memory/INDEX.md —— 指挥官和 memory-scout 的入口。"""
        lines = [
            "# 指挥官记忆索引",
            "",
            f"> 由 `commander memory index` 生成 · {time.strftime('%Y-%m-%d %H:%M')}",
            "> 检索请用 `commander memory search <关键词>`，或读具体文件。",
            "",
        ]
        total = 0
        for kind in KINDS:
            mems = self.all(kind)
            total += len(mems)
            lines += [f"## {KIND_ICON[kind]} {kind} ({len(mems)})", ""]
            if not mems:
                lines += ["_（空）_", ""]
                continue
            lines += ["| 状态 | 名称 | 摘要 | 标签 | 日期 |",
                      "|---|---|---|---|---|"]
            lines += [m.one_line() for m in mems]
            lines.append("")
        lines.insert(3, f"共 {total} 条记忆。\n")
        self.ws.memory_index.write_text("\n".join(lines), encoding="utf-8")
        return self.ws.memory_index

    # ── 边界 ──────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return {k: len(self.all(k)) for k in KINDS}

    def stale_index(self) -> bool:
        """索引是否落后于记忆文件。指挥官的巡检项之一。

        ⚠️ 记忆库为空时**不算落后**。全新安装的工作区本来就没有记忆，
        此时提示"索引落后"是纯噪声 —— 而它恰好是新用户看到的第一条巡检结果，
        会让人以为装坏了。
        """
        idx = self.ws.memory_index
        if not idx.is_file():
            # 没有索引文件：只有"确实有记忆"时才算问题
            return any(self.all(k) for k in KINDS)
        idx_mtime = idx.stat().st_mtime
        for k in KINDS:
            d = self.ws.memory_kind(k)
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                if f.stat().st_mtime > idx_mtime:
                    return True
        return False
