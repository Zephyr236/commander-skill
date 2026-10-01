"""工作区路径解析。

所有目录的唯一定义处。任何模块需要路径都必须走这里，不允许自己拼字符串
—— 否则「目录契约」就只是文档而不是约束。
"""

from __future__ import annotations

import os
from pathlib import Path

# 工作区根的标记文件。从 CWD 向上查找，找到即认为是根。
ROOT_MARKER = ".commander-root"

# 内嵌布局的目录名。装进已有项目时，引擎与状态全部收在这个目录里，
# 避免与项目自己的 config/ logs/ skills/ 撞名。
EMBED_DIR = ".commander"


class WorkspaceNotFound(RuntimeError):
    """向上找不到工作区根。"""


def find_root(start: Path | None = None) -> Path:
    """从 start（默认 CWD）向上查找工作区根。

    支持两种布局，按优先级：

      ① COMMANDER_ROOT 环境变量显式指定
      ② 扁平布局     <dir>/.commander-root              → 根 = <dir>
      ③ 内嵌布局     <dir>/.commander/.commander-root   → 根 = <dir>/.commander

    内嵌布局是给"把指挥官装进一个已有项目"用的：引擎与状态全部收在
    `.commander/` 里，项目自己的 config/ logs/ skills/ 一点都不受影响。

    先找 ② 再找 ③，且**每层都是先扁平后内嵌** —— 这样嵌套场景下（工作区
    里又套了一个工作区）取的是最靠近 CWD 的那个。
    """
    env = os.environ.get("COMMANDER_ROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_dir():
            raise WorkspaceNotFound(f"COMMANDER_ROOT 指向的目录不存在: {p}")
        return p

    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ROOT_MARKER).is_file():
            return candidate
        embedded = candidate / EMBED_DIR
        if (embedded / ROOT_MARKER).is_file():
            return embedded

    raise WorkspaceNotFound(
        f"从 {cur} 向上未找到 {ROOT_MARKER}（也未找到 {EMBED_DIR}/{ROOT_MARKER}）。"
        f"请在工作区根目录下运行，或设置 COMMANDER_ROOT 环境变量。"
    )


class Workspace:
    """工作区所有目录的访问点。

    每个属性都对应目录契约中的一个角色，命名与文档一致。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root: Path = (root or find_root()).resolve()

    # ── 顶层 ──────────────────────────────────────────────────────────
    @property
    def bin_dir(self) -> Path:          # Python 工程（本文件所在）
        return self.root / "bin"

    @property
    def src_dir(self) -> Path:          # 派发层源码
        return self.bin_dir / "src" / "commander"

    @property
    def backends_dir(self) -> Path:     # 每个 SDK 一个独立 uv 工程
        return self.bin_dir / "backends"

    @property
    def dotenv(self) -> Path:
        return self.bin_dir / ".env"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def models_toml(self) -> Path:
        return self.config_dir / "models.toml"

    @property
    def agents_toml(self) -> Path:
        return self.config_dir / "agents.toml"

    @property
    def policy_toml(self) -> Path:
        return self.config_dir / "policy.toml"

    # ── 记忆 ──────────────────────────────────────────────────────────
    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def memory_index(self) -> Path:
        return self.memory_dir / "INDEX.md"

    def memory_kind(self, kind: str) -> Path:
        """memory/{facts,attempts,decisions,entities,journal}"""
        return self.memory_dir / kind

    # ── 任务 ──────────────────────────────────────────────────────────
    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def task_registry(self) -> Path:
        return self.tasks_dir / "registry.jsonl"

    @property
    def task_board(self) -> Path:
        return self.tasks_dir / "BOARD.md"

    @property
    def task_active(self) -> Path:
        return self.tasks_dir / "active"

    @property
    def task_archive(self) -> Path:
        return self.tasks_dir / "archive"

    def task_dir(self, task_id: str) -> Path:
        return self.task_active / task_id

    # ── 下属 agent ────────────────────────────────────────────────────
    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    def agent_dir(self, agent_id: str) -> Path:
        """某个下属 agent 的沙箱根目录。"""
        return self.agents_dir / agent_id

    def agent_workdir(self, agent_id: str) -> Path:
        """该 agent **唯一**允许写文件的目录。子进程 cwd 钉死在这里。"""
        return self.agent_dir(agent_id) / "work"

    def agent_outbox(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / "outbox"

    def agent_inbox(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / "inbox"

    def agent_logs(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / "logs"

    def agent_artifacts(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / "artifacts"

    def agent_brief(self, agent_id: str) -> Path:
        return self.agent_dir(agent_id) / "BRIEF.md"

    # ── 技能库 ────────────────────────────────────────────────────────
    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    def skill_dir(self, name: str) -> Path:
        return self.skills_dir / name

    def skill_file(self, name: str) -> Path:
        return self.skills_dir / name / "SKILL.md"

    # ── 远端 ──────────────────────────────────────────────────────────
    @property
    def remote_dir(self) -> Path:
        return self.root / "remote"

    @property
    def hosts_toml(self) -> Path:
        return self.remote_dir / "hosts.toml"

    @property
    def remote_keys(self) -> Path:
        return self.remote_dir / "keys"

    @property
    def remote_staging(self) -> Path:
        return self.remote_dir / "staging"

    @property
    def remote_logs(self) -> Path:
        return self.remote_dir / "logs"

    # ── 全局日志 ──────────────────────────────────────────────────────
    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def guard_log(self) -> Path:
        return self.logs_dir / "guard-violations.jsonl"

    # ── 给人看的路径 ──────────────────────────────────────────────────
    def display_path(self, p: Path) -> str:
        """把工作区内的路径转成**用户从项目根敲得出来**的形式。

        两种布局下同一个文件的位置不同：
            扁平   <项目>/bin/.env
            内嵌   <项目>/.commander/bin/.env

        文档里的「路径描述」统一相对工作区根（所以是 `bin/.env`），
        但**错误提示里要让用户能照着敲** —— 那就得从项目根算。
        混用这两种口径会让某一种布局的用户照抄到不存在的路径。
        """
        try:
            rel = p.resolve().relative_to(self.root)
        except (ValueError, OSError):
            return str(p)
        if self.root.name == EMBED_DIR:
            return f"./{EMBED_DIR}/{rel.as_posix()}"
        return rel.as_posix()

    # ── 枚举 ──────────────────────────────────────────────────────────
    def all_agent_ids(self) -> list[str]:
        """列出 agents/ 下所有已实例化的 agent 目录名。"""
        if not self.agents_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.agents_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def ensure_agent_dirs(self, agent_id: str) -> None:
        """幂等地建出某个 agent 的完整目录骨架。"""
        for d in (
            self.agent_dir(agent_id),
            self.agent_workdir(agent_id),
            self.agent_outbox(agent_id),
            self.agent_inbox(agent_id),
            self.agent_logs(agent_id),
            self.agent_artifacts(agent_id),
        ):
            d.mkdir(parents=True, exist_ok=True)

    def ensure_all(self) -> None:
        """建出工作区所有标准目录（幂等）。"""
        for d in (
            self.config_dir, self.memory_dir, self.tasks_dir,
            self.task_active, self.task_archive, self.agents_dir,
            self.skills_dir, self.remote_dir, self.remote_keys,
            self.remote_staging, self.remote_logs, self.logs_dir,
            self.backends_dir,
            self.memory_kind("facts"), self.memory_kind("attempts"),
            self.memory_kind("decisions"), self.memory_kind("entities"),
            self.memory_kind("journal"),
        ):
            d.mkdir(parents=True, exist_ok=True)
