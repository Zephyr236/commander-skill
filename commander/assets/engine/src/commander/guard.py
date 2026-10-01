"""目录契约的强制执行层。

用户需求原文：「需要严格规定每一个目录的作用，每一个agent必须要在指定的目录中工作」

本模块是这条规则的**唯一执行点**。文档里写死的规则如果没人检查，就只是建议。
所有文件写入、子进程启动都必须先过这里。

三层防线：
  1. resolve_workdir()  —— 子进程 cwd 钉死在 agents/<id>/work/，物理上就到不了别处
  2. assert_writable()  —— 任何显式写入路径都要过白名单/黑名单检查
  3. audit_argv()       —— 子进程参数里出现工作区路径时的越界检查（尽力而为）
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .workspace import Workspace


class GuardViolation(PermissionError):
    """目录契约被违反。这是硬错误，调用方不应捕获后继续。"""

    def __init__(self, message: str, *, actor: str, path: str, rule: str) -> None:
        super().__init__(message)
        self.actor = actor
        self.path = path
        self.rule = rule

    def as_record(self) -> dict[str, Any]:
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "actor": self.actor,
            "path": self.path,
            "rule": self.rule,
            "message": str(self),
        }


@dataclass
class GuardPolicy:
    """从 config/policy.toml 的 [guard] 段加载。"""

    enforce: bool = True
    memory_writers: list[str] = field(default_factory=lambda: ["commander", "memory-scout"])
    writable_roots: list[str] = field(default_factory=lambda: ["agents/", "logs/", "tasks/"])
    denied_roots: list[str] = field(default_factory=lambda: [".git/", ".claude/"])
    # 子进程沙箱：auto（有 bwrap 就用）| bwrap | none
    sandbox: str = "auto"

    @classmethod
    def from_toml(cls, data: dict[str, Any]) -> GuardPolicy:
        g = data.get("guard", {})
        return cls(
            enforce=g.get("enforce_workdir_isolation", True),
            memory_writers=list(g.get("memory_writers", ["commander", "memory-scout"])),
            writable_roots=list(g.get("writable_roots", ["agents/", "logs/", "tasks/"])),
            denied_roots=list(g.get("denied_roots", [".git/", ".claude/"])),
            sandbox=g.get("sandbox", "auto"),
        )


class Guard:
    """把目录契约变成会抛异常的检查。"""

    def __init__(self, ws: Workspace, policy: GuardPolicy | None = None) -> None:
        self.ws = ws
        self.policy = policy or GuardPolicy()
        self._denials: list[dict[str, Any]] = []

    # ── 内部 ──────────────────────────────────────────────────────────
    def _rel(self, path: Path | str) -> str:
        """把绝对路径转成相对工作区根的字符串，用 / 分隔。"""
        p = Path(path)
        if not p.is_absolute():
            p = (self.ws.root / p)
        try:
            rel = p.resolve().relative_to(self.ws.root)
        except ValueError:
            return f"<OUTSIDE>/{p}"
        return rel.as_posix()

    def _matches(self, rel: str, prefixes: Iterable[str]) -> bool:
        return any(rel == p.rstrip("/") or rel.startswith(p) for p in prefixes)

    def _violate(self, msg: str, *, actor: str, path: str, rule: str) -> None:
        v = GuardViolation(msg, actor=actor, path=path, rule=rule)
        self._denials.append(v.as_record())
        self._flush_denials()
        raise v

    def _flush_denials(self) -> None:
        """拒绝记录必须留痕 —— 失败的尝试也是情报。"""
        if not self._denials:
            return
        try:
            self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
            with self.ws.guard_log.open("a", encoding="utf-8") as f:
                for rec in self._denials:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志写不进去不能掩盖原始违规
        finally:
            self._denials.clear()

    # ── 防线 1：工作目录 ──────────────────────────────────────────────
    def resolve_workdir(self, agent_id: str) -> Path:
        """返回该 agent 的工作目录，并确保它在工作区内。

        子进程的 cwd 必须用这个返回值。agent 的相对路径写入因此天然被限制在
        自己的一亩三分地里 —— 这是最省事也最可靠的一道防线。
        """
        self.assert_valid_agent_id(agent_id)
        wd = self.ws.agent_workdir(agent_id)

        rel = self._rel(wd)
        if rel.startswith("<OUTSIDE>") or not rel.startswith(f"agents/{agent_id}/"):
            self._violate(
                f"agent {agent_id!r} 的工作目录逃逸出沙箱: {wd}",
                actor=agent_id, path=str(wd), rule="workdir_escape",
            )
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    def assert_valid_agent_id(self, agent_id: str) -> None:
        """agent_id 会变成目录名，必须防止路径穿越。"""
        if not agent_id or agent_id in {".", ".."}:
            self._violate(f"非法 agent_id: {agent_id!r}", actor="commander",
                          path=agent_id, rule="invalid_agent_id")
        if "/" in agent_id or "\\" in agent_id or "\x00" in agent_id:
            self._violate(f"agent_id 不得含路径分隔符: {agent_id!r}", actor="commander",
                          path=agent_id, rule="invalid_agent_id")
        if agent_id.startswith("."):
            self._violate(f"agent_id 不得以点开头: {agent_id!r}", actor="commander",
                          path=agent_id, rule="invalid_agent_id")

    # ── 防线 2：写入路径 ──────────────────────────────────────────────
    def assert_writable(self, path: Path | str, *, actor: str) -> Path:
        """检查 actor 是否有权写 path。返回解析后的绝对路径。

        actor 为 "commander" 时代表指挥官自身（代表用户操作，权限最高但仍有禁区）。
        actor 为某个 agent_id 时，只能写自己的工作区 + 被授权的公共区。
        """
        p = Path(path)
        if not p.is_absolute():
            p = (self.ws.root / p)
        p = p.resolve()
        rel = self._rel(p)

        if not self.policy.enforce:
            return p

        # ① 绝对禁区：任何身份都不许写
        if self._matches(rel, self.policy.denied_roots):
            self._violate(
                f"{actor!r} 试图写入禁区 {rel!r}（{self.policy.denied_roots}）",
                actor=actor, path=rel, rule="denied_root",
            )

        # ② 工作区外
        if rel.startswith("<OUTSIDE>"):
            self._violate(
                f"{actor!r} 试图写入工作区之外的 {p}",
                actor=actor, path=rel, rule="outside_workspace",
            )

        # ③ 记忆目录：只有白名单身份可写
        if rel == "memory" or rel.startswith("memory/"):
            if actor not in self.policy.memory_writers:
                self._violate(
                    f"{actor!r} 无权写记忆目录。允许的身份: {self.policy.memory_writers}。"
                    f"需要沉淀记忆时请把结论交回指挥官，由指挥官或 memory-scout 写入。",
                    actor=actor, path=rel, rule="memory_not_writable",
                )
            return p

        # ④ 其他 agent 的目录：只能写自己的
        if rel.startswith("agents/"):
            owner = rel.split("/", 2)[1] if rel.count("/") >= 1 else ""
            if actor != "commander" and owner != actor:
                self._violate(
                    f"{actor!r} 试图写入别的 agent 的目录（属主: {owner!r}）: {rel}",
                    actor=actor, path=rel, rule="cross_agent_write",
                )
            return p

        # ⑤ 公共可写区
        if self._matches(rel, self.policy.writable_roots):
            return p

        # ⑥ 其余一律拒绝（默认拒绝，而非默认允许）
        self._violate(
            f"{actor!r} 试图写入未授权的路径 {rel!r}。"
            f"允许的前缀: {self.policy.writable_roots}",
            actor=actor, path=rel, rule="not_in_writable_roots",
        )

    # ── 防线 3：子进程参数 ────────────────────────────────────────────
    # 输出类参数：这些标志后面跟的路径是「要写到哪」
    _OUTPUT_FLAGS = frozenset({
        "-o", "--output", "--out", "--output-file", "--output-dir",
        "-w", "--write", "--save", "--dest", "--destination",
        "--log-file", "--report", "--artifact", "--export",
    })

    def audit_argv(self, argv: list[str], *, actor: str, cwd: Path) -> None:
        """尽力检查子进程参数里是否夹带越界的工作区路径。

        这不是沙箱，能力有限 —— argv 里的路径无法区分「读」和「写」，
        所以不能对所有路径一律套用写入规则（那会把 `python runner.py`
        这种正常调用也拦下来）。

        只查两类，误报率可控：
          ① 落在绝对禁区（.git/ .claude/）或记忆区的路径 —— 无论出现在哪都拦
          ② 跟在输出类标志后面的路径 —— 那才是真的在指定写入目标

        真正的保障仍是防线 1（cwd 隔离），这里只做补充。
        """
        if not self.policy.enforce:
            return

        root_str = str(self.ws.root)

        def tokens_of(arg: str):
            """把 `--output=/x/y` 或 `/x/y` 拆成候选路径。"""
            for part in arg.replace("=", " ").split():
                if part.startswith(root_str):
                    yield part

        def check(token: str) -> None:
            rel = self._rel(token)
            if rel.startswith("<OUTSIDE>"):
                return
            if self._matches(rel, self.policy.denied_roots) or \
               rel == "memory" or rel.startswith("memory/"):
                self.assert_writable(token, actor=actor)

        # ① 禁区/记忆区：任何位置出现都拦
        for arg in argv:
            if root_str not in arg:
                continue
            for token in tokens_of(arg):
                check(token)

        # ② 输出类标志后面的路径
        for i, arg in enumerate(argv):
            flag = arg.split("=", 1)[0]
            if flag not in self._OUTPUT_FLAGS:
                continue
            # `--output=/path` 形式
            if "=" in arg:
                for token in tokens_of(arg.split("=", 1)[1]):
                    self.assert_writable(token, actor=actor)
                continue
            # `--output /path` 形式
            if i + 1 < len(argv):
                for token in tokens_of(argv[i + 1]):
                    self.assert_writable(token, actor=actor)

    # ── 环境变量 ──────────────────────────────────────────────────────
    def sandbox_env(self, agent_id: str, workdir: Path) -> dict[str, str]:
        """给子进程准备的环境变量。

        要点：给每个 agent 一个独立的 HOME / TMPDIR，避免它们通过
        ~/.cache 之类的路径互相串味，也避免污染指挥官自己的环境。
        """
        env = dict(os.environ)
        agent_home = self.ws.agent_dir(agent_id) / ".home"
        agent_tmp = self.ws.agent_dir(agent_id) / ".tmp"
        agent_home.mkdir(parents=True, exist_ok=True)
        agent_tmp.mkdir(parents=True, exist_ok=True)

        env.update({
            "HOME": str(agent_home),
            "TMPDIR": str(agent_tmp),
            "COMMANDER_AGENT_ID": agent_id,
            "COMMANDER_WORKDIR": str(workdir),
            "COMMANDER_ROOT": str(self.ws.root),
            # 明确告诉子进程它的边界在哪
            "COMMANDER_CONTRACT": (
                f"你只在 {workdir} 内工作。产物写入 stdout(JSON) 或该目录。"
                f"不得写 {self.ws.memory_dir} 或其他 agent 的目录。"
            ),
            # 防止子进程里的 Claude Code 读到指挥官的全局配置
            "CLAUDE_CONFIG_DIR": str(agent_home / ".claude"),
        })

        # 隔离父进程的虚拟环境痕迹。
        # 不清掉的话 `uv run --project bin/backends/<sdk>` 会报
        # "VIRTUAL_ENV does not match the project environment path" 并拒绝工作。
        for leak in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX",
                     "PYTHONHOME", "PYTHONPATH", "UV_ACTIVE"):
            env.pop(leak, None)

        return env
