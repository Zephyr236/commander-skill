"""指挥官的命令行界面。

指挥官（Claude Code 主会话）通过 `uv run commander <子命令>` 使用一切能力。
每个子命令对应指挥工作的一个动作，没有隐藏状态 —— 状态全在文件里。
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import Config, ConfigError
from .dispatch import Commander, DispatchRequest
from .guard import Guard, GuardViolation
from .memory import KINDS, Memory, MemoryStore
from .patrol import Patroller
from .record import list_outbox, mark_collected
from .tasks import TaskRegistry
from .workspace import WorkspaceNotFound

app = typer.Typer(
    name="commander",
    help="指挥官派发层 —— 把任务交给不同 SDK 驱动的下属 agent，本地或远端执行。",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

# 子命令组
mem_app = typer.Typer(help="长期记忆的读写与检索", no_args_is_help=True)
task_app = typer.Typer(help="任务管理", no_args_is_help=True)
outbox_app = typer.Typer(help="收集下属的产出", no_args_is_help=True)
remote_app = typer.Typer(help="SSH 远端执行", no_args_is_help=True)
be_app = typer.Typer(help="SDK 后端管理", no_args_is_help=True)
app.add_typer(mem_app, name="memory")
app.add_typer(task_app, name="task")
app.add_typer(outbox_app, name="outbox")
app.add_typer(remote_app, name="remote")
app.add_typer(be_app, name="backend")
cfg_app = typer.Typer(help="凭据配置：API 密钥、base url、SSH 主机", no_args_is_help=True)
app.add_typer(cfg_app, name="config")



# ══════════════════════════════════════════════════════════════════════════
# 公共
# ══════════════════════════════════════════════════════════════════════════
def _cfg() -> Config:
    try:
        return Config()
    except WorkspaceNotFound as exc:
        err.print(f"[red]找不到工作区：{exc}[/]")
        raise typer.Exit(2) from None
    except ConfigError as exc:
        err.print(f"[red]配置错误：{exc}[/]")
        raise typer.Exit(2) from None


def _read_text_arg(inline: str | None, from_file: Path | None) -> str:
    if from_file:
        try:
            return from_file.read_text(encoding="utf-8")
        except OSError as exc:
            err.print(f"[red]读不到 {from_file}: {exc}[/]")
            raise typer.Exit(2) from None
    return inline or ""


# ══════════════════════════════════════════════════════════════════════════
# doctor —— 环境自检
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def doctor(
    remote: bool = typer.Option(False, "--remote", help="顺带检测远端主机连通性"),
) -> None:
    """检查工作区是否可用。出问题时先跑这个。"""
    cfg = _cfg()
    ws = cfg.ws

    t = Table(title="指挥官环境自检", show_header=True, header_style="bold")
    t.add_column("检查项")
    t.add_column("状态")
    t.add_column("说明")

    def row(name: str, ok: bool | str, detail: str) -> None:
        mark = {"ok": "[green]✓[/]", "warn": "[yellow]⚠[/]", "off": "[dim]—[/]"}.get(
            ok if isinstance(ok, str) else ("ok" if ok else "warn"), "?"
        )
        t.add_row(name, mark, detail)

    row("工作区根", True, str(ws.root))
    row("配置文件", ws.models_toml.is_file() and ws.agents_toml.is_file()
        and ws.policy_toml.is_file(), "models/agents/policy.toml")

    for name, status, detail in cfg.doctor():
        t.add_row(name, {"ok": "[green]✓[/]", "warn": "[yellow]⚠[/]",
                         "off": "[dim]—[/]"}.get(status, "?"), detail)

    avail = cfg.available_models()
    row("可用模型", bool(avail), ", ".join(f"{m.alias}({m.cost_tier})" for m in avail)
        or "[red]无 —— 检查 bin/.env[/]")

    row("已编制 agent", bool(cfg.agents),
        ", ".join(sorted(cfg.agents)) or "无")

    # 后端就绪度 —— 与 patrol 共用实现，重后端查远端而非本地
    from .backends import backend_readiness
    for r in backend_readiness(cfg, check_remote=True):
        loc = f"[{r['where']}] " if r["where"] == "remote" else ""
        row(f"后端 {r['backend']}", r["ok"], loc + r["detail"])

    if remote:
        from .ssh_runner import RemoteRunner
        rr = RemoteRunner(cfg, Guard(ws, cfg.policy.guard))
        for h in cfg.hosts.values():
            ok, detail = rr.check(h)
            row(f"远端 {h.name}", ok, f"{h.host} — {detail}")

    console.print(t)

    st = MemoryStore(ws).stats()
    console.print(
        f"\n记忆: {sum(st.values())} 条 "
        f"({' '.join(f'{k}={v}' for k, v in st.items())})"
    )
    console.print(f"任务: {len(TaskRegistry(ws).list_active())} 个活动")


# ══════════════════════════════════════════════════════════════════════════
# dispatch —— 派发一次
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def dispatch(
    agent: str = typer.Argument(..., help="agent_id，见 commander agents"),
    prompt: str | None = typer.Option(None, "--prompt", "-p", help="任务指令"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file", help="从文件读指令"),
    task: str | None = typer.Option(None, "--task", "-t", help="归属的 task_id"),
    model: str | None = typer.Option(None, "--model", "-m", help="覆盖模型别名"),
    target: str | None = typer.Option(None, "--target", help="local | remote"),
    host: str | None = typer.Option(None, "--host", help="指定远端主机名"),
    skills: str | None = typer.Option(None, "--skills", help="逗号分隔，覆盖默认技能"),
    extra_skills: str | None = typer.Option(None, "--extra-skills", help="在默认技能上追加"),
    system: str = typer.Option("", "--system", help="附加 system prompt"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="覆盖 max_tokens"),
    timeout: int | None = typer.Option(None, "--timeout", help="秒"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="只输出结果，不显示过程"),
    json_out: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """派发一个子任务给某个下属 agent。"""
    cfg = _cfg()
    text = _read_text_arg(prompt, prompt_file)
    if not text.strip():
        err.print("[red]必须提供 --prompt 或 --prompt-file[/]")
        raise typer.Exit(2) from None

    req = DispatchRequest(
        agent_id=agent, prompt=text, task_id=task, model=model,
        target=target, host=host,
        skills=skills.split(",") if skills else None,
        extra_skills=extra_skills.split(",") if extra_skills else [],
        system_prompt=system, max_tokens=max_tokens, timeout=timeout,
    )

    commander = Commander(cfg)

    def on_event(ev) -> None:
        if quiet:
            return
        if ev.type == "tool_call":
            err.print(f"    [cyan]⚙ {ev.data.get('name')}[/]")
        elif ev.type == "thought" and ev.data.get("text"):
            err.print(f"    [dim]💭 {str(ev.data['text'])[:120]}[/]")

    # 路由发生在构造 RunSpec 之前，所以它抛的错没有 recorder 兜着。
    # 不接的话用户看到的是原始 traceback —— 而这类错误（缺密钥、没编制）
    # 恰恰是最常见的，必须给人话。
    from .router import RoutingError
    try:
        result, route, outbox = commander.dispatch(
            req, on_event=on_event, quiet=quiet
        )
    except RoutingError as exc:
        err.print(Panel(
            str(exc),
            title="[red]无法派发[/]",
            subtitle=f"agent: {agent}",
            border_style="red",
        ))
        raise typer.Exit(2) from None
    except ConfigError as exc:
        err.print(f"[red]配置错误：{exc}[/]")
        raise typer.Exit(2) from None

    if json_out:
        console.print_json(json.dumps({
            "ok": result.ok, "task_id": task or "(auto)", "agent": agent,
            "route": route.explain(), "outbox": outbox,
            "result": result.model_dump(),
        }, ensure_ascii=False))
    else:
        color = "green" if result.ok else "red"
        console.print(Panel(
            result.text or (result.error or "(无输出)"),
            title=f"[{color}]{'成功' if result.ok else '失败'}[/] · "
                  f"{route.agent.id}/{result.model} · {result.duration_s:.1f}s",
            subtitle=f"记录: {outbox}",
            border_style=color,
        ))

    raise typer.Exit(0 if result.ok else 1)


# ══════════════════════════════════════════════════════════════════════════
# fanout —— 多角度并行派发
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def fanout(
    task: str = typer.Option(..., "--task", "-t", help="task_id"),
    angles: str = typer.Option(..., "--angles", help="JSON: [{agent, prompt}, ...]"),
    parallel: int | None = typer.Option(None, "--parallel", help="并发上限"),
    models: str | None = typer.Option(
        None, "--models",
        help="逗号分隔的模型别名，按角度轮转，如 flash,pro"),
    spread_models: bool = typer.Option(
        False, "--spread-models",
        help="自动在所有可用模型间轮转（按成本从低到高），让每个角度换一个脑子"),
    quiet: bool = typer.Option(True, "--quiet/--verbose", "-q"),
) -> None:
    """把一个任务拆成多个角度，并行派给不同 agent、不同模型。

    用户需求原文：「指挥官agent主要负责派发一个个子任务，分发多个角度，
    交给不同的agent，交给不同的大模型去完成」

    angles 是一个 JSON 数组，每项 {"agent": "...", "prompt": "...", "model": "..."}。

    ━━ 为什么要换模型 ━━
    同一个模型看三个角度，盲区是一样的 —— 它对某类错误有固定的"看不见"，
    换个 prompt 也躲不掉。换模型才会换盲区：不同的训练数据、不同的推理倾向、
    不同的失败模式。

    `--spread-models` 按成本从低到高排序后轮转，所以便宜的先上；
    `--models flash,pro` 可以精确控制用哪几个。
    角度里显式写了 "model" 的**不会被覆盖** —— 显式意图优先。
    """
    cfg = _cfg()
    try:
        spec = json.loads(angles)
    except json.JSONDecodeError as exc:
        err.print(f"[red]--angles 不是合法 JSON: {exc}[/]")
        raise typer.Exit(2) from None
    if not isinstance(spec, list) or not spec:
        err.print("[red]--angles 应是非空数组[/]")
        raise typer.Exit(2) from None

    # ── 决定模型轮转池 ────────────────────────────────────────────────
    pool: list[str] = []
    if models:
        pool = [m.strip() for m in models.split(",") if m.strip()]
    elif spread_models:
        # 只轮转"真模型"：mock 是零成本自测用的，混进分析里没有意义。
        # 按成本从低到高 —— 便宜的先上，贵的不浪费在简单角度上。
        order = {"cheap": 0, "free": 0, "standard": 1, "premium": 2}
        avail = [m for m in cfg.available_models() if m.provider != "local"]
        avail.sort(key=lambda m: (order.get(m.cost_tier, 9), m.alias))
        pool = [m.alias for m in avail]

    unknown = [m for m in pool if m not in cfg.models]
    if unknown:
        err.print(f"[red]未登记的模型: {', '.join(unknown)}。"
                  f"有的：{', '.join(sorted(cfg.models))}[/]")
        raise typer.Exit(2) from None

    # 轮转赋值：显式写了 model 的角度不动
    assigned = 0
    for i, item in enumerate(spec):
        if isinstance(item, dict) and not item.get("model") and pool:
            item["model"] = pool[i % len(pool)]
            assigned += 1

    limit = parallel or cfg.policy.concurrency.remote
    commander = Commander(cfg)
    tr = TaskRegistry(cfg.ws)

    # 把角度登记到任务档案里（带模型，方便事后对比哪个模型看出了什么）
    if tr.load(task):
        tr.update(task, angles=[
            f"{s.get('prompt','')[:90]}  [{s.get('model') or '默认'}]" for s in spec
        ])

    console.print(f"[bold]并行派发 {len(spec)} 个角度[/] (并发上限 {limit})")
    if assigned:
        console.print(f"  模型轮转池: {', '.join(pool)}   "
                      f"[dim]（{assigned} 个角度自动分配）[/]")

    results: list[tuple[str, object, str]] = []

    def one(item: dict):
        req = DispatchRequest(
            agent_id=item["agent"], prompt=item["prompt"], task_id=task,
            model=item.get("model"), target=item.get("target"),
            extra_skills=item.get("skills", []),
            max_tokens=item.get("max_tokens"),
        )
        r, _route, outbox = commander.dispatch(req, quiet=True)
        return item["agent"], r, outbox

    with ThreadPoolExecutor(max_workers=limit) as pool_exec:
        futs = {pool_exec.submit(one, s): s for s in spec}
        for fut in as_completed(futs):
            item = futs[fut]
            try:
                agent_id, r, outbox = fut.result()
                results.append((agent_id, r, outbox))
                mark = "[green]✓[/]" if r.ok else "[red]✗[/]"
                # 把模型标出来 —— 多模型派发的价值就在于能对比
                # "同一个问题，不同脑子分别看出了什么"
                console.print(
                    f"  {mark} {agent_id:14} [dim]{r.model or item.get('model') or '默认'}[/]"
                    f" → {outbox}"
                )
            except Exception as exc:
                console.print(f"  [red]✗ {item.get('agent')} 异常: {exc}[/]")

    ok = sum(1 for _, r, _ in results if r.ok)
    console.print(f"\n[bold]完成 {ok}/{len(spec)}[/] · 结果在 agents/*/outbox/")

    # 多模型时给一句提示 —— 下一步该做的是**对比**，不是简单汇总
    used = {r.model for _, r, _ in results if r.model}
    if len(used) > 1:
        console.print(
            f"[dim]用了 {len(used)} 个模型：{', '.join(sorted(used))}。"
            f"比较它们的结论 —— 分歧点往往就是任务里真正难的地方。[/]"
        )
    raise typer.Exit(0 if ok else 1)


# ══════════════════════════════════════════════════════════════════════════
# agents / models
# ══════════════════════════════════════════════════════════════════════════
@app.command("agents")
def list_agents() -> None:
    """列出编制内的下属 agent。"""
    cfg = _cfg()
    t = Table(title="Agent 编制", show_header=True, header_style="bold")
    for c in ("id", "后端", "模型", "落点", "技能", "职责"):
        t.add_column(c)

    for a in cfg.agents.values():
        where = {"local": "本地", "remote": "远端", "auto": "自动"}.get(a.target, a.target)
        if a.target == "auto":
            from .config import HEAVY_BACKENDS
            where = "远端(重依赖)" if a.backend in HEAVY_BACKENDS else "本地(轻量)"
        desc = " ".join(a.description.split())[:60]
        t.add_row(
            a.id, a.backend, a.model, where,
            ",".join(a.skills) or "-", desc,
            style="" if a.enabled else "dim",
        )
    console.print(t)
    console.print("\n派发：`uv run commander dispatch <agent> -p '<任务>'`")


@app.command()
def init(
    dest: Path = typer.Argument(..., help="目标目录（独立工作区，或要装进的项目）"),
    embed: bool = typer.Option(False, "--embed",
                               help="装进一个已有项目：引擎与状态收进 .commander/，不碰项目自己的文件"),
    force: bool = typer.Option(False, "--force", help="目标非空时也铺（只对扁平布局有意义）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不动手"),
    with_memory: bool = typer.Option(False, "--with-memory",
                                     help="连记忆库一起复制（默认不带，新工作区从零开始）"),
    with_host: bool = typer.Option(False, "--with-host",
                                   help="连远端主机配置一起复制（默认不带，避免带走别人的主机）"),
    with_env: bool = typer.Option(False, "--with-env",
                                  help="⚠️ 连 bin/.env 一起复制（含明文密钥，交接给别人时绝不要开）"),
) -> None:
    """把指挥官铺到一个新目录，或装进一个已有项目。

    **独立工作区**（默认）：目标目录成为一个自包含的指挥官工作区。

    **装进已有项目**（`--embed`）：引擎与状态全部收进 `<项目>/.commander/`，
    项目自己的 `src/` `config/` `logs/` `skills/` 一个都不动；
    技能与 subagent 装到 `<项目>/.claude/`（与已有内容合并，不覆盖）。

    两种布局下命令都通过同一个启动器调用：`./.commander/cmd <子命令>`。

    复制引擎源码 + 配置 + 技能库 + .claude，但**不带** venv、运行状态和密钥 ——
    见输出里的"安全排除"。
    """
    cfg = _cfg()
    from .scaffold import Scaffolder, ScaffoldError

    sc = Scaffolder(cfg.ws, with_memory=with_memory, with_host=with_host,
                    with_env_secrets=with_env, embed=embed)

    try:
        plan = sc.apply(dest, force=force, dry_run=dry_run)
    except ScaffoldError as exc:
        err.print(f"[red]✗ {exc}[/]")
        raise typer.Exit(2) from None

    console.print(plan.summary())

    if dry_run:
        console.print("\n[dim]--dry-run，未做任何改动。[/]")
        if plan.conflicts:
            console.print("[yellow]会被保留（不覆盖）的已有文件:[/]")
            for c in plan.conflicts:
                console.print(f"  [yellow]{c}[/]")
        if plan.secrets_kept_out:
            console.print("[dim]被安全排除的密钥文件:[/]")
            for s in plan.secrets_kept_out[:8]:
                console.print(f"  [dim]{s}[/]")
        return

    if with_env:
        console.print(
            "\n[yellow]⚠️  已把 bin/.env（含明文密钥）复制过去。[/]\n"
            "   交接给别人前请务必删除，或改用 --with-env 之外的默认行为。"
        )
    if with_host:
        console.print(
            "[yellow]⚠️  已复制远端主机配置 remote/hosts.toml，交接前请检查内容。[/]"
        )
    if plan.merges:
        console.print(
            f"[green]·[/] 已与现有内容合并（未覆盖）: {', '.join(plan.merges)}"
        )

    console.print(f"\n[green]✓[/] 已就绪: [bold]{plan.dest}[/]")
    console.print(f"  工作区根: {plan.wsdir}")
    console.print(f"  下一步看 [bold]{plan.dest / 'COMMANDER.md'}[/]")
    console.print("\n  最小验证（零成本、不需要密钥）：")
    console.print(f"    cd {plan.dest}")
    console.print("    ./.commander/cmd doctor        # 首次会自动提示装依赖")


@app.command("skill-pack")
def skill_pack(
    dest: Path = typer.Argument(Path("dist"), help="输出目录"),
    force: bool = typer.Option(False, "--force", help="目标已存在时重建"),
    allow_dirty: bool = typer.Option(
        False, "--allow-dirty", help="工作区有未提交改动也照打（默认拒绝）"
    ),
) -> None:
    """打出一个**自举式技能包** —— 拷进 .claude/skills/ 就能用。

    包里自带引擎源码（assets/），所以用户不需要任何安装脚本：
    把目录拷过去，Claude 首次使用时自己派 subagent 完成安装。

    默认要求打包源相对 HEAD 干净 —— 包是原样打工作区的，未提交的改动
    会被无声发出去。要临时试打用 `--allow-dirty`。
    """
    cfg = _cfg()
    from .scaffold import ScaffoldError, build_skill_pack

    try:
        root = build_skill_pack(
            cfg.ws, dest, force=force, allow_dirty=allow_dirty
        )
    except ScaffoldError as exc:
        err.print(f"[red]✗ {exc}[/]")
        raise typer.Exit(2) from None

    n_files = sum(1 for _ in root.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())

    console.print(f"[green]✓[/] 技能包已生成: [bold]{root}[/]")
    console.print(f"  {n_files} 个文件，{size/1024:.0f}K")
    console.print("\n  装到项目里：")
    console.print("    mkdir -p <项目>/.claude/skills")
    console.print(f"    cp -r {root} <项目>/.claude/skills/")
    console.print("\n  然后 cd <项目> && claude，说「用指挥官帮我做 X」")
    console.print("  Claude 会自己发现引擎没装并完成安装。")


# ── agent 的按需定制 ──────────────────────────────────────────────────────
# 用户需求：「指挥官就可以按需定制完成任务的agent」
# 编制表里那 8 个是常备部队；这三个命令让指挥官能当场造一个新的下属。

@app.command("agent-templates")
def list_agent_templates() -> None:
    """列出可用的 agent 模板。

    模板是「造下属」的起手式 —— 选定后端、模型、技能、参数的一组默认值，
    再用 agent-new 的其它参数覆盖其中任意几项。
    """
    cfg = _cfg()
    from .agents import load_templates

    templates = load_templates(cfg.ws)
    if not templates:
        console.print("模板文件 config/agent-templates.toml 不存在或为空。")
        console.print("不用模板也能造：`commander agent-new <id> --backend ... --model ...`")
        return

    t = Table(title=f"Agent 模板 ({len(templates)})", show_header=True, header_style="bold")
    for c in ("模板", "后端", "模型", "落点", "技能", "职责"):
        t.add_column(c)
    for name, tp in sorted(templates.items()):
        desc = " ".join(tp.description.split())[:52]
        t.add_row(name, tp.backend, tp.model, tp.target,
                  ",".join(tp.skills) or "-", desc)
    console.print(t)
    console.print("\n造一个：`commander agent-new <新id> --template <模板名>`")


@app.command("agent-new")
def agent_new(
    agent_id: str = typer.Argument(..., help="新 agent 的 id（会变成目录名，不能含斜杠）"),
    template: str | None = typer.Option(None, "--template", "-T",
                                        help="从哪个模板起手，见 agent-templates"),
    backend: str | None = typer.Option(None, "--backend", "-b", help="用哪个 SDK 后端"),
    model: str | None = typer.Option(None, "--model", "-m", help="用哪个模型别名"),
    target: str | None = typer.Option(None, "--target", help="local | remote | auto"),
    skills: str | None = typer.Option(None, "--skills", help="逗号分隔，覆盖模板默认"),
    description: str | None = typer.Option(None, "--desc", "-d", help="职责说明"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    timeout: int | None = typer.Option(None, "--timeout", help="秒"),
    max_turns: int | None = typer.Option(None, "--max-turns"),
    force: bool = typer.Option(False, "--force", help="已存在时覆盖"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示会写成什么，不动手"),
) -> None:
    """造一个下属 agent，写进 config/agents.toml。

    **这是指挥官的常规动作**，不是高级功能：当任务需要的能力现有 agent 都不具备
    （不同的后端、不同的模型、专门的职责），就当场造一个。

    两种用法：

    ```bash
    # 从模板起手，改几个字段
    commander agent-new api-auditor --template reviewer

    # 完全自定义 —— 指挥官自己发挥
    commander agent-new perf-hunter --backend openai --model pro \
      --skills analyze,critique -d "专攻性能瓶颈定位，产出必须带压测数据"
    ```

    创建前会校验后端、模型、技能是否存在；有问题当场报错，
    不会写进去一个跑不通的 agent。用完 `brief` 给它生成边界说明书。
    """
    cfg = _cfg()
    from .agents import AgentError, backup_agents_toml, create

    try:
        agent, warns = create(
            cfg.ws, cfg, agent_id,
            template=template, backend=backend, model=model, target=target,
            skills=skills.split(",") if skills else None,
            description=description, max_tokens=max_tokens,
            timeout=timeout, max_turns=max_turns,
            force=force, dry_run=dry_run,
        )
    except AgentError as exc:
        err.print(f"[red]✗ {exc}[/]")
        raise typer.Exit(2) from None

    if dry_run:
        console.print(f"[bold]agent {agent.id}[/] 将会是这样：")
        console.print(f"  后端   {agent.backend}")
        console.print(f"  模型   {agent.model}")
        console.print(f"  落点   {agent.target}")
        console.print(f"  技能   {', '.join(agent.skills) or '(无)'}")
        console.print(f"  tokens {agent.max_tokens}   超时 {agent.timeout}s   轮数 {agent.max_turns}")
        console.print(f"  职责   {' '.join(agent.description.split())[:70]}")
        for w in warns:
            console.print(f"  [yellow]⚠[/] {w}")
        console.print("\n[dim]--dry-run，未写入。[/]")
        return

    bak = backup_agents_toml(cfg.ws)
    console.print(f"[green]✓[/] agent [bold]{agent.id}[/] 已写入 "
                  f"{cfg.ws.agents_toml.relative_to(cfg.ws.root)}")
    if bak:
        console.print(f"  [dim]改动前的备份: {bak.name}[/]")
    for w in warns:
        console.print(f"  [yellow]⚠[/] {w}")
    console.print(f"\n  派发试试：./.commander/cmd dispatch {agent.id} -p '<任务>'")


@app.command("agent-rm")
def agent_rm(
    agent_id: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force", help="确认删除"),
) -> None:
    """从编制表里删掉一个 agent。

    **不删它的工作目录** —— `agents/<id>/` 里有日志、产物、BRIEF，
    那是审计记录。要清得自己动手。

    只想临时停用的话，别删 —— 在 agents.toml 里给它加一行 `enabled = false`。
    """
    cfg = _cfg()
    from .agents import AgentError, backup_agents_toml, remove

    try:
        remove(cfg.ws, agent_id, force=force)
    except AgentError as exc:
        err.print(f"[red]✗ {exc}[/]")
        raise typer.Exit(2) from None

    bak = backup_agents_toml(cfg.ws)
    console.print(f"[green]✓[/] 已从编制表移除 [bold]{agent_id}[/]")
    if bak:
        console.print(f"  [dim]备份: {bak.name}；被删的内容也追加到了 "
                      f"logs/removed-agents.toml[/]")
    console.print(f"  [dim]工作目录 agents/{agent_id}/ 未动（审计记录）[/]")


@app.command("agent-show")
def agent_show(agent_id: str = typer.Argument(...)) -> None:
    """看一个 agent 的完整配置。"""
    cfg = _cfg()
    try:
        a = cfg.agent(agent_id)
    except ConfigError as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from None

    from .config import HEAVY_BACKENDS
    where = ("远端（重依赖）" if a.backend in HEAVY_BACKENDS
             else "本地（轻量）") if a.target == "auto" else a.target
    m = cfg.models.get(a.model)
    key_ok = bool(cfg.providers.get(m.provider) and cfg.providers[m.provider].api_key()) \
        if m else False

    t = Table(show_header=False, box=None)
    t.add_column(style="bold")
    t.add_column()
    t.add_row("id", a.id)
    t.add_row("后端", a.backend)
    t.add_row("模型", f"{a.model}" + (f"  ({m.model})" if m else "") +
              ("" if key_ok or (m and m.provider == "local") else "  [red]⚠ 该 provider 缺密钥[/]"))
    t.add_row("落点", where)
    t.add_row("技能", ", ".join(a.skills) or "(无)")
    t.add_row("max_tokens", str(a.max_tokens))
    t.add_row("超时", f"{a.timeout}s")
    t.add_row("最大轮数", str(a.max_turns))
    t.add_row("启用", "是" if a.enabled else "[yellow]否（enabled=false）[/]")
    console.print(t)

    if a.description.strip():
        console.print(Panel(a.description.strip(), title="职责", border_style="dim"))

    workdir = cfg.ws.agent_workdir(agent_id)
    if workdir.is_dir():
        n = len(list(cfg.ws.agent_logs(agent_id).glob("*")))
        console.print(f"工作目录 {workdir}（{n} 个记录文件）")


@app.command(
    "python",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_python(ctx: typer.Context) -> None:
    """在引擎的 venv 里跑一段 Python —— 诊断用的逃生口。

    为什么需要它：文档里有些排查片段要直接调引擎的模块
    （比如手动验证沙箱、查远端状态）。写成 `uv run --project <路径> python`
    就得硬编码路径，而两种布局下那个路径不同（扁平 `bin/`、内嵌 `.commander/bin/`），
    写死哪个都会在另一种布局下失败。

    这个子命令自己知道引擎在哪，所以文档可以统一写：

        ./.commander/cmd python -c "from commander.sandbox import Sandbox; ..."

    等价于在过去那个 venv 里跑 python，并带上工作区环境变量。
    """
    import os
    import subprocess as sp

    cfg = _cfg()
    venv_python = cfg.ws.bin_dir / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        err.print(f"[red]引擎 venv 不存在：{venv_python}[/]")
        err.print("  先跑：./.commander/cmd doctor")
        raise typer.Exit(2) from None

    env = dict(os.environ)
    env["COMMANDER_ROOT"] = str(cfg.ws.root)
    args = list(ctx.args)
    if not args:
        err.print('[red]要跑什么？例如： ./.commander/cmd python -c "print(1)"[/]')
        raise typer.Exit(2) from None
    raise typer.Exit(sp.run([str(venv_python), *args], env=env).returncode)


@app.command("models")
def list_models() -> None:
    """列出可用的大模型 API。"""
    cfg = _cfg()
    t = Table(title="模型注册表", show_header=True, header_style="bold")
    for c in ("别名", "模型", "Provider", "成本", "上下文", "工具", "状态"):
        t.add_column(c)

    for m in cfg.models.values():
        p = cfg.providers.get(m.provider)
        has_key = bool(p and (p.api_key() or m.provider == "local"))
        t.add_row(
            m.alias, m.model, m.provider, m.cost_tier,
            f"{m.context:,}",
            "✓" if m.supports_tools else "-",
            "[green]可用[/]" if has_key else "[red]缺密钥[/]",
            style="" if has_key else "dim",
        )
    console.print(t)


@app.command("brief")
def write_brief(
    agent: str | None = typer.Argument(None, help="留空则全部生成"),
    force: bool = typer.Option(False, "--force", help="覆盖已有 BRIEF.md"),
) -> None:
    """从 agents.toml 生成下属的 BRIEF.md（职责与边界说明书）。

    BRIEF 是给 agent 自己看的边界声明，也是审计时的依据。
    它由编制表生成，所以不会与 agents.toml 漂移。
    """
    cfg = _cfg()
    from .agents import write_brief as gen

    ws = cfg.ws
    ids = [agent] if agent else sorted(cfg.agents)
    made, skipped = [], []

    for aid in ids:
        try:
            cfg.agent(aid)
        except ConfigError as exc:
            err.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from None
        (made if gen(ws, cfg, aid, force=force) else skipped).append(aid)

    for aid in made:
        console.print(f"[green]✓[/] {aid} → {ws.agent_brief(aid).relative_to(ws.root)}")
    if skipped:
        console.print(f"[dim]跳过 {len(skipped)} 个已存在的（--force 可覆盖）: "
                      f"{', '.join(skipped)}[/]")



# ══════════════════════════════════════════════════════════════════════════
# memory
# ══════════════════════════════════════════════════════════════════════════
@mem_app.command("index")
def memory_index() -> None:
    """重建 memory/INDEX.md。记忆文件变更后跑一次。"""
    cfg = _cfg()
    p = MemoryStore(cfg.ws).render_index()
    st = MemoryStore(cfg.ws).stats()
    console.print(f"[green]✓[/] 索引已重建: {p.relative_to(cfg.ws.root)}")
    console.print("  " + "  ".join(f"{k}={v}" for k, v in st.items()))


@mem_app.command("search")
def memory_search(
    query: str = typer.Argument("", help="关键词；留空则列全部"),
    kind: str | None = typer.Option(None, "--kind", "-k", help=f"限定类型: {KINDS}"),
    tag: str | None = typer.Option(None, "--tag"),
    status: str | None = typer.Option(None, "--status",
                                          help="success | failed | partial | open"),
    limit: int = typer.Option(20, "--limit", "-n"),
    full: bool = typer.Option(False, "--full", help="打印正文"),
) -> None:
    """检索记忆。失败的尝试排在前面 —— 那是最该先看的。"""
    cfg = _cfg()
    store = MemoryStore(cfg.ws)
    hits = store.search(query, kind=kind, tag=tag, status=status, limit=limit)

    if not hits:
        console.print("[yellow]无匹配。[/]")
        if not store.has_rg():
            console.print("[dim]提示：未安装 rg，已退化为全量扫描[/]")
        raise typer.Exit(0) from None

    t = Table(title=f"记忆检索: {query or '(全部)'} — {len(hits)} 条",
              show_header=True, header_style="bold")
    for c in ("状态", "类型", "名称", "标签", "路径"):
        t.add_column(c)
    for m in hits:
        sc = {"failed": "red", "success": "green", "partial": "yellow"}.get(m.status, "")
        t.add_row(
            f"[{sc}]{m.status}[/]" if sc else m.status,
            m.kind, m.name, ",".join(m.tags[:3]),
            str(m.path.relative_to(cfg.ws.root)) if m.path else "",
        )
    console.print(t)

    if full:
        for m in hits:
            console.print(Panel(m.body, title=f"{m.kind}/{m.name}",
                                subtitle=str(m.path), border_style="dim"))


@mem_app.command("attempt")
def memory_attempt(
    name: str = typer.Argument(..., help="尝试的名称"),
    approach: str = typer.Option(..., "--approach", "-a", help="做了什么"),
    outcome: str = typer.Option(..., "--outcome", "-o", help="结果如何"),
    status: str = typer.Option("failed", "--status", "-s",
                               help="success | failed | partial"),
    why: str = typer.Option("", "--why", help="为什么（不）成立"),
    retry_when: str = typer.Option("", "--retry-when",
                                   help="什么条件下可以再试（关键字段）"),
    tags: str = typer.Option("", "--tags", help="逗号分隔"),
) -> None:
    """记录一次尝试。失败也要记 —— 避免指挥官反复踩同一个坑。"""
    cfg = _cfg()
    p = MemoryStore(cfg.ws).note_attempt(
        name, approach=approach, outcome=outcome, status=status,
        why=why, retry_when=retry_when,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    )
    console.print(f"[green]✓[/] 已记入 {p.relative_to(cfg.ws.root)}")


@mem_app.command("write")
def memory_write(
    name: str = typer.Argument(...),
    kind: str = typer.Option("facts", "--kind", "-k", help=f"{KINDS}"),
    body: str = typer.Option("", "--body", "-b"),
    body_file: Path | None = typer.Option(None, "--body-file"),
    tags: str = typer.Option("", "--tags"),
    status: str = typer.Option("open", "--status"),
) -> None:
    """写一条记忆。"""
    cfg = _cfg()
    text = _read_text_arg(body, body_file)
    p = MemoryStore(cfg.ws).write(Memory(
        name=name, kind=kind, body=text, status=status,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
    ))
    console.print(f"[green]✓[/] {p.relative_to(cfg.ws.root)}")


@mem_app.command("show")
def memory_show(path: str = typer.Argument(...)) -> None:
    """打印一条记忆的全文。"""
    cfg = _cfg()
    f = cfg.ws.root / path
    if not f.is_file():
        f = cfg.ws.memory_dir / path
    if not f.is_file():
        err.print(f"[red]找不到 {path}[/]")
        raise typer.Exit(2) from None
    m = MemoryStore(cfg.ws).load(f)
    console.print(Panel(m.body if m else f.read_text(encoding="utf-8"),
                        title=m.name if m else path))


# ══════════════════════════════════════════════════════════════════════════
# task
# ══════════════════════════════════════════════════════════════════════════
@task_app.command("new")
def task_new(
    task_id: str = typer.Argument(...),
    title: str = typer.Option("", "--title"),
    goal: str = typer.Option("", "--goal", "-g"),
    acceptance: str = typer.Option("", "--acceptance", "-a", help="验收标准"),
    angles: str = typer.Option("", "--angles", help="逗号分隔的拆解角度"),
    parent: str | None = typer.Option(None, "--parent"),
) -> None:
    """新建任务。先写清验收标准再派发 —— 否则无法判断何时算完成。"""
    cfg = _cfg()
    t = TaskRegistry(cfg.ws).create(
        task_id, title=title, goal=goal, acceptance=acceptance,
        angles=[a.strip() for a in angles.split(",") if a.strip()],
        parent=parent,
    )
    console.print(f"[green]✓[/] 任务 {t.task_id} 已建")
    console.print(f"  {cfg.ws.task_dir(t.task_id).relative_to(cfg.ws.root)}/task.md")


@task_app.command("list")
def task_list(
    all_: bool = typer.Option(False, "--all", help="含已归档"),
) -> None:
    """列出任务。"""
    cfg = _cfg()
    tr = TaskRegistry(cfg.ws)
    tasks = tr.list_active()
    archived = sorted(p.name for p in cfg.ws.task_archive.iterdir()
                      ) if cfg.ws.task_archive.is_dir() else []

    if not tasks and not all_:
        console.print("无活动任务。新建：commander task new <id> -g '<目标>'")
        return

    t = Table(title="任务", show_header=True, header_style="bold")
    for c in ("task_id", "状态", "目标", "派发", "tokens", "更新"):
        t.add_column(c)
    for x in sorted(tasks, key=lambda v: -v.updated):
        t.add_row(
            x.task_id, x.status, (x.goal or x.title or "")[:50],
            str(len(x.runs)), f"{x.tokens_used:,}",
            time.strftime("%m-%d %H:%M", time.localtime(x.updated)),
        )
    console.print(t)
    if all_ and archived:
        console.print(f"[dim]已归档: {', '.join(archived)}[/]")


@task_app.command("show")
def task_show(task_id: str = typer.Argument(...)) -> None:
    """打印任务档案全文。"""
    cfg = _cfg()
    f = TaskRegistry(cfg.ws).task_file(task_id)
    if not f.is_file():
        err.print(f"[red]找不到任务 {task_id}[/]")
        raise typer.Exit(2) from None
    console.print(f.read_text(encoding="utf-8"))


@task_app.command("update")
def task_update(
    task_id: str = typer.Argument(...),
    status: str | None = typer.Option(None, "--status", "-s",
                                         help="pending|active|blocked|done|failed|abandoned"),
    note: str | None = typer.Option(None, "--note", "-n"),
    goal: str | None = typer.Option(None, "--goal"),
) -> None:
    """更新任务状态或追加笔记。"""
    cfg = _cfg()
    tr = TaskRegistry(cfg.ws)
    if status:
        tr.update(task_id, status=status)
    if goal:
        tr.update(task_id, goal=goal)
    if note:
        tr.add_note(task_id, note)
    tr.render_board()
    console.print(f"[green]✓[/] {task_id} 已更新")


@task_app.command("board")
def task_board() -> None:
    """重建 tasks/BOARD.md 看板。"""
    cfg = _cfg()
    p = TaskRegistry(cfg.ws).render_board()
    console.print(f"[green]✓[/] {p.relative_to(cfg.ws.root)}")


@task_app.command("archive")
def task_archive(task_id: str = typer.Argument(...)) -> None:
    """把任务移入 archive/。"""
    cfg = _cfg()
    tr = TaskRegistry(cfg.ws)
    tr.update(task_id, status="done")
    p = tr.archive(task_id)
    if p:
        console.print(f"[green]✓[/] 已归档到 {p.relative_to(cfg.ws.root)}")
        tr.render_board()
    else:
        err.print(f"[red]找不到 {task_id}[/]")
        raise typer.Exit(2) from None


# ══════════════════════════════════════════════════════════════════════════
# outbox
# ══════════════════════════════════════════════════════════════════════════
@outbox_app.command("list")
def outbox_list(
    agent: str | None = typer.Option(None, "--agent", "-a"),
    uncollected: bool = typer.Option(False, "--uncollected", "-u"),
) -> None:
    """列出下属交回的结果。"""
    cfg = _cfg()
    recs = list_outbox(cfg.ws, agent, uncollected_only=uncollected)
    if not recs:
        console.print("无结果。")
        return

    t = Table(title=f"Outbox — {len(recs)} 条", show_header=True, header_style="bold")
    for c in ("已收", "Agent", "任务", "结果", "模型", "耗时", "文件"):
        t.add_column(c)
    for r in recs:
        res = r.get("result", {})
        ok = "[green]✓[/]" if res.get("ok") else "[red]✗[/]"
        t.add_row(
            "●" if r.get("collected") else "○",
            r.get("agent_id", ""), r.get("task_id", ""), ok,
            res.get("model", ""), f"{res.get('duration_s', 0):.0f}s",
            r.get("_file", ""),
        )
    console.print(t)


@outbox_app.command("show")
def outbox_show(file: str = typer.Argument(..., help="outbox 相对路径")) -> None:
    """打印一条结果的完整内容。"""
    cfg = _cfg()
    f = cfg.ws.root / file
    if not f.is_file():
        err.print(f"[red]找不到 {file}[/]")
        raise typer.Exit(2) from None
    rec = json.loads(f.read_text(encoding="utf-8"))
    res = rec.get("result", {})
    console.print(Panel(res.get("text") or res.get("error") or "(空)",
                        title=f"{rec.get('agent_id')} · {rec.get('task_id')}",
                        subtitle=file))
    if res.get("usage"):
        u = res["usage"]
        console.print(f"tokens: in={u.get('input_tokens')} out={u.get('output_tokens')} "
                      f"reasoning={u.get('reasoning_tokens')}")
    if res.get("artifacts"):
        console.print("产物: " + ", ".join(res["artifacts"]))


@outbox_app.command("collect")
def outbox_collect(
    agent: str | None = typer.Option(None, "--agent", "-a"),
    all_: bool = typer.Option(False, "--all"),
    file: str | None = typer.Option(None, "--file"),
) -> None:
    """标记结果已收取，避免重复处理。"""
    cfg = _cfg()
    if file:
        mark_collected(cfg.ws, file)
        console.print(f"[green]✓[/] {file}")
        return
    recs = list_outbox(cfg.ws, agent, uncollected_only=True)
    if not recs:
        console.print("没有待收集的结果。")
        return
    for r in recs:
        mark_collected(cfg.ws, r["_file"])
    console.print(f"[green]✓[/] 已标记 {len(recs)} 条为已收集")


# ══════════════════════════════════════════════════════════════════════════
# patrol —— loop 用
# ══════════════════════════════════════════════════════════════════════════
@app.command()
def patrol(
    remote: bool = typer.Option(False, "--remote", help="顺带检测远端"),
    json_out: bool = typer.Option(False, "--json"),
    oneline: bool = typer.Option(False, "--oneline", help="只输出一行摘要（loop 用）"),
    write: bool = typer.Option(True, "--write/--no-write", help="落盘到 logs/patrol-latest.md"),
) -> None:
    """巡检 —— `/loop` 每次唤醒时跑这个，然后按简报决策。"""
    cfg = _cfg()
    rep = Patroller(cfg).run(check_remote=remote)

    if write:
        Patroller(cfg).write_report(rep)

    if oneline:
        console.print(rep.to_oneline())
    elif json_out:
        console.print_json(json.dumps({
            "summary": rep.to_oneline(),
            "actions": [f.__dict__ for f in rep.findings if f.level == "action"],
            "warnings": [f.__dict__ for f in rep.findings if f.level == "warn"],
            "stats": rep.stats,
        }, ensure_ascii=False, default=str))
    else:
        console.print(rep.to_markdown())

    raise typer.Exit(1 if rep.has_work() else 0)


# ══════════════════════════════════════════════════════════════════════════
# remote
# ══════════════════════════════════════════════════════════════════════════
@remote_app.command("check")
def remote_check(host: str | None = typer.Option(None, "--host")) -> None:
    """检测远端连通性与环境。"""
    cfg = _cfg()
    from .ssh_runner import RemoteRunner
    rr = RemoteRunner(cfg, Guard(cfg.ws, cfg.policy.guard))
    hosts = [cfg.hosts[host]] if host else list(cfg.hosts.values())
    if not hosts:
        err.print("[red]remote/hosts.toml 里没有主机[/]")
        raise typer.Exit(2) from None
    for h in hosts:
        ok, detail = rr.check(h)
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"{mark} {h.name} ({h.ssh_target()}) — {detail}")


@remote_app.command("bootstrap")
def remote_bootstrap(
    host: str | None = typer.Option(None, "--host"),
    no_key: bool = typer.Option(False, "--no-key", help="不生成/安装专用密钥"),
) -> None:
    """首次准备远端：装 uv、建目录、装专用密钥。"""
    cfg = _cfg()
    from .ssh_runner import RemoteError, RemoteRunner
    rr = RemoteRunner(cfg, Guard(cfg.ws, cfg.policy.guard))
    hosts = [cfg.hosts[host]] if host else list(cfg.hosts.values())
    for h in hosts:
        console.print(f"[bold]bootstrap {h.name} ({h.ssh_target()})[/]")
        try:
            for step in rr.bootstrap(h, install_key=not no_key):
                console.print(f"  {step}")
        except RemoteError as exc:
            err.print(f"[red]✗ {exc}[/]")
            raise typer.Exit(1) from None


@remote_app.command("sync")
def remote_sync(
    backend: str = typer.Argument(..., help="要同步的后端，如 crewai"),
    host: str | None = typer.Option(None, "--host"),
    install: bool = typer.Option(True, "--install/--no-install",
                                 help="顺带在远端 uv sync"),
) -> None:
    """把后端工程同步到远端并装依赖。"""
    cfg = _cfg()
    from .ssh_runner import RemoteError, RemoteRunner
    rr = RemoteRunner(cfg, Guard(cfg.ws, cfg.policy.guard))
    h = cfg.hosts[host] if host else cfg.default_host()
    if h is None:
        err.print("[red]没有可用主机[/]")
        raise typer.Exit(2) from None

    console.print(f"[bold]{h.name}[/] ← 同步后端 {backend}")
    console.print(rr.sync(h, backend))
    if install:
        try:
            rr.ensure_remote_env(h, backend)
            console.print(f"[green]✓[/] 远端 {backend} 依赖就绪")
        except RemoteError as exc:
            err.print(f"[red]✗ {exc}[/]")
            raise typer.Exit(1) from None


# ══════════════════════════════════════════════════════════════════════════
# backend
# ══════════════════════════════════════════════════════════════════════════
@be_app.command("list")
def backend_list() -> None:
    """列出所有 SDK 后端及其就绪状态。"""
    cfg = _cfg()
    from .backends import BACKEND_DIRS, BackendLauncher, backend_readiness
    from .config import HEAVY_BACKENDS
    launcher = BackendLauncher(cfg.ws, Guard(cfg.ws, cfg.policy.guard))

    t = Table(title="SDK 后端", show_header=True, header_style="bold")
    for c in ("后端", "SDK", "依赖", "建议落点", "本机状态"):
        t.add_column(c)
    sdk_names = {
        "claude": "claude-agent-sdk", "openai": "openai-agents",
        "langchain": "langchain + langgraph", "crewai": "crewai",
        "autogen": "autogen-agentchat", "hermes": "hermes-agent + hermes-acp-sdk",
        "browser_use": "browser-use（需 Chrome）",
        "openai_compat": "纯 httpx", "mock": "无依赖",
    }
    # 状态用 backend_readiness —— 它还会检查「装了但跑不起来」的前置条件
    # （比如 browser_use 装了依赖却没有 Chrome）。只看 .venv 在不在会误判。
    readiness = {r["backend"]: r for r in backend_readiness(cfg, check_remote=True)}

    for b in sorted(BACKEND_DIRS):
        d = launcher.project_dir(b)
        exists = (d / "runner.py").is_file()
        r = readiness.get(b, {})
        if not exists:
            status = "[red]缺 runner.py[/]"
        elif r.get("ok"):
            status = "[green]就绪[/]"
        else:
            status = f"[yellow]{r.get('detail', '未就绪')[:26]}[/]"
        t.add_row(
            b, sdk_names.get(b, "?"),
            "已生成" if exists else "[red]缺 runner.py[/]",
            "远端" if b in HEAVY_BACKENDS else "本地",
            status,
        )
    console.print(t)
    console.print("\n装依赖：`uv sync --project bin/backends/<后端>`")


@be_app.command("install")
def backend_install(
    backend: str = typer.Argument(...),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="装完顺带验证 import"),
) -> None:
    """在本地安装某个后端的依赖。"""
    cfg = _cfg()
    from .backends import BackendError, BackendLauncher
    launcher = BackendLauncher(cfg.ws, Guard(cfg.ws, cfg.policy.guard))
    try:
        console.print(f"安装后端 [bold]{backend}[/] …（首次可能较慢）")
        launcher.ensure_installed(backend, sync=True)
        console.print("[green]✓[/] 依赖已就绪")
        if probe:
            r = launcher.probe_env(backend)
            mark = "[green]✓[/]" if r.get("ok") else "[red]✗[/]"
            console.print(f"{mark} import 验证: {r.get('detail', r.get('error'))}")
    except BackendError as exc:
        err.print(f"[red]✗ {exc}[/]")
        raise typer.Exit(1) from None


@be_app.command("probe")
def backend_probe(backend: str = typer.Argument(...)) -> None:
    """验证某个后端的依赖能否真的 import。"""
    cfg = _cfg()
    from .backends import BackendLauncher
    launcher = BackendLauncher(cfg.ws, Guard(cfg.ws, cfg.policy.guard))
    r = launcher.probe_env(backend)
    console.print_json(json.dumps(r, ensure_ascii=False))


# ══════════════════════════════════════════════════════════════════════════
# skill
# ══════════════════════════════════════════════════════════════════════════
@app.command("skills")
def list_skills() -> None:
    """列出技能库中可赋予下属的技能。"""
    cfg = _cfg()
    from .skills import SkillLibrary
    lib = SkillLibrary(cfg.ws)
    names = lib.names()
    if not names:
        console.print("技能库为空。用 `commander skill-new <name>` 新建。")
        return
    t = Table(title=f"技能库 ({len(names)})", show_header=True, header_style="bold")
    for c in ("技能", "描述", "估算 tokens"):
        t.add_column(c)
    for n in names:
        try:
            m = lib.load(n)
            t.add_row(n, m.description[:70] or "-", f"~{m.tokens_estimate}")
        except Exception as exc:
            t.add_row(n, f"[red]{exc}[/]", "-")
    console.print(t)


@app.command("skill-new")
def skill_new(
    name: str = typer.Argument(...),
    description: str = typer.Option("", "--description", "-d"),
) -> None:
    """新建一个技能骨架，供指挥官赋予下属。"""
    cfg = _cfg()
    from .skills import SkillLibrary
    p = SkillLibrary(cfg.ws).scaffold(name, description)
    console.print(f"[green]✓[/] {p.relative_to(cfg.ws.root)}")


@app.command("skill-show")
def skill_show(name: str = typer.Argument(...)) -> None:
    """打印技能内容。"""
    cfg = _cfg()
    from .skills import SkillLibrary
    m = SkillLibrary(cfg.ws).load(name)
    console.print(Panel(m.body, title=f"{m.name} — {m.description}",
                        subtitle=str(m.path.relative_to(cfg.ws.root))))



# ── budget：预算与决策 ───────────────────────────────────────────────────
# 用户需求：「预算不应只是硬限制，而应作为任务决策信号」
#
# 预算在这里有两个身份：
#   ① 刹车 —— 到上限了就停（max_tokens_per_run / timeout）
#   ② 信号 —— 到水位了生成状态报告，让指挥官决定继续/加预算/调整/重规划
# 这两个命令管的是 ②。

budget_app = typer.Typer(help="预算状态与决策记录", no_args_is_help=True)
app.add_typer(budget_app, name="budget")


@budget_app.command("show")
def budget_show(
    task: str | None = typer.Option(None, "--task", "-t", help="只看某个任务"),
    agent: str | None = typer.Option(None, "--agent", "-a", help="只看某个下属"),
    history: int = typer.Option(0, "--history", "-H", help="顺带列出最近 N 条状态报告"),
) -> None:
    """显示三层预算的水位，以及是否需要干预。

    四维独立计量，取最紧张的那一维作为水位：
      tokens        token 消耗
      cost_units    经济成本代理量（按 cost_tier 折算，永远可用）
      wall_seconds  墙钟时间
      core_seconds  算力（墙钟 × 核数）
    """
    cfg = _cfg()
    from .budget import LEVEL_LABEL, BudgetStore

    store = BudgetStore(cfg.ws, cfg)

    t = Table(title="预算水位", show_header=True, header_style="bold")
    for c in ("层", "对象", "tokens", "成本", "时间", "算力", "轮次", "水位", "卡在"):
        t.add_column(c, overflow="fold", max_width=22)

    def fmt(used, lim, unit="", scale=1.0, integer=False):
        """轮次是计数，显示成小数很怪；其余维度保留一位小数。"""
        w = (lambda v: f"{v / scale:,.0f}") if integer else (lambda v: f"{v / scale:,.1f}")
        if lim is None:
            return f"{w(used)}{unit}" if used else "—"
        return f"{w(used)}/{w(lim)}{unit}"

    rows: list[tuple[str, str, object, object]] = []

    # 全局
    g_lim = store.limits()["global"]
    g_cons = store.consumption()
    rows.append(("global", "(全部)", g_cons, g_lim))

    if task:
        lims = store.limits(task_id=task, agent_id=agent)
        rows.append(("task", task, store.consumption(task_id=task), lims["task"]))
    if agent:
        rows.append(("agent", agent, store.consumption(agent_id=agent),
                     store.limits(agent_id=agent)["agent"]))

    for name, obj, cons, lim in rows:
        used, dim = cons.worst_ratio(lim)
        lvl = ("exhausted" if used >= 1.0 else "critical" if used >= 0.85
               else "warn" if used >= 0.6 else "ok")
        color = {"ok": "green", "warn": "yellow",
                 "critical": "red", "exhausted": "red"}[lvl]
        t.add_row(
            name, obj,
            fmt(cons.tokens, lim.tokens),
            fmt(cons.cost_units, lim.cost_units),
            fmt(cons.wall_seconds, lim.wall_seconds, "s"),
            fmt(cons.core_seconds, lim.core_seconds, "核秒"),
            fmt(cons.runs, lim.runs, integer=True),
            f"[{color}]{used:.0%} {LEVEL_LABEL[lvl]}[/]",
            f"{dim}",
        )
    console.print(t)

    # 有任务时给出完整评估（含建议动作与理由）
    if task:
        rep = store.evaluate_task(task, agent_id=agent)
        console.print()
        console.print(Panel(
            "\n".join(rep.reasons) or "一切正常，无需干预。",
            title=f"{rep.one_line()}",
            border_style="red" if rep.needs_attention else "green",
        ))

    if history:
        d = (cfg.ws.task_dir(task) / "budget") if task else (cfg.ws.logs_dir / "budget")
        hist = d / "history.jsonl"
        if hist.is_file():
            lines = hist.read_text(encoding="utf-8", errors="replace").splitlines()
            console.print("[bold]最近的状态报告[/]")
            for ln in lines[-history:]:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
                console.print(f"  {ts}  {r.get('one_line', '')}")
        else:
            console.print("[dim]还没有状态报告。[/]")


@budget_app.command("set")
def budget_set(
    task: str = typer.Argument(..., help="task_id"),
    tokens: int | None = typer.Option(None, "--tokens"),
    cost_units: float | None = typer.Option(None, "--cost-units"),
    wall_seconds: float | None = typer.Option(None, "--wall-seconds"),
    runs: int | None = typer.Option(None, "--runs"),
    clear: bool = typer.Option(False, "--clear", help="清空该任务的覆盖，回到默认"),
) -> None:
    """给某个任务单独设预算，覆盖 policy 里的 [budget.task]。

    ⚠️ **读完状态报告再做这个决定。** 如果报告说「消耗大、进展小」，
    正确答案是重规划而不是加预算 —— 给走错路的策略加钱，只是让它错得更贵。
    """
    cfg = _cfg()
    from .tasks import TaskRegistry
    tr = TaskRegistry(cfg.ws)
    t = tr.load(task)
    if t is None:
        err.print(f"[red]没有任务 {task}[/]")
        raise typer.Exit(2) from None

    if clear:
        tr.update(task, budget={})
        console.print(f"[green]✓[/] {task} 的预算覆盖已清空，回到默认")
        return

    b = dict(t.budget or {})
    for k, v in (("tokens", tokens), ("cost_units", cost_units),
                 ("wall_seconds", wall_seconds), ("runs", runs)):
        if v is not None:
            b[k] = v
    if not b:
        err.print("[red]至少要给一个维度。例如 --tokens 500000[/]")
        raise typer.Exit(2) from None

    tr.update(task, budget=b)
    console.print(f"[green]✓[/] {task} 预算已设为 {b}")
    from .budget import BudgetStore
    rep = BudgetStore(cfg.ws, cfg).evaluate_task(task)
    console.print(f"  当前水位: {rep.one_line()}")


@budget_app.command("decide")
def budget_decide(
    task: str = typer.Argument(..., help="task_id"),
    action: str = typer.Argument(..., help="continue | adjust | replan | increase | abort"),
    reason: str = typer.Option(..., "--reason", "-r", help="为什么这么决定"),
    outcome: str | None = typer.Option(None, "--outcome",
                                       help="回填上一次决策之后实际发生了什么"),
) -> None:
    """记录一次预算决策。

    决策历史有两个用途：单任务回溯（"当时为什么加预算"），
    以及跨任务复盘（"这类判断我是不是总做错"）。
    """
    cfg = _cfg()
    from .budget import ACTIONS, BudgetStore, Decision, DecisionLog

    if action not in ACTIONS:
        err.print(f"[red]action 必须是 {'/'.join(ACTIONS)}[/]")
        raise typer.Exit(2) from None

    log = DecisionLog(cfg.ws)

    if outcome:
        if log.annotate_outcome(task, outcome):
            console.print(f"[green]✓[/] 已回填 {task} 最近一次决策的结果")
        else:
            err.print("[red]没有可回填的决策记录[/]")
            raise typer.Exit(2) from None
        return

    store = BudgetStore(cfg.ws, cfg)
    rep = store.evaluate_task(task)
    log.record(Decision(
        task_id=task, action=action, reason=reason,
        snapshot={
            "level": rep.level, "used_ratio": round(rep.used_ratio, 4),
            "binding_dim": rep.binding_dim,
            "progress": rep.progress.effective,
            "burn_ratio": rep.burn_ratio,
            "system_suggested": rep.recommendation,
        },
    ))

    agree = "" if action == rep.recommendation else \
        f"  [yellow]（系统建议的是 {rep.action_label}）[/]"
    console.print(f"[green]✓[/] 已记录：{task} → {action}{agree}")
    console.print(f"  [dim]{reason}[/]")
    console.print("\n  [dim]做完之后回填结果："
                  f"commander budget decide {task} {action} -r '...' "
                  f"--outcome '实际发生了什么'[/]")


@budget_app.command("history")
def budget_history(
    task: str | None = typer.Option(None, "--task", "-t"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """看决策历史 —— 包括系统当时建议了什么、指挥官实际选了什么。"""
    cfg = _cfg()
    from .budget import DecisionLog

    recs = DecisionLog(cfg.ws).history(task, limit=limit)
    if not recs:
        console.print("还没有决策记录。")
        return

    t = Table(title=f"预算决策历史 ({len(recs)})", show_header=True, header_style="bold")
    for c in ("时间", "任务", "决策", "系统建议", "当时的现场", "理由", "结果"):
        t.add_column(c)
    for r in recs:
        snap = r.get("snapshot") or {}
        ctx = (f"水位 {snap.get('used_ratio', 0):.0%} "
               f"进展 {snap.get('progress') if snap.get('progress') is not None else '?'}"
               + (f" 效率 {snap['burn_ratio']:.1f}x" if snap.get("burn_ratio") else ""))
        agree = "✓" if r.get("action") == snap.get("system_suggested") else "✗"
        t.add_row(
            time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0))),
            r.get("task_id", ""), r.get("action_label", r.get("action", "")),
            f"{agree} {snap.get('system_suggested', '')}",
            ctx, (r.get("reason") or "")[:40],
            (r.get("outcome") or "[dim]未回填[/]")[:30],
        )
    console.print(t)


def _find_any_browser() -> tuple[str | None, str]:
    """找一个可用的 Chromium 系浏览器。返回 (路径, 来源)。

    两档，按代价从低到高：
      ① 系统装的 Chrome/Chromium/Edge/Brave —— 有就直接用
      ② playwright 缓存的 chromium —— 没系统浏览器时，可以下一个（~187MB）

    ⚠️ 为什么需要第②档：browser-use **自己不下载浏览器**。它内置了一个兜底
    （找不到时跑 `uvx playwright install chromium --with-deps`），但**那个兜底
    只给 60 秒超时**，而冷启动要下 233MB —— 实测必然超时。所以这一步得有人
    显式做掉，不能指望它。
    """
    import glob
    import shutil

    for b in ("google-chrome-stable", "google-chrome", "chromium",
              "chromium-browser", "microsoft-edge", "brave-browser"):
        if p := shutil.which(b):
            return p, "系统浏览器"
    for p in ("/opt/google/chrome/chrome", "/usr/lib/chromium/chromium"):
        if Path(p).exists():
            return p, "系统浏览器"

    # playwright 缓存（browser-use 的 _find_installed_browser_path 也找这里）
    cache = Path.home() / ".cache" / "ms-playwright"
    for pat in ("chromium-*/chrome-linux*/chrome",
                "chromium_headless_shell-*/chrome-linux*/headless_shell"):
        hits = sorted(glob.glob(str(cache / pat)))
        if hits:
            return hits[-1], "playwright 缓存"
    return None, "无"


def _install_playwright_chromium() -> tuple[bool, str]:
    """下 playwright 的 chromium。装在用户缓存目录，不动系统。

    约 187MB。这是没有系统浏览器时最省事的路径 ——
    不需要 root、不改系统包、且 browser-use 会自己找到它。
    """
    import shutil
    import subprocess as sp

    if not shutil.which("uvx"):
        return False, "需要 uvx（uv 自带）"
    try:
        p = sp.run(["uvx", "playwright", "install", "chromium"],
                   capture_output=True, text=True, timeout=900)
    except sp.TimeoutExpired:
        return False, "下载超时（>15 分钟）"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "")[-300:]
    found, _src = _find_any_browser()
    return (True, found) if found else (False, "装完仍找不到可执行文件")


def _local_browser(action: str, port: int = 9222) -> str:
    """在本机起/停/查无头 Chrome。返回类似远端管家脚本的输出。"""
    import shutil
    import subprocess as sp
    import urllib.request

    profile = "/tmp/bu-profile"    # noqa: S108 — 是 Chrome 的 profile 目录，不是临时文件

    def cdp_version():
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=3
            ) as r:
                import json as _j
                return _j.loads(r.read().decode()).get("Browser")
        except Exception:
            return None

    if action == "status":
        v = cdp_version()
        return f"UP {v}" if v else "DOWN"

    if action == "down":
        sp.run(["pkill", "-f", f"user-data-dir={profile}"],
               capture_output=True)
        return "STOPPED" if not cdp_version() else "STILL_UP"

    # up
    if v := cdp_version():
        return f"ALREADY {v}"
    exe, src = _find_any_browser()
    if not exe:
        return (
            "NO_BROWSER 本机没有任何 Chromium 系浏览器。\n"
            "    两条路：\n"
            "      · 系统装 Chrome（.deb 版，snap 版连不上 DevTools）\n"
            "      · 或让指挥官下一个 playwright chromium（约 187MB，装在用户缓存，"
            "不动系统）：\n"
            "          ./.commander/cmd browser up --install-browser"
        )
    log_note = f"（{src}）" if src == "playwright 缓存" else ""
    _ = log_note
    sp.run(["pkill", "-f", f"user-data-dir={profile}"], capture_output=True)
    shutil.rmtree(profile, ignore_errors=True)
    with open("/tmp/chrome-cdp.log", "w") as log:  # noqa: S108 — 日志文件，不是临时目录
        sp.Popen(
            [exe, "--headless=new", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "about:blank"],
            stdout=log, stderr=log, stdin=sp.DEVNULL, start_new_session=True,
        )
    for _ in range(25):
        import time as _t
        _t.sleep(1)
        if v := cdp_version():
            return f"STARTED {v}"
    return "FAILED 见 /tmp/chrome-cdp.log"


# ── browser：浏览器的生命周期（本地或远端）─────────────────────────────────────────
# browser-use **不下载浏览器**，而是通过 CDP 连到一个**已经在跑的 Chrome**。
# 所以用之前必须确保远端有一个开着调试端口的 Chrome —— 这个命令组管这件事。
#
# 为什么不在后端里自动拉起：Chrome 起来后生命周期比一次派发长得多
# （反复起停既慢又要重新登录），而且孤儿 Chrome 会占住管道（踩过）。
# 由指挥官显式管理更可控。

browser_app = typer.Typer(help="远端浏览器（browser-use 需要）", no_args_is_help=True)
app.add_typer(browser_app, name="browser")

# 远端上那个管家脚本。写成脚本而不是一长串内联命令，因为
# 内联命令里出现 "remote-debugging-port" 会让 pkill 匹配到自己
# （踩过两次），而且多层引号转义极容易出错。
_BROWSER_SCRIPT = r"""#!/bin/sh
# 远端无头 Chrome 管家。幂等。
PORT="${BU_PORT:-9222}"
PROFILE="${BU_PROFILE:-/tmp/bu-profile}"
LOG="/tmp/chrome-cdp.log"
CMD="${BU_CHROME:-google-chrome-stable}"

cdp_ok() {
  python3 - "$PORT" <<'PY' 2>/dev/null
import json, sys, urllib.request
try:
    d = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{sys.argv[1]}/json/version", timeout=3))
    print(d.get("Browser", "?"))
except Exception:
    raise SystemExit(1)
PY
}

case "$1" in
  up)
    if v=$(cdp_ok); then echo "ALREADY $v"; exit 0; fi
    command -v "$CMD" >/dev/null 2>&1 || {
      echo "NO_CHROME 找不到 $CMD。apt 装 .deb 版 Chrome（snap 版连不上 DevTools）"; exit 2; }
    for pid in $(pgrep -f "user-data-dir=$PROFILE" 2>/dev/null); do kill "$pid" 2>/dev/null; done
    sleep 1; rm -rf "$PROFILE"
    setsid "$CMD" --headless=new --no-sandbox --disable-gpu \
      --disable-dev-shm-usage --remote-debugging-port="$PORT" \
      --user-data-dir="$PROFILE" about:blank </dev/null >"$LOG" 2>&1 &
    i=0
    while [ $i -lt 25 ]; do
      sleep 1; i=$((i+1))
      if v=$(cdp_ok); then echo "STARTED $v"; exit 0; fi
    done
    echo "FAILED"; tail -5 "$LOG"; exit 1
    ;;
  down)
    for pid in $(pgrep -f "user-data-dir=$PROFILE" 2>/dev/null); do kill "$pid" 2>/dev/null; done
    sleep 1
    if cdp_ok >/dev/null 2>&1; then echo "STILL_UP"; exit 1; fi
    echo "STOPPED"
    ;;
  status)
    if v=$(cdp_ok); then echo "UP $v"; else echo "DOWN"; fi
    ;;
  *) echo "用法: browser.sh up|down|status"; exit 2 ;;
esac
"""


def _browser_host(cfg, host: str | None, local: bool = False):
    """决定浏览器管家跑在哪台机器上。

    本地有 Chrome 就跑本地 —— browser-use 落本地时最省事（不用跨机器传数据），
    而且不需要先配远端。只有本地没有 Chrome 时才需要远端。
    """
    from .backends import _find_chrome

    if not local and (host is not None or not _find_chrome()):
        from .guard import Guard
        from .ssh_runner import RemoteRunner
        h = cfg.hosts.get(host) if host else cfg.default_host()
        if h is not None:
            return h, RemoteRunner(cfg, Guard(cfg.ws, cfg.policy.guard))
        if host is not None:
            err.print(f"[red]hosts.toml 里没有主机 {host!r}[/]")
            raise typer.Exit(2)
        err.print("[red]本机没有 Chrome，也没配远端。[/]")
        err.print("  本机装 Chrome 后重跑；或配远端：./.commander/cmd remote bootstrap")
        raise typer.Exit(2)
    return None, None      # 本地


def _browser_ensure_script(rr, host) -> None:
    import base64
    b64 = base64.b64encode(_BROWSER_SCRIPT.encode()).decode()
    p = rr.ssh_exec(
        host,
        f"echo {b64} | base64 -d > {host.workdir}/browser.sh && "
        f"chmod +x {host.workdir}/browser.sh && echo OK",
        timeout=60,
    )
    if "OK" not in (getattr(p, "stdout", "") or ""):
        err.print("[red]无法在远端写入 browser.sh[/]")
        raise typer.Exit(2)


@browser_app.command("up")
def browser_up(
    host: str | None = typer.Option(None, "--host"),
    port: int = typer.Option(9222, "--port"),
    local: bool = typer.Option(False, "--local", help="强制在本机起"),
    install_browser: bool = typer.Option(
        False, "--install-browser",
        help="没浏览器时自动下一个 playwright chromium（约 187MB，装在用户缓存）"),
) -> None:
    """起一个带 CDP 的无头浏览器（幂等，已经在跑就直接返回）。

    browser-use **自己不下载浏览器** —— 它连一个已运行的 Chrome/Chromium。
    所以机器上得有一个。两档来源：

      ① 系统装的 Chrome/Chromium/Edge/Brave  —— 首选
      ② playwright 缓存的 chromium            —— 没有系统浏览器时用这个

    第②档可以用 `--install-browser` 自动准备。它下载约 187MB 到
    `~/.cache/ms-playwright`，**不需要 root、不动系统包**，
    而且 browser-use 会自己找到它。
    """
    cfg = _cfg()
    # --install-browser 的语义就是「在本机装一个」，所以它隐含 --local。
    # ⚠️ 实测踩过：不隐含时会落到远端 —— 明明要装本地浏览器，
    #    却因为远端恰好有一个而直接连过去了，用户拿不到想要的结果。
    h, rr = _browser_host(cfg, host, local=local or install_browser)

    if h is None:
        exe, src = _find_any_browser()
        if not exe and install_browser:
            console.print("[bold]本机没有浏览器，下载 playwright chromium…[/]")
            console.print("[dim]  约 187MB，装到 ~/.cache/ms-playwright，"
                          "不需要 root、不动系统包[/]")
            okdl, detail = _install_playwright_chromium()
            if okdl:
                console.print(f"[green]✓[/] 已就绪: {detail}")
            else:
                err.print(f"[red]✗ 下载失败:[/] {detail}")
                raise typer.Exit(1)
        elif exe and src == "playwright 缓存":
            console.print(f"[dim]  用 playwright 缓存的 chromium: {exe}[/]")
        console.print("[bold]本机[/] 启动浏览器…")
        out = _local_browser("up", port)
    else:
        _browser_ensure_script(rr, h)
        console.print(f"[bold]{h.name}[/] 上启动浏览器…")
        p = rr.ssh_exec(h, f"BU_PORT={port} {h.workdir}/browser.sh up", timeout=120)
        out = (getattr(p, "stdout", "") or "").strip()

    if out.startswith(("STARTED", "ALREADY")):
        console.print(f"[green]✓[/] {out}")
        where = "本机" if h is None else f"远端 {h.name}"
        console.print(f"  {where}的 CDP 端点: http://127.0.0.1:{port}"
                      f"{'（远端 localhost，不经网络暴露）' if h else ''}")
        console.print("  browser agent 会自动连它 —— 不用手工配")
    else:
        err.print(f"[red]✗ 启动失败[/]\n{out[-500:]}")
        err.print("\n  常见原因：机器上没有 Chrome（snap 版连不上，要 .deb 版）；"
                  "或端口被占。")
        raise typer.Exit(1)


@browser_app.command("down")
def browser_down(
    host: str | None = typer.Option(None, "--host"),
    local: bool = typer.Option(False, "--local"),
) -> None:
    """停掉浏览器。任务做完就停，别让它空转占内存。"""
    cfg = _cfg()
    h, rr = _browser_host(cfg, host, local=local)
    if h is None:
        out = _local_browser("down")
    else:
        _browser_ensure_script(rr, h)
        p = rr.ssh_exec(h, f"{h.workdir}/browser.sh down", timeout=60)
        out = (getattr(p, "stdout", "") or "").strip()
    console.print(f"[green]✓[/] {out}" if "STOPPED" in out else f"[yellow]{out}[/]")


@browser_app.command("status")
def browser_status(
    host: str | None = typer.Option(None, "--host"),
    local: bool = typer.Option(False, "--local"),
) -> None:
    """看浏览器在不在跑（本机 + 远端都看）。"""
    cfg = _cfg()
    from .backends import _find_chrome

    rows = []
    rows.append(("本机", _local_browser("status"), bool(_find_chrome())))
    if cfg.hosts:
        h, rr = _browser_host(cfg, host, local=False)
        if h is not None:
            _browser_ensure_script(rr, h)
            p = rr.ssh_exec(h, f"{h.workdir}/browser.sh status", timeout=60)
            rows.append((h.name, (getattr(p, "stdout", "") or "").strip(), True))

    for name, out, has_chrome in rows:
        if out.startswith("UP"):
            console.print(f"[green]✓[/] {name}: {out}")
        elif not has_chrome:
            console.print(f"[dim]·[/] {name}: 没装 Chrome")
        else:
            console.print(f"[yellow]·[/] {name}: 未运行 —— 用 `browser up` 启动")

# ── config：凭据 ──────────────────────────────────────────────────────────
@cfg_app.command("apply")
def config_apply(
    file: Path | None = typer.Option(None, "--file", "-f",
                                        help="从文件读；省略则读 stdin"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示会写什么，不动手"),
) -> None:
    """写入凭据 —— API 密钥、base url、SSH 账号密码。

    **从 stdin 读一段 JSON**，不走命令行参数：密钥出现在 argv 里会进
    `ps aux` 和 shell 历史，这是实测踩过的泄露途径。

    格式（字段都可省，给多少写多少）：

    ```json
    {
      "providers": {
        "deepseek": {
          "api_key": "sk-...",
          "base_url": "https://api.deepseek.com/v1",
          "anthropic_base_url": "https://api.deepseek.com/anthropic"
        }
      },
      "hosts": {
        "osboxes": {
          "host": "192.0.2.10", "user": "root",
          "password": "...", "port": 22, "workdir": "/opt/commander"
        }
      }
    }
    ```

    写到哪：密钥与 base_url → `bin/.env`（600 + gitignored）；
    主机信息 → `remote/hosts.toml`。**密钥绝不写进 config/*.toml**。
    """
    cfg = _cfg()
    from .credentials import CredentialError, apply_update, parse_stdin

    try:
        raw = file.read_text(encoding="utf-8") if file else sys.stdin.read()
    except OSError as exc:
        err.print(f"[red]读不到 {file}: {exc}[/]")
        raise typer.Exit(2) from None

    try:
        update = parse_stdin(raw)
        if update.is_empty():
            err.print("[yellow]输入里没有任何凭据字段，什么都没做。[/]")
            raise typer.Exit(0)
        log = apply_update(cfg.ws, update, dry_run=dry_run)
    except CredentialError as exc:
        err.print(f"[red]✗ {exc}[/]")
        raise typer.Exit(2) from None

    for line in log:
        console.print(f"  {line}")
    if dry_run:
        console.print("\n[dim]--dry-run，未写入任何文件。[/]")
        return
    console.print(f"\n[green]✓[/] 已写入 [bold]{cfg.ws.dotenv.relative_to(cfg.ws.root)}[/]"
                  f"（权限 {oct(cfg.ws.dotenv.stat().st_mode)[-3:]}）")
    console.print("  跑 `./.commander/cmd doctor` 确认生效。")


@cfg_app.command("show")
def config_show() -> None:
    """显示当前凭据状态。**密钥一律打码**，可以安全贴给别人看。"""
    cfg = _cfg()
    from .credentials import describe

    t = Table(title="凭据状态（密钥已打码）", show_header=True, header_style="bold")
    for c in ("类别", "名称", "说明"):
        t.add_column(c)
    for kind, name, detail in describe(cfg.ws):
        t.add_row(kind, name, detail)
    console.print(t)
    console.print("\n[dim]写入用 `config apply`（从 stdin 读 JSON），"
                  "详见 --help[/]")


@cfg_app.command("check")
def config_check() -> None:
    """验证凭据是否真的能用 —— 打一次最小的 API 请求。"""
    cfg = _cfg()

    ok_any = False
    for name, p in cfg.providers.items():
        if not p.enabled:
            continue
        key = p.api_key()
        if not key:
            console.print(f"  [yellow]⚠[/] {name}: 缺密钥（{p.api_key_env}）")
            continue

        base = p.effective_base_url()
        if not base:
            console.print(f"  [dim]·[/] {name}: 有密钥，无 OpenAI 兼容端点，跳过连通性检查")
            ok_any = True
            continue

        # 用标准库而不是 httpx —— 引擎的基础依赖里没有 httpx（它只在
        # openai_compat 后端自己的 venv 里）。为了一句健康检查给引擎加个
        # 依赖不划算，urllib 够用。
        url = base.rstrip("/") + "/models"
        # 只允许 http(s)：base_url 来自配置，配置可能被改错或被人塞进
        # file:// 之类的 scheme。urlopen 本身不挑协议，得我们自己拦。
        if not url.startswith(("http://", "https://")):
            console.print(f"  [red]✗[/] {name}: base_url 不是 http(s) —— {url}")
            continue
        try:
            # scheme 已在上方白名单校验过，这里只可能是 http(s)
            import urllib.error
            import urllib.request

            req = urllib.request.Request(  # noqa: S310
                url, headers={"Authorization": f"Bearer {key}"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                code = resp.status
            console.print(f"  [green]✓[/] {name}: HTTP {code}  {url}")
            ok_any = True
        except urllib.error.HTTPError as exc:
            # 401/403 = 密钥不对；404 = 端点不对但服务在线。都要如实报
            mark = "[red]✗[/]" if exc.code in (401, 403) else "[yellow]⚠[/]"
            hint = "（密钥可能不对）" if exc.code in (401, 403) else ""
            console.print(f"  {mark} {name}: HTTP {exc.code}{hint}  {url}")
        except Exception as exc:
            console.print(f"  [red]✗[/] {name}: {type(exc).__name__}: {str(exc)[:80]}")

    for name, h in cfg.hosts.items():
        from .guard import Guard
        from .ssh_runner import RemoteRunner
        rr = RemoteRunner(cfg, Guard(cfg.ws, cfg.policy.guard))
        ok, detail = rr.check(h)
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        console.print(f"  {mark} host {name}: {detail}")

    raise typer.Exit(0 if ok_any else 1)


# ══════════════════════════════════════════════════════════════════════════
def main() -> None:
    try:
        app()
    except GuardViolation as exc:
        err.print(f"[red]目录契约违规[/] ({exc.rule}): {exc}")
        sys.exit(3)
    except WorkspaceNotFound as exc:
        err.print(f"[red]{exc}[/]")
        sys.exit(2)


if __name__ == "__main__":
    main()
