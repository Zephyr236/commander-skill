"""工作区脚手架 —— 把指挥官铺到新目录，或装进一个已有项目。

两种布局，由 `embed` 决定：

  ┌─ 扁平布局（embed=False）—— 独立工作区
  │    dest/
  │    ├── .commander-root
  │    ├── bin/  config/  skills/
  │    ├── memory/  tasks/  agents/  logs/  remote/
  │    └── .claude/{skills/commander, agents, loop.md, settings.json}
  │
  └─ 内嵌布局（embed=True）—— 装进已有项目
       project/
       ├── src/  package.json  config/  logs/     ← 项目自己的，完全不动
       ├── .claude/                               ← 与已有的合并，不覆盖
       │   ├── skills/commander/  agents/  loop.md
       │   └── settings.json
       └── .commander/                            ← ★ 引擎与状态全收在这里
           ├── .commander-root  cmd
           ├── bin/  config/  skills/
           └── memory/  tasks/  agents/  logs/  remote/

内嵌布局存在的理由：`config/` `logs/` `skills/` 在真实项目里太常见，
平铺进去必然撞名。收进 `.commander/` 之后零撞名风险，卸载也只需删一个目录。

两种布局共用同一个启动器路径 `./.commander/cmd` —— 技能文档因此只需写一套
命令，不必为每种布局各写一遍（那是错误的主要来源）。

拷什么 / 不拷什么
    拷   引擎源码、配置、技能库、.claude（技能定义与 subagent）
    不拷 任何 .venv（体积大且与路径绑定，换位置就废）
    不拷 密钥文件 .env —— 最重要的一条，绝不把凭据带到别人机器上
    不拷 运行状态（memory/tasks/agents/logs）—— 除非显式要求
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import EMBED_DIR, Workspace

# ── 模板内容：会拷到新工作区 ──────────────────────────────────────────────
TEMPLATE_DIRS = ("bin", "config", "skills")
TEMPLATE_FILES = ("README.md", ".gitignore", "setup.sh")

# ── 永远排除 ──────────────────────────────────────────────────────────────
EXCLUDE_DIR_NAMES = frozenset({
    ".venv", "venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "node_modules", ".home", ".tmp",  # 后两个是 agent 的隔离 HOME/TMP
})
EXCLUDE_FILE_NAMES = frozenset({
    ".env", ".DS_Store", ".askpass.sh", "uv.lock",  # uv.lock 由各后端自行生成
})
EXCLUDE_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".log", ".pem", ".key",
    ".bak", ".broken", ".orig", ".rej",   # 备份/损坏文件不该分发
})

# 例外：引擎自身的依赖锁要保留（保证可复现）
KEEP_LOCK = "bin/uv.lock"

# 新工作区必须具备的空目录
STATE_DIRS = (
    "memory/facts", "memory/attempts", "memory/decisions",
    "memory/entities", "memory/journal",
    "tasks/active", "tasks/archive",
    "agents",
    "logs",
    "remote/staging", "remote/logs", "remote/keys",
)


def _excluded(rel_to_root: str) -> bool:
    """按「相对工作区根」的路径判断是否排除，并处理 uv.lock 例外。"""
    if rel_to_root == KEEP_LOCK:
        return False
    p = Path(rel_to_root)
    for part in p.parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
    if p.name in EXCLUDE_FILE_NAMES:
        return True
    if p.name.startswith("id_") and p.suffix in ("", ".pub"):
        return True   # SSH 私钥/公钥
    return p.suffix in EXCLUDE_SUFFIXES


class ScaffoldError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# 随包发出的模板文本
# ══════════════════════════════════════════════════════════════════════════

LAUNCHER = """#!/bin/sh
# ══════════════════════════════════════════════════════════════════════════
# 指挥官启动器
#
#   ./.commander/cmd <子命令> [参数...]
#
# 例：
#   ./.commander/cmd patrol --oneline
#   ./.commander/cmd dispatch scout -p "调研 X"
#
# 存在的意义：让**两种工作区布局共用同一个命令路径**，技能文档因此只需
# 写一套命令，不必为每种布局各写一遍（那是错误的主要来源）。
#
#   扁平布局   工作区根/bin/pyproject.toml        引擎就在上一级
#   内嵌布局   .commander/bin/pyproject.toml      引擎与本脚本同级
#
# 工作区根由 .commander-root 标记文件决定，commander 自己会向上查找。
# ══════════════════════════════════════════════════════════════════════════
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -f "$here/bin/pyproject.toml" ]; then
    engine="$here"                                    # 内嵌布局
elif [ -f "$here/../bin/pyproject.toml" ]; then
    engine=$(CDPATH= cd -- "$here/.." && pwd)         # 扁平布局
else
    printf '找不到指挥官引擎。\\n' >&2
    printf '  在 %s 及其上一级都没找到 bin/pyproject.toml\\n' "$here" >&2
    printf '  若引擎在别处，用 COMMANDER_ROOT 环境变量显式指定工作区根。\\n' >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    printf '需要 uv（本项目全部 Python 调用依赖它，无需 pip）。\\n' >&2
    printf '  安装: curl -LsSf https://astral.sh/uv/install.sh | sh\\n' >&2
    exit 1
fi

exec uv run --project "$engine/bin" commander "$@"
"""

HOSTS_TEMPLATE = """\
# ══════════════════════════════════════════════════════════════════════════
# 远端主机清单
#
# 用途：把重依赖的 agent（crewai / langchain / autogen / hermes）放到算力
# 更强的机器上跑。本地若只有 2 核 2Gi，装不下 crewai 的 136 个包。
#
# 认证优先级：identity_file > password_env。
# 跑 `./.commander/cmd remote bootstrap <host>` 会自动生成专用密钥并装到远端，
# 装完请清空 password_env 并在远端关闭 PasswordAuthentication。
#
# 不需要远端时可以整份留空（hosts 表为空即可），指挥官会自动全部落本地。
# ══════════════════════════════════════════════════════════════════════════

# 复制下面这段并取消注释即可启用：
#
# [hosts.myhost]
# host          = "192.0.2.10"
# user          = "root"
# port          = 22
# identity_file = "remote/keys/commander_ed25519"
# password_env  = "COMMANDER_SSH_PASSWORD"   # bootstrap 完成后清空
# workdir       = "/opt/commander"
# allowed_backends = ["langchain", "crewai", "autogen", "hermes"]
# max_parallel  = 8
"""

# Claude Code 的项目级设置。权限与钩子都指向 ./.commander/cmd，
# 因为两种布局下这个路径都一样。
SETTINGS_TEMPLATE: dict = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {
        "allow": [
            "Bash(./.commander/cmd:*)",
            "Bash(uv run --project .commander/bin:*)",
            "Bash(uv sync --project .commander/bin/backends/*)",
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Read(.commander/**)",
        ],
        "deny": [
            # .env 的精确名 + .env.* 变体（.env.local / .env.production）
            # 官方文档明确推荐两个都挡
            "Read(.commander/bin/.env)",
            "Read(.commander/bin/.env.*)",
            "Read(.commander/remote/keys/**)",
            "Bash(rm -rf /*)",
            "Bash(git push:*)",
        ],
    },
    "hooks": {
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "./.commander/cmd patrol --oneline --no-write 2>/dev/null || true",
                        "timeout": 60,
                        "statusMessage": "指挥官巡检中…",
                    }
                ],
            }
        ]
    },
}


@dataclass
class ScaffoldPlan:
    """先算清楚要做什么，再动手。便于 --dry-run 与向用户解释。"""

    dest: Path
    embed: bool = False
    copies: list[tuple[Path, str]] = field(default_factory=list)
    mkdirs: list[str] = field(default_factory=list)
    skipped_heavy: int = 0
    secrets_kept_out: list[str] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)   # 与已有文件合并而非覆盖
    conflicts: list[str] = field(default_factory=list)

    @property
    def wsdir(self) -> Path:
        """工作区根（内嵌布局下是 dest/.commander）。"""
        return self.dest / EMBED_DIR if self.embed else self.dest

    def summary(self) -> str:
        lines = [
            f"目标: {self.dest}",
            f"布局: {'内嵌（项目 + .commander/）' if self.embed else '扁平（独立工作区）'}",
            f"  工作区根: {self.wsdir}",
            f"  复制 {len(self.copies)} 项",
            f"  新建 {len(self.mkdirs)} 个目录",
            f"  排除 {self.skipped_heavy} 个虚拟环境（换位置会失效，需重新 uv sync）",
        ]
        if self.merges:
            lines.append(
                f"  合并 {len(self.merges)} 个已存在的文件（不覆盖）: "
                + ", ".join(self.merges)
            )
        if self.secrets_kept_out:
            lines.append(
                f"  安全排除 {len(self.secrets_kept_out)} 个密钥文件: "
                + ", ".join(self.secrets_kept_out)
            )
        return "\n".join(lines)


class Scaffolder:
    def __init__(self, ws: Workspace, *, with_memory: bool = False,
                 with_host: bool = False, with_env_secrets: bool = False,
                 embed: bool = False) -> None:
        self.ws = ws
        self.with_memory = with_memory
        self.with_host = with_host
        self.with_env_secrets = with_env_secrets
        self.embed = embed

    # ── 计划 ──────────────────────────────────────────────────────────
    def plan(self, dest: Path) -> ScaffoldPlan:
        dest = dest.expanduser().resolve()
        p = ScaffoldPlan(dest=dest, embed=self.embed)

        # 引擎与状态统一放在 wsdir 下（内嵌布局下即 dest/.commander）

        for f in TEMPLATE_FILES:
            src = self.ws.root / f
            if src.is_file():
                p.copies.append((src, f))

        roots: list[tuple[Path, str]] = [
            (self.ws.root / d, d) for d in TEMPLATE_DIRS
            if (self.ws.root / d).exists()
        ]
        if self.with_memory and self.ws.memory_dir.exists():
            roots.append((self.ws.memory_dir, "memory"))
        if self.with_host and self.ws.remote_dir.exists():
            roots.append((self.ws.remote_dir, "remote"))

        seen: set[str] = {rel for _, rel in p.copies}

        for src_root, _rel_root in roots:
            for item in sorted(src_root.rglob("*")):
                rel_root_str = str(item.relative_to(self.ws.root))

                if item.is_dir() and item.name == ".venv":
                    p.skipped_heavy += 1
                    continue

                # 密钥必须在通用排除规则**之前**单独判定 ——
                # 否则 `.env` 会被通用规则静默吃掉，既不复制也不报告，
                # 用户看到输出里没有它，会以为已经带过去了。这是安全隐患。
                if item.is_file() and item.name == ".env":
                    if self.with_env_secrets and rel_root_str not in seen:
                        p.copies.append((item, rel_root_str))
                    else:
                        p.secrets_kept_out.append(rel_root_str)
                    continue

                if _excluded(rel_root_str):
                    continue

                if item.is_file() and rel_root_str not in seen:
                    p.copies.append((item, rel_root_str))
                    seen.add(rel_root_str)

        keys = self.ws.remote_keys
        if keys.is_dir():
            for k in keys.iterdir():
                # .gitkeep 是占位文件，不是密钥 —— 报成密钥会让人以为漏了什么
                if k.is_file() and k.name != ".gitkeep":
                    p.secrets_kept_out.append(str(k.relative_to(self.ws.root)))

        # .claude/ 是**技能与 subagent**，永远装在项目根（Claude Code 只从那加载），
        # 不进 .commander/。已有同名文件时走合并而非覆盖。
        claude_dst = dest / ".claude"
        p.mkdirs = list(STATE_DIRS)
        for rel in (".claude/skills/commander", ".claude/agents"):
            if (claude_dst / rel[len(".claude/"):]).exists():
                p.merges.append(rel)
        if (claude_dst / "settings.json").is_file():
            p.merges.append(".claude/settings.json")
        for rel in (".claude/loop.md",):
            if (claude_dst / rel[len(".claude/"):]).is_file():
                p.conflicts.append(rel)

        return p

    # ── 执行 ──────────────────────────────────────────────────────────
    def apply(self, dest: Path, *, force: bool = False,
              dry_run: bool = False) -> ScaffoldPlan:
        plan = self.plan(dest)
        dest = plan.dest
        wsdir = plan.wsdir
        _ = wsdir   # 后面要用，这里先留个引用避免误删

        # 只拦「铺到当前工作区自己身上」。
        #
        # ⚠️ 原来这里还有个 `or dest == wsdir` —— 而 wsdir 是从 dest 派生的
        # （扁平布局下直接就是 dest），所以那个子句**恒为真**，
        # 导致扁平布局的 init 一次都跑不了。纯属自伤，删掉。
        if dest == self.ws.root:
            raise ScaffoldError("不能把工作区铺到自己身上")

        if not self.embed and dest.exists() and any(dest.iterdir()) and not force:
            raise ScaffoldError(
                f"目标目录 {dest} 已存在且非空。\n"
                f"  想做独立工作区 → 换一个空目录\n"
                f"  想装进这个已有项目 → 加 --embed（引擎收进 .commander/，不碰你的文件）"
            )

        if dry_run:
            return plan

        # ① 工作区目录骨架
        wsdir.mkdir(parents=True, exist_ok=True)
        for d in plan.mkdirs:
            target = wsdir / d
            target.mkdir(parents=True, exist_ok=True)
            if not any(target.iterdir()):
                (target / ".gitkeep").write_text("", encoding="utf-8")

        # ② 引擎与配置
        for src, rel in plan.copies:
            target = wsdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)

        # ③ 工作区标记 + 启动器
        (wsdir / ".commander-root").write_text(
            "指挥官工作区标记。含此文件即视为工作区根。\n", encoding="utf-8"
        )

        # 启动器**永远**放在 <dest>/.commander/cmd，两种布局都是。
        # 内嵌布局下 .commander/ 就是工作区根，启动器与引擎同级；
        # 扁平布局下 .commander/ 只放这一个脚本，它自己向上找到引擎。
        # 路径统一是 `./.commander/cmd` —— 技能文档因此只需写一套命令。
        launcher_dir = dest / EMBED_DIR
        launcher_dir.mkdir(parents=True, exist_ok=True)
        cmd = launcher_dir / "cmd"
        cmd.write_text(LAUNCHER, encoding="utf-8")
        cmd.chmod(0o755)

        # ④ 远端模板（没带远端配置时）
        hosts = wsdir / "remote" / "hosts.toml"
        if not hosts.is_file():
            hosts.parent.mkdir(parents=True, exist_ok=True)
            hosts.write_text(HOSTS_TEMPLATE, encoding="utf-8")

        # ⑤ .claude/：技能、subagent、loop.md、settings（合并）
        self._install_claude(dest, plan)

        # ⑥ 说明文档
        self._write_next_steps(dest, plan)
        return plan

    # ── .claude/ 安装（含合并）────────────────────────────────────────
    def _install_claude(self, dest: Path, plan: ScaffoldPlan) -> None:
        """把技能与 subagent 装到 <dest>/.claude/。

        已有内容一律**保留**：同名技能目录跳过、settings.json 走合并、
        loop.md 若已存在则写到 .commander/loop.md.example 让用户自己决定。
        """
        src_claude = self.ws.root / ".claude"
        dst_claude = dest / ".claude"
        dst_claude.mkdir(parents=True, exist_ok=True)

        # 技能目录：逐个安装，已存在的不动
        src_skills = src_claude / "skills"
        if src_skills.is_dir():
            for skill in sorted(src_skills.iterdir()):
                if not skill.is_dir():
                    continue
                target = dst_claude / "skills" / skill.name
                if target.exists():
                    continue
                shutil.copytree(skill, target)

        # subagent：同名不覆盖（用户的可能是有意改过的）
        src_agents = src_claude / "agents"
        if src_agents.is_dir():
            (dst_claude / "agents").mkdir(parents=True, exist_ok=True)
            for a in sorted(src_agents.glob("*.md")):
                target = dst_claude / "agents" / a.name
                if not target.exists():
                    shutil.copy2(a, target)

        # loop.md：已有则让位，写到旁边让人自己挑
        src_loop = src_claude / "loop.md"
        if src_loop.is_file():
            target = dst_claude / "loop.md"
            if target.exists():
                shutil.copy2(src_loop, dst_claude / "loop.md.commander")
            else:
                shutil.copy2(src_loop, target)

        # settings.json：合并，不覆盖
        self._merge_settings(dst_claude / "settings.json")

    def _merge_settings(self, path: Path) -> None:
        """把指挥官的权限与钩子并进已有的 settings.json。

        合并规则：权限取并集；钩子按 command 去重后追加。
        绝不覆盖用户已有的任何配置 —— 那是他们的项目，不是我们的。
        """
        existing: dict = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, json.JSONDecodeError):
                # 已有文件坏了不能覆盖 —— 备份后另写，让用户自己处理
                shutil.copy2(path, path.with_suffix(".json.broken"))
                existing = {}

        merged = json.loads(json.dumps(existing))  # 深拷贝
        merged.setdefault("$schema", SETTINGS_TEMPLATE["$schema"])

        perms = merged.setdefault("permissions", {})
        for key in ("allow", "deny"):
            cur = list(perms.get(key) or [])
            for item in SETTINGS_TEMPLATE["permissions"][key]:
                if item not in cur:
                    cur.append(item)
            if cur:
                perms[key] = cur

        hooks = merged.setdefault("hooks", {})
        ss = list(hooks.get("SessionStart") or [])
        have = {
            h.get("command")
            for grp in ss for h in (grp.get("hooks") or [])
        }
        for grp in SETTINGS_TEMPLATE["hooks"]["SessionStart"]:
            for h in grp.get("hooks") or []:
                if h.get("command") not in have:
                    ss.append(grp)
                    break
        hooks["SessionStart"] = ss

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # ── 说明文档 ──────────────────────────────────────────────────────
    def _write_next_steps(self, dest: Path, plan: ScaffoldPlan) -> None:
        wsdir = plan.wsdir
        rel = wsdir.relative_to(dest) if plan.embed else Path(".")
        prefix = "" if not plan.embed else f"{rel}/"
        has_env = (wsdir / "bin" / ".env").is_file()
        backends = self._backends_used()
        sync_lines = "\n".join(
            f"uv sync --project {prefix}bin/backends/{b}" for b in backends
        )

        if has_env:
            env_step = f"# {prefix}bin/.env 已随工作区带来，检查内容并确认权限 600"
        else:
            env_step = (f"cp {prefix}bin/.env.example {prefix}bin/.env && "
                        f"chmod 600 {prefix}bin/.env")

        engine_note = (
            f"引擎与状态收在 `{rel}/` 里，项目自己的文件一个都没动。"
            if plan.embed else "这是一个独立的指挥官工作区。"
        )

        (dest / "COMMANDER.md").write_text(f"""# 指挥官 · 下一步

> 由 `commander init` 于 {time.strftime('%Y-%m-%d %H:%M')} 生成，可删。

{engine_note}

## 0. 怎么调用

所有命令都通过同一个启动器 —— 两种布局下路径都一样：

```bash
./{prefix}cmd <子命令>
```

例：

```bash
./{prefix}cmd doctor                       # 环境自检
./{prefix}cmd patrol --oneline             # 巡检
./{prefix}cmd dispatch scout -p "调研 X"   # 派发
```

## 1. 装依赖

```bash
uv sync --project {prefix}bin            # 引擎（很轻，几秒）
{sync_lines}
```

> 重依赖后端（crewai / autogen / hermes / langchain）体积很大
> （crewai 单后端 700M+），**建议只在远端装**：
> ```bash
> ./{prefix}cmd remote bootstrap
> ./{prefix}cmd remote sync crewai
> ```
>
>
> **`browser_use` 是本地后端，但要额外准备一个浏览器** —— `uv sync`
> 装 browser-use **不带任何浏览器**，它连的是一个已运行的 Chromium：
> ```bash
> uv sync --project {prefix}bin/backends/browser_use
> ./.{prefix}cmd browser up --install-browser   # 没有浏览器就自动下一个
> ```
> 跑 `./{prefix}cmd backend list` 看缺什么 —— 它会真去查浏览器，不只看依赖。

## 2. 配密钥

```bash
{env_step}
# 然后编辑 {prefix}bin/.env 填入你的 API key
```

密钥**只放在 {prefix}bin/.env 里**，不要写进 `{prefix}config/*.toml` ——
后者是要进版本库的。

## 3. 自检

```bash
./{prefix}cmd doctor
./{prefix}cmd dispatch smoke -p "端到端自测"      # 零成本、不联网、不需要密钥
```

`smoke` 用的是 mock 后端，能验证「派发 → 目录隔离 → 留痕 → outbox」
整条链路。**先跑通它再上真模型。**

## 4. 在 Claude Code 里用

```bash
claude          # 从 {dest.name}/ 启动
```

主会话会自动加载 `commander` 技能。直接说「用指挥官把 X 拆了并行做」即可，
或显式 `/commander`。

## 5. 加进 .gitignore

```gitignore
{prefix}.commander/bin/.env
{prefix}.commander/remote/keys/
{prefix}.commander/memory/
{prefix}.commander/tasks/
{prefix}.commander/agents/*/work/
{prefix}.commander/agents/*/logs/
{prefix}.commander/logs/
```

## 安全默认

- `bin/.env` 未被复制过来（除非你用了 `--with-env`，那是你自己的选择）
- `remote/keys/` 下的 SSH 私钥同样没有被复制
- 已有的 `.claude/settings.json` 是**合并**的，你原来的配置一条没丢

## 远端主机

{self._host_section(prefix)}
""", encoding="utf-8")

    def _backends_used(self) -> list[str]:
        try:
            from .config import Config
            cfg = Config(self.ws)
            return sorted({a.backend for a in cfg.agents.values()})
        except Exception:
            return ["mock"]

    def _host_section(self, prefix: str) -> str:
        if self.with_host:
            return ("`remote/hosts.toml` 已随工作区复制过来。\n"
                    "⚠️ 里面可能含你原有的主机地址与凭据配置，交接前请检查。")
        return ("`remote/hosts.toml` 是**空的模板**，需要自己填。填好后跑：\n\n"
                "```bash\n"
                f"./{prefix}cmd remote bootstrap --host <名字>\n"
                "```\n\n"
                "它会装 uv、建目录、生成专用密钥并**实测验证**密钥认证可用。\n"
                "装好后建议在远端关闭 `PasswordAuthentication`。")


# ══════════════════════════════════════════════════════════════════════════
# 技能包 —— 可直接放进 .claude/skills/ 的自举式安装包
# ══════════════════════════════════════════════════════════════════════════
#
# 与 init/dist 的区别：
#   init/dist  产出「已经装好的工作区」，用户要跑 install.sh
#   技能包     产出「技能目录本身」，用户只把它拷进 .claude/skills/，
#              剩下的事由 Claude 自己派 subagent 完成（见 references/bootstrap.md）
#
# 技能包里自带引擎源码（assets/engine），所以是真正自包含的：
# 拷一个目录过去，什么都不用额外准备。

SKILL_NAME = "commander"

# 引擎里不带的东西：venv 换位置就失效；密钥绝不能带；缓存没意义
ENGINE_EXCLUDE_DIRS = frozenset({
    ".venv", "venv", "__pycache__", ".ruff_cache", ".pytest_cache",
    ".mypy_cache", ".git", "node_modules", ".idea", ".vscode",
    ".tox", ".nox", "htmlcov", ".coverage",
})
ENGINE_EXCLUDE_NAMES = frozenset({
    ".env", ".DS_Store", ".askpass.sh", ".netrc", ".pgpass",
})
# 备份/损坏文件不该分发 —— 实测漏过 5 个 agents.toml.*.bak 进包，
# 里面是过期的 agent 配置，用户拿到只会困惑。
_ENGINE_EXCLUDE_SUFFIXES = EXCLUDE_SUFFIXES | frozenset(
    {".bak", ".broken", ".orig", ".rej", ".swp", ".swo", ".tmp", ".p12", ".pfx", ".jks"}
)


def _engine_filter(src_dir: Path):
    """给 shutil.copytree 用的过滤器：决定 bin/ 里哪些进包。"""
    def ignore(directory: str, names: list[str]) -> set[str]:
        drop = set()
        for n in names:
            p = Path(n)
            if n in ENGINE_EXCLUDE_DIRS or n in ENGINE_EXCLUDE_NAMES or p.suffix in _ENGINE_EXCLUDE_SUFFIXES or (n.startswith("id_") and p.suffix in ("", ".pub")) or (n.startswith(".env.") and not n.endswith(".example")) or n.startswith("._") or n.endswith(("~", ".swp", ".swo", ".tmp")):
                drop.add(n)
        return drop
    return ignore


def _pack_sources(ws: Workspace) -> list[Path]:
    """打包**真正取料**的路径。

    ⚠️ 这份清单必须跟着下面 build_skill_pack 的复制代码走 —— 它多拷一处，
    这里就要多一条，否则那道闸就漏了那条路径。
    """
    return [
        ws.root / ".claude" / "skills" / SKILL_NAME,
        # 含 scaffold.py 自己 —— LAUNCHER / SETTINGS_TEMPLATE 是它的常量
        ws.bin_dir,
        ws.config_dir,
        ws.skills_dir,
        ws.root / ".claude" / "agents",
    ]


def _git_dirty(ws: Workspace, paths: list[Path]) -> list[str] | None:
    """这些路径下相对 HEAD 有未提交改动的行。

    不是 git 仓库 / 没有 git 命令 / 路径不在仓库里 → 返回 None，等于不检查。
    这样从一份纯拷贝（没有 .git）里打包也能正常工作。
    只看**已跟踪文件** —— 未跟踪的临时文件不算问题。
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ws.root, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0:
        return None

    repo = Path(top.stdout.strip())
    rel: list[str] = []
    for p in paths:
        try:
            rel.append(str(p.resolve().relative_to(repo)))
        except (ValueError, OSError):
            continue                      # 不在仓库里（罕见），跳过这条
    if not rel:
        return None

    try:
        st = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no", "--", *rel],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if st.returncode != 0:
        return None
    return [ln for ln in st.stdout.splitlines() if ln.strip()]


def build_skill_pack(
    ws: Workspace, dest: Path, *, force: bool = False, allow_dirty: bool = False
) -> Path:
    """把一个自举式技能包组装到 dest/commander/。返回该目录。

    结构：
        commander/
        ├── SKILL.md
        ├── references/            9 份细则（含 bootstrap.md）
        └── assets/                ← 不加载进上下文，只被 subagent 引用
            ├── engine/            引擎源码（bin/ 的内容，不含 venv）
            ├── config/            三份 toml
            ├── skills/            技能库
            ├── agents/            3 个 subagent 定义
            ├── cmd                启动器
            └── settings-fragment.json

    打包前会确认取料的那些路径相对 HEAD 是干净的。这不是洁癖：包是**原样**
    把工作区打进去的，未提交的内容会被无声地发出去 —— 包括"本该在、却被
    意外退回旧版"的东西。宁可停在这里，也别发出一个残缺的包。
    """
    # ⓪ 干净闸 —— 在碰 dest 之前先拦下
    if not allow_dirty:
        dirty = _git_dirty(ws, _pack_sources(ws))
        if dirty:
            raise ScaffoldError(
                "打包源相对 HEAD 有未提交改动，拒绝构建：\n  "
                + "\n  ".join(dirty)
                + "\n\n包是原样把工作区打进去的，未提交的内容会一起发出去。"
                "\n确认要发就先 git commit；只想临时试打，加 --allow-dirty。"
            )

    root = dest / SKILL_NAME
    if root.exists():
        if not force:
            raise ScaffoldError(f"{root} 已存在。加 --force 重建。")
        shutil.rmtree(root)

    src_skill = ws.root / ".claude" / "skills" / SKILL_NAME
    if not (src_skill / "SKILL.md").is_file():
        raise ScaffoldError(f"找不到技能定义: {src_skill / 'SKILL.md'}")

    # ① 技能本体
    (root / "references").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_skill / "SKILL.md", root / "SKILL.md")
    for f in sorted((src_skill / "references").glob("*.md")):
        shutil.copy2(f, root / "references" / f.name)

    # ② 引擎源码（这是自包含的关键）
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ws.bin_dir, assets / "engine", ignore=_engine_filter(ws.bin_dir))

    # ③ 配置与技能库
    shutil.copytree(ws.config_dir, assets / "config")
    if ws.skills_dir.is_dir():
        shutil.copytree(ws.skills_dir, assets / "skills")

    # ④ subagent 定义
    src_agents = ws.root / ".claude" / "agents"
    if src_agents.is_dir():
        shutil.copytree(src_agents, assets / "agents")

    # ⑤ 启动器与设置片段
    (assets / "cmd").write_text(LAUNCHER, encoding="utf-8")
    (assets / "cmd").chmod(0o755)
    (assets / "settings-fragment.json").write_text(
        json.dumps(SETTINGS_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # ⑥ 一份给用户看的说明
    (dest / "README.txt").write_text(
        f"""指挥官技能包
{'=' * 60}

安装（一条命令）：

    tar xzf commander-skill.tar.gz -C <你的项目>/.claude/skills/

或者直接拷贝 commander/ 目录到 <你的项目>/.claude/skills/ 下。

然后：

    1. cd <你的项目> && claude
    2. 说一句「用指挥官帮我做 X」
       —— Claude 会发现引擎没装，自动派 subagent 完成安装
    3. 按它给的提示填 API 密钥（编辑 .commander/bin/.env）

不需要跑任何安装脚本。装什么、怎么装，技能里写清楚了，
Claude 自己会做，并会给你一份逐项验证过的检查清单。

包里有什么
----------
  commander/SKILL.md          技能定义（Claude 读这个）
  commander/references/       细则（按需加载）
  commander/assets/           引擎源码与配置（不加载进上下文）

  assets/ 里是完整引擎，所以这个包是自包含的 ——
  拷过去就能装，不需要先有别的什么东西。

安全
----
  包里不含任何密钥、不含虚拟环境。
  .env 是空模板，需要你自己填。
""",
        encoding="utf-8",
    )
    return root
