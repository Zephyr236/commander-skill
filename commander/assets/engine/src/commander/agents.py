"""Agent 的按需定制 —— 指挥官造下属。

用户需求原文：「指挥官就可以按需定制完成任务的agent」

编制表 `config/agents.toml` 里那 8 个是常备部队。但真实任务千变万化，
指挥官需要能**当场造一个**符合当前需要的下属：换后端、换模型、换技能、
写一段专门的职责说明。

两个入口：
    agent-new --template <名>     从模板起手，再覆盖几个字段
    agent-new --backend ... ...   完全自定义（指挥官自己发挥）

━━ 为什么用文本追加而不是 TOML 重写 ━━
`agents.toml` 里有大量手写注释和说明。用 tomli_w 重写整份文件会**把所有注释
抹掉**，而那份文件的注释正是「怎么配 agent」的文档。所以这里只往文件末尾
追加一段格式化的 TOML —— 追加位置在最后，不会打断任何已有表。
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import BACKENDS, AgentConfig, Config
from .workspace import Workspace


class AgentError(RuntimeError):
    pass


@dataclass
class AgentTemplate:
    """一个可复用的 agent 原型。"""

    name: str = ""
    description: str = ""
    backend: str = "claude"
    model: str = "flash"
    target: str = "auto"
    skills: list[str] = field(default_factory=list)
    max_tokens: int = 8192
    timeout: int = 1800
    max_turns: int = 20
    options: dict = field(default_factory=dict)


def load_templates(ws: Workspace) -> dict[str, AgentTemplate]:
    """读 config/agent-templates.toml。没有就返回空表（不报错）。"""
    f = ws.config_dir / "agent-templates.toml"
    if not f.is_file():
        return {}
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentError(f"读不了 {f.name}: {exc}") from exc

    out: dict[str, AgentTemplate] = {}
    for name, spec in (data.get("templates") or {}).items():
        if not isinstance(spec, dict):
            continue
        known = {k: v for k, v in spec.items()
                 if k in AgentTemplate.__dataclass_fields__ and k != "name"}
        out[name] = AgentTemplate(name=name, **known)
    return out


# ══════════════════════════════════════════════════════════════════════════
# TOML 文本处理
# ══════════════════════════════════════════════════════════════════════════

_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _find_table_span(text: str, table: str) -> tuple[int, int] | None:
    """找 `[table]` 这一段在文本里的起止行号（含表头，不含下一张表）。

    返回 (start, end)，end 是下一张表表头所在行（或行数）。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = _TABLE_RE.match(ln)
        if not m:
            continue
        if m.group(1).strip() == table:
            start = i
            continue
        if start is not None:
            return start, i          # 遇到下一张表就收尾
    if start is not None:
        return start, len(lines)
    return None


def _toml_str(s: str) -> str:
    """把一个字符串渲染成 TOML 值。多行用三引号，更接近手写风格。"""
    s = s.strip()
    if "\n" in s:
        # 三引号里首尾各留一个换行，这是 TOML 的惯例写法
        return f'"""\n{s}\n"""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_agent(agent_id: str, a: AgentConfig) -> str:
    skills = ", ".join(f'"{s}"' for s in a.skills)
    return f"""
# 由 `commander agent-new` 于生成，可直接手工编辑。
[agents.{agent_id}]
backend     = {_toml_str(a.backend)}
model       = {_toml_str(a.model)}
target      = {_toml_str(a.target)}
skills      = [{skills}]
max_tokens  = {a.max_tokens}
timeout     = {a.timeout}
max_turns   = {a.max_turns}
description = {_toml_str(a.description)}
"""


# ══════════════════════════════════════════════════════════════════════════
# 创建 / 删除
# ══════════════════════════════════════════════════════════════════════════

def validate(ws: Workspace, cfg: Config, a: AgentConfig) -> list[str]:
    """检查这个 agent 配得对不对。返回警告列表；有硬错误就抛。"""
    from .guard import Guard

    Guard(ws, cfg.policy.guard).assert_valid_agent_id(a.id)

    if a.backend not in BACKENDS:
        raise AgentError(
            f"未知后端 {a.backend!r}。可用的：{', '.join(sorted(BACKENDS))}"
        )
    if a.model not in cfg.models:
        raise AgentError(
            f"未登记的模型 {a.model!r}。models.toml 里有的："
            f"{', '.join(sorted(cfg.models))}"
        )
    if a.target not in ("local", "remote", "auto"):
        raise AgentError(f"target 只能是 local/remote/auto，收到 {a.target!r}")

    warns: list[str] = []
    from .skills import SkillLibrary
    have = set(SkillLibrary(ws).names())
    missing = [s for s in a.skills if s not in have]
    if missing:
        warns.append(
            f"技能 {', '.join(missing)} 不存在，派发时会被静默跳过。"
            f"要新建：./.commander/cmd skill-new {missing[0]}"
        )

    # 模型与后端的相容性（和 router._compatible 同一套规则）
    m = cfg.models[a.model]
    if a.backend != "mock" and m.provider == "local":
        raise AgentError(
            f"模型 {a.model!r} 是本地的（provider={m.provider}），"
            f"不能配给真后端 {a.backend!r}。"
        )
    if not m.supports_tools and a.backend in ("langchain", "crewai", "autogen"):
        warns.append(f"模型 {a.model} 不支持工具调用，{a.backend} 后端可能跑不动")

    return warns


def create(
    ws: Workspace,
    cfg: Config,
    agent_id: str,
    *,
    template: str | None = None,
    backend: str | None = None,
    model: str | None = None,
    target: str | None = None,
    skills: list[str] | None = None,
    description: str | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    max_turns: int | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[AgentConfig, list[str]]:
    """造一个 agent 并写进 agents.toml。返回 (配置, 警告)。"""
    if agent_id in cfg.agents and not force:
        raise AgentError(
            f"agent {agent_id!r} 已存在。换一个 id，或用 --force 覆盖。"
        )

    # 起点：模板 → 已有 agent（派生）→ 空
    if template:
        templates = load_templates(ws)
        if template not in templates:
            raise AgentError(
                f"没有模板 {template!r}。可用的："
                f"{', '.join(sorted(templates)) or '(模板文件不存在)'}"
            )
        t = templates[template]
        base = AgentConfig(
            id=agent_id, backend=t.backend, model=t.model, target=t.target,
            skills=list(t.skills), max_tokens=t.max_tokens, timeout=t.timeout,
            max_turns=t.max_turns, description=t.description.strip(),
            options=dict(t.options),
        )
    else:
        base = AgentConfig(
            id=agent_id, backend=backend or "claude", model=model or "flash",
            description=(description or "").strip(),
        )

    # 逐个覆盖 —— 只有显式给了的才覆盖，没给就沿用模板
    overrides: dict = {}
    if backend:
        overrides["backend"] = backend
    if model:
        overrides["model"] = model
    if target:
        overrides["target"] = target
    if skills is not None:
        overrides["skills"] = list(skills)
    if description:
        overrides["description"] = description.strip()
    if max_tokens:
        overrides["max_tokens"] = max_tokens
    if timeout:
        overrides["timeout"] = timeout
    if max_turns:
        overrides["max_turns"] = max_turns

    # ⚠️ AgentConfig 是 pydantic BaseModel，不是 dataclass ——
    # 用 dataclasses.replace() 会抛 TypeError。pydantic 对应的是 model_copy。
    agent = base.model_copy(update=overrides) if overrides else base

    warns = validate(ws, cfg, agent)

    if dry_run:
        return agent, warns

    path = ws.agents_toml
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    if agent_id in cfg.agents:
        # --force 覆盖：先把旧的那一段挖掉
        span = _find_table_span(text, f"agents.{agent_id}")
        if span:
            lines = text.splitlines()
            text = "\n".join(lines[:span[0]] + lines[span[1]:])

    if text and not text.endswith("\n"):
        text += "\n"
    text += _render_agent(agent_id, agent)

    path.write_text(text, encoding="utf-8")

    # 顺手生成 BRIEF.md，让新下属一诞生就有边界说明书。
    # 生成失败不该让创建本身失败 —— 配置已经写进去了，BRIEF 可以后补。
    import contextlib
    with contextlib.suppress(Exception):
        write_brief(ws, Config(ws), agent_id)

    return agent, warns


def remove(ws: Workspace, agent_id: str, *, force: bool = False) -> str:
    """从 agents.toml 里删掉一个 agent。返回被删掉的 TOML 片段。

    **不删 agents/<id>/ 目录** —— 那里有它的日志、产物、BRIEF，
    是审计记录。要清得自己动手。
    """
    from .config import Config
    cfg = Config(ws)
    if agent_id not in cfg.agents:
        raise AgentError(f"没有 agent {agent_id!r}")

    if not force:
        raise AgentError(
            f"删除 {agent_id!r} 需要 --force。\n"
            f"  它的工作目录 agents/{agent_id}/ 不会被删（那里是审计记录）。\n"
            f"  想临时停用而不是删除：在 agents.toml 里给它加 enabled = false"
        )

    path = ws.agents_toml
    text = path.read_text(encoding="utf-8")
    span = _find_table_span(text, f"agents.{agent_id}")
    if span is None:
        raise AgentError(f"agents.toml 里找不到 [agents.{agent_id}] 段")

    lines = text.splitlines()
    removed = "\n".join(lines[span[0]:span[1]])
    rest = "\n".join(lines[:span[0]] + lines[span[1]:])
    path.write_text(rest.rstrip("\n") + "\n", encoding="utf-8")

    # 备份一次被删的内容，后悔了还能翻出来
    rec = ws.logs_dir / "removed-agents.toml"
    rec.parent.mkdir(parents=True, exist_ok=True)
    with rec.open("a", encoding="utf-8") as f:
        f.write(f"\n# ── {agent_id} 于 {_now()} 被移除 ──\n{removed}\n")

    return removed


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def backup_agents_toml(ws: Workspace) -> Path | None:
    """改 agents.toml 之前留个备份。"""
    p = ws.agents_toml
    if not p.is_file():
        return None
    b = p.with_suffix(f".toml.{_now().replace(':', '').replace(' ', '-').replace('-', '')[:14]}.bak")
    shutil.copy2(p, b)
    return b


# ══════════════════════════════════════════════════════════════════════════
# BRIEF.md —— 下属的职责与边界说明书
# ══════════════════════════════════════════════════════════════════════════

BRIEF_TEMPLATE = """# {aid}

> 由 `commander brief` 从 `config/agents.toml` 生成。改配置后重跑，不要手改本文件。

## 你是谁

{description}

## 工作参数

| 项 | 值 |
|---|---|
| 后端 | `{backend}` |
| 模型 | `{model}`{model_name} |
| 落点 | {where} |
| 默认技能 | {skills} |
| max_tokens | {max_tokens} |
| 超时 | {timeout}s |
| 最大轮数 | {max_turns} |

## 你的目录

**你只在下面这个目录里工作**，这是唯一允许你创建或修改文件的地方：

```
{workdir}
```

- 相对路径天然落在该目录内（进程 cwd 被钉死在此）
- 绝对路径也写不出去 —— 你运行在只读挂载的沙箱里，
  `memory/`、别的 agent 的目录、`.claude/`、`.git/` 全部返回 `Read-only file system`
- 你的密钥环境里读不到 `bin/.env`（已被遮蔽）

## 你不能做的事

| 禁止 | 说明 |
|---|---|
| 写 `memory/` | 记忆由指挥官维护。把结论交回去，不要自己写 |
| 写别的 agent 目录 | 需要对方数据就请指挥官协调 |
| 写 `config/` | 配置变更属于用户决策 |
| 绕过 stdout 交结果 | 结论必须作为最终回复给出，不要只写进文件 |

## 怎么交结果

你的**最终回复文本**就是交付物 —— 它会进 outbox，是指挥官唯一会读的东西。

产物文件（如果有）写在当前目录，路径会被自动记录。

## 边界提醒

不确定某件事该不该做时，**不要做**，在回复里说明你不确定什么。
指挥官会根据你的说明决定下一步。被拒绝的写入会记进 `logs/guard-violations.jsonl`，
那对谁都不是好事。
"""


def render_brief(ws: Workspace, cfg: Config, agent_id: str) -> str:
    """渲染某个 agent 的 BRIEF.md 内容。"""
    from .backends import _find_chrome
    from .config import HEAVY_BACKENDS, NEEDS_BROWSER

    a = cfg.agent(agent_id)
    if a.target == "auto":
        if a.backend in NEEDS_BROWSER:
            # 需要浏览器的后端：落点由「哪台机器有浏览器」决定。
            # 早先这里只判 HEAVY_BACKENDS，browser_use 会得到「本地（轻量）」——
            # 而真实落点可能是远端，BRIEF 就写错了。
            where = ("本地（本机有浏览器）" if _find_chrome()
                     else "远端（本机无浏览器，落有浏览器的那台）")
        else:
            where = "远端（重依赖）" if a.backend in HEAVY_BACKENDS else "本地（轻量）"
    else:
        where = a.target
    m = cfg.models.get(a.model)

    return BRIEF_TEMPLATE.format(
        aid=agent_id,
        description=a.description.strip() or "(未填写职责说明)",
        backend=a.backend,
        model=a.model,
        model_name=f" ({m.model})" if m else "",
        where=where,
        skills=", ".join(a.skills) or "(无)",
        max_tokens=a.max_tokens,
        timeout=a.timeout,
        max_turns=a.max_turns,
        workdir=ws.agent_workdir(agent_id),
    )


def write_brief(ws: Workspace, cfg: Config, agent_id: str, *,
                force: bool = False) -> bool:
    """给一个 agent 写 BRIEF.md。返回是否真的写了（跳过时返回 False）。"""
    target = ws.agent_brief(agent_id)
    if target.is_file() and not force:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_brief(ws, cfg, agent_id), encoding="utf-8")
    return True
