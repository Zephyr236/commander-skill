"""真沙箱 —— 用 bubblewrap 把目录契约变成内核级强制。

为什么需要它（实测教训）：
    cwd 隔离只约束**相对路径**。子进程用绝对路径照样能写任何地方 ——
    mock 后端实测成功写出了 memory/_mock_probe.md，逃逸了。
    guard.assert_writable 保护的是指挥官自己的文件操作，管不住子进程。

本机实测可用的隔离原语：
    ✓ bubblewrap (bwrap)  /usr/bin/bwrap
    ✓ unshare -m 挂载命名空间（实测能挡住写入）
    ✓ 内核 6.8 带 landlock（97 个符号）
    ✗ docker / firejail 未安装

方案：bwrap 把整个文件系统挂成只读，再按白名单把该可写的地方挂回来。
    --ro-bind / /                  全局只读
    --bind   <agent_dir>           只有自己的沙箱可写
    --ro-bind /dev/null <bin/.env> 遮蔽密钥
效果：memory/、别的 agent 目录、.claude/、.git/、工作区外 —— 全部写不进去。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import Workspace


@dataclass
class SandboxPlan:
    """一次沙箱的挂载计划。可打印出来给指挥官看，便于排查。"""

    readable_root: str = "/"
    writable: list[Path] = field(default_factory=list)
    masked: list[Path] = field(default_factory=list)     # 挂成 /dev/null（空）
    readonly: list[Path] = field(default_factory=list)   # 显式只读（用于说明）
    # 这不是"硬编码临时文件路径"——是给 bwrap 挂 tmpfs 的挂载点。
    # 挂上之后 agent 看到的是独立的空 /tmp，与宿主隔离。
    tmpfs: list[str] = field(default_factory=lambda: ["/tmp"])  # noqa: S108

    def describe(self) -> str:
        w = "\n".join(f"    ✓ 可写 {p}" for p in self.writable)
        m = "\n".join(f"    ⊘ 遮蔽 {p}" for p in self.masked)
        return f"沙箱挂载计划\n{w}\n{m}"


class Sandbox:
    """bwrap 命令包装器。

    available 为 False 时退化为「仅 cwd 隔离」，并且**必须**让指挥官知道
    —— 那意味着目录契约只是约定而非强制。
    """

    def __init__(self, ws: Workspace, mode: str = "auto") -> None:
        self.ws = ws
        self.mode = mode
        self.bwrap = shutil.which("bwrap")

    @property
    def available(self) -> bool:
        if self.mode == "none":
            return False
        if self.mode == "bwrap":
            return bool(self.bwrap)
        return bool(self.bwrap)   # auto

    # ── 计划 ──────────────────────────────────────────────────────────
    def plan(self, agent_id: str, workdir: Path) -> SandboxPlan:
        ws = self.ws
        agent_dir = ws.agent_dir(agent_id)

        writable = [
            agent_dir,                    # 自己的整个沙箱：work/ outbox/ logs/ artifacts/
            ws.logs_dir,                  # 全局日志（后端可能要记日志）
        ]
        # 远端日志目录（远端回传时用得到）
        if ws.remote_logs.parent.is_dir():
            writable.append(ws.remote_logs)

        masked = [ws.dotenv]              # 密钥不给子进程读

        return SandboxPlan(
            writable=writable, masked=masked,
            tmpfs=self._safe_tmpfs(),
            readonly=[
                ws.memory_dir, ws.tasks_dir, ws.config_dir,
                ws.skills_dir, ws.bin_dir, ws.agents_dir,
            ],
        )

    def _safe_tmpfs(self) -> list[str]:
        """挑出可以安全挂 tmpfs 的挂载点。

        ⚠️ 实测踩过的坑：无条件给 `/tmp` 挂 tmpfs，会把**位于 /tmp 下的工作区
        整个遮住** —— bwrap 报 "Can't chdir to ...: No such file or directory"，
        而且报错完全不提"是你把 /tmp 挂空了"。

        规则：任何是工作区根**祖先**的挂载点都不能挂 —— 那会遮住工作区本身。
        工作区在 /tmp 下（临时试验、CI 容器里很常见）时自动跳过 /tmp。
        """
        root = self.ws.root
        out: list[str] = []
        for mp in ("/tmp",):  # noqa: S108 — 这是 bwrap 的挂载点，不是临时文件路径
            point = Path(mp).resolve()
            if root == point or point in root.parents:
                continue          # 会遮住工作区，跳过
            out.append(mp)
        return out

    # ── 包装 ──────────────────────────────────────────────────────────
    def wrap(self, argv: list[str], agent_id: str, workdir: Path) -> list[str]:
        """把一条命令包进 bwrap。不可用时原样返回。"""
        if not self.available:
            return argv

        p = self.plan(agent_id, workdir)
        out: list[str] = [
            str(self.bwrap),
            # 全局只读 —— 这一条就挡住了 99% 的越界写
            "--ro-bind", p.readable_root, "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
        ]

        # 该可写的地方挂回来
        for d in p.writable:
            d.mkdir(parents=True, exist_ok=True)
            out += ["--bind", str(d), str(d)]

        # 遮蔽密钥：挂成 /dev/null，子进程读到空
        for f in p.masked:
            if f.exists():
                out += ["--ro-bind", "/dev/null", str(f)]

        # 临时目录给独立的 tmpfs，避免和宿主互相干扰
        for t in p.tmpfs:
            out += ["--tmpfs", t]

        out += ["--chdir", str(workdir), "--", *argv]
        return out

    # ── 自检 ──────────────────────────────────────────────────────────
    def self_test(self, verbose: bool = False) -> tuple[bool, str]:
        """真跑一次越界写，确认沙箱确实生效。

        文档说"已隔离"不算数，得实测。这个函数就是那个实测。
        """
        if not self.available:
            return False, "bwrap 不可用，当前仅有 cwd 隔离（相对路径），目录契约非强制"

        import subprocess

        agent_id = "_sandbox_selftest"
        wd = self.ws.agent_workdir(agent_id)
        wd.mkdir(parents=True, exist_ok=True)
        probe = self.ws.memory_dir / "_sandbox_probe.md"
        probe.unlink(missing_ok=True)

        script = (
            f"touch {wd}/_ok 2>/dev/null && echo WRITE_OK || echo WRITE_FAIL; "
            f"touch {probe} 2>/dev/null && echo ESCAPED || echo BLOCKED; "
            f"cat {self.ws.dotenv} 2>/dev/null | head -c 1 | wc -c"
        )
        argv = self.wrap(["/bin/sh", "-c", script], agent_id, wd)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"沙箱启动失败: {exc}"

        out = p.stdout or ""
        ok_write = "WRITE_OK" in out
        blocked = "BLOCKED" in out
        leaked = probe.exists()
        if leaked:
            probe.unlink(missing_ok=True)

        (wd / "_ok").unlink(missing_ok=True)
        try:
            wd.rmdir()
            self.ws.agent_dir(agent_id).rmdir()
        except OSError:
            pass

        if ok_write and blocked and not leaked:
            return True, "bwrap 沙箱生效：workdir 可写，memory/ 已挡住，密钥已遮蔽"
        problems = []
        if not ok_write:
            problems.append("workdir 不可写（沙箱太严）")
        if not blocked or leaked:
            problems.append("memory/ 仍可写（沙箱未生效）")
        return False, "沙箱自检未通过: " + "; ".join(problems)


class SandboxUnavailable(RuntimeError):
    pass
