"""派发核心 —— 指挥官调用 Python 把任务交出去的唯一入口。

用户需求原文：「所有派发的agent都是已指挥官调用python的形式派发下去」

一次派发的完整链路：
    路由 → 契约检查 → 装载技能 → 构造规格 → 启动后端 → 流式留痕 → 收尾

每一步都会留痕。派发失败也要留下失败的原因，那是下次决策的依据。
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from dataclasses import dataclass, field

from .backends import BackendError, BackendLauncher
from .budget import StatusReport
from .config import Config
from .guard import Guard, GuardViolation
from .record import RunRecorder
from .router import Route, Router, RoutingError
from .schemas import (
    Event,
    RunResult,
    RunSpec,
    Usage,
    classify_error,
)
from .skills import SkillLibrary
from .workspace import Workspace


@dataclass
class DispatchRequest:
    """指挥官发起一次派发时给的东西。"""

    agent_id: str
    prompt: str
    task_id: str | None = None
    model: str | None = None            # 覆盖 agent 默认模型
    target: str | None = None           # local | remote | auto
    host: str | None = None
    skills: list[str] | None = None     # 覆盖 agent 默认技能
    extra_skills: list[str] = field(default_factory=list)   # 在默认技能上加
    system_prompt: str = ""
    max_tokens: int | None = None
    timeout: int | None = None
    options: dict = field(default_factory=dict)
    # 是否要求 agent 附状态块。默认开 —— 「消耗大进展小」的判断依赖它
    report_progress: bool = True


class DispatchError(RuntimeError):
    pass


class Commander:
    """指挥官的派发器。一个进程内复用一个实例。"""

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.ws: Workspace = self.cfg.ws
        self.guard = Guard(self.ws, self.cfg.policy.guard)
        self.router = Router(self.cfg)
        self.skills = SkillLibrary(self.ws)
        self.launcher = BackendLauncher(self.ws, self.guard)
        self.ws.ensure_all()

    # ══════════════════════════════════════════════════════════════════
    # 主入口
    # ══════════════════════════════════════════════════════════════════
    def dispatch(
        self,
        req: DispatchRequest,
        *,
        on_event=None,
        on_stderr=None,
        quiet: bool = False,
    ) -> tuple[RunResult, Route, str]:
        """派发一次任务。返回 (结果, 路由, outbox 相对路径)。"""
        task_id = req.task_id or self._new_task_id(req.agent_id)

        # ① 路由
        route = self.router.route(
            req.agent_id, model_alias=req.model,
            target=req.target, host_name=req.host,
        )

        # ② 技能
        skill_names = self._resolve_skills(route, req)
        metas = self.skills.load_many(skill_names, strict=False)
        system_prompt = self.skills.compose_system_prompt(req.system_prompt, metas)

        # ③ max_tokens
        max_tokens = self.router.resolve_max_tokens(
            route.agent, route.model, req.max_tokens
        )

        # ④ 构造规格
        spec = self._build_spec(
            req, route, task_id, system_prompt, max_tokens,
            self.skills.to_specs(metas),
        )

        if not quiet:
            print(f"  ⟶ {route.explain()}", file=__import__("sys").stderr)

        # ⑤ 执行（含重试）
        attempts = self.cfg.policy.retry.max_attempts
        last: RunResult | None = None

        for attempt in range(1, attempts + 1):
            result, outbox = self._execute(spec, route, on_event, on_stderr)
            last = result

            if result.ok or not result.retryable or attempt == attempts:
                break

            delay = self.cfg.policy.retry.backoff_base * (2 ** (attempt - 1))
            if not quiet:
                print(
                    f"  ↻ 第 {attempt} 次失败（{result.error_kind}），{delay}s 后重试",
                    file=__import__("sys").stderr,
                )
            time.sleep(delay)
            # 重试要换 run_id，否则记录文件会混在一起
            spec = spec.model_copy(update={"run_id": _run_id()})

        assert last is not None
        self._after_dispatch(task_id, route, last, outbox, quiet=quiet)
        return last, route, outbox

    # ══════════════════════════════════════════════════════════════════
    # 内部
    # ══════════════════════════════════════════════════════════════════
    def _new_task_id(self, agent_id: str) -> str:
        return f"{time.strftime('%m%d')}-{agent_id}-{uuid.uuid4().hex[:6]}"

    def _resolve_skills(self, route: Route, req: DispatchRequest) -> list[str]:
        if req.skills is not None:
            return list(req.skills)
        return list(dict.fromkeys([*route.agent.skills, *req.extra_skills]))

    def _build_spec(
        self,
        req: DispatchRequest,
        route: Route,
        task_id: str,
        system_prompt: str,
        max_tokens: int,
        skill_specs,
    ) -> RunSpec:
        workdir = self.guard.resolve_workdir(route.agent.id)
        self.ws.ensure_agent_dirs(route.agent.id)

        # 让 agent 在回复末尾附一段结构化状态块（完成度/置信度/障碍）。
        # 只有它自己知道是在原地打转还是在收敛 —— 机械信号看不出这个。
        # 关掉的方式：--no-progress（见 DispatchRequest.report_progress）
        if req.report_progress:
            from .budget import status_instruction
            system_prompt = (system_prompt + "\n\n---\n\n"
                             + status_instruction()).strip()

        return RunSpec(
            run_id=_run_id(),
            task_id=task_id,
            agent_id=route.agent.id,
            prompt=req.prompt,
            system_prompt=system_prompt,
            skills=skill_specs,
            models=[self.router.to_spec(route.model)],
            max_tokens=max_tokens,
            workdir=str(workdir),
            workspace_root=str(self.ws.root),
            timeout=req.timeout or route.agent.timeout or self.cfg.policy.budget.default_timeout,
            max_turns=route.agent.max_turns,
            options={**route.agent.options, **req.options},
        )

    def _execute(
        self,
        spec: RunSpec,
        route: Route,
        on_event,
        on_stderr,
    ) -> tuple[RunResult, str]:
        """跑一次，全程留痕。契约违规和启动失败也返回 RunResult，不抛。"""
        recorder = RunRecorder(self.ws, spec)
        t0 = time.time()

        def relay(ev: Event) -> None:
            recorder.event(ev)
            if on_event:
                on_event(ev)

        def relay_err(line: str) -> None:
            recorder.stderr(line)
            if on_stderr:
                on_stderr(line)

        try:
            if route.remote:
                result = self._execute_remote(spec, route, relay, relay_err)
            else:
                result = self._execute_local(spec, route, relay, relay_err)
        except GuardViolation as exc:
            result = RunResult(
                ok=False, error=str(exc), error_kind="guard_violation",
                retryable=False, model=route.model.model,
                backend=route.agent.backend,
                raw={"rule": exc.rule, "path": exc.path},
            )
            recorder.event(Event(type="error", data={
                "message": f"目录契约违规: {exc}", "rule": exc.rule
            }))
        except (BackendError, RoutingError, DispatchError) as exc:
            kind, retryable = classify_error(exc)
            result = RunResult(
                ok=False, error=str(exc), error_kind=kind, retryable=retryable,
                model=route.model.model, backend=route.agent.backend,
            )
            recorder.event(Event(type="error", data={"message": str(exc)}))
        except Exception as exc:
            kind, retryable = classify_error(exc)
            result = RunResult(
                ok=False, error=f"{type(exc).__name__}: {exc}",
                error_kind=kind, retryable=retryable,
                model=route.model.model, backend=route.agent.backend,
            )
            recorder.event(Event(type="error", data={"message": str(exc)}))

        result.duration_s = result.duration_s or (time.time() - t0)
        result.model = result.model or route.model.model
        result.backend = result.backend or route.agent.backend

        # 剥状态块、算成本 —— **必须在 finalize 之前**，否则写进 outbox 的
        # 结果里没有这些字段（实测踩过：raw.cost 是 None）。
        self._enrich(result, route)

        outbox = recorder.finalize(result)
        return result, str(outbox.relative_to(self.ws.root))

    def _enrich(self, result: RunResult, route: Route) -> None:
        """给结果补上「预算系统需要的现场信息」。

        两件事，都放在派发层做（后端有 8 个，逻辑只该有一份）：
          ① 剥出 agent 自报的状态块，并从正文里删掉 —— 那是给指挥官看的
             元信息，不该混进交付物
          ② 算出这次派发的四维消耗，写进 result.raw

        调用时机很关键：必须在 recorder.finalize() **之前**。
        """
        from .budget import CostModel, parse_status_block

        # ① 状态块
        data, clean = parse_status_block(result.text or "")
        if data is not None:
            result.text = clean
            result.raw["declared_progress"] = data

        # ② 四维消耗
        cm = CostModel(self.cfg)
        cost_units, cost_usd, priced = cm.of(result.model, result.usage)
        cores = self._cores_for(route)
        result.raw["cost"] = {
            "cost_units": round(cost_units, 6),
            "cost_usd": round(cost_usd, 6) if priced else None,
            "priced": priced,
            "core_seconds": round(result.duration_s * cores, 2),
            "cores": cores,
        }

    def _execute_local(self, spec: RunSpec, route: Route, relay, relay_err) -> RunResult:
        self.launcher.ensure_installed(route.agent.backend)
        launch = self.launcher.launch(
            route.agent.backend, spec, on_event=relay, on_stderr=relay_err
        )
        return self._collect(launch, route)

    def _execute_remote(self, spec: RunSpec, route: Route, relay, relay_err) -> RunResult:
        from .ssh_runner import RemoteRunner
        runner = RemoteRunner(self.cfg, self.guard)
        host = self.cfg.hosts[route.host_name]  # type: ignore[index]
        return runner.run(host, route.agent.backend, spec, relay, relay_err)

    def _collect(self, launch, route: Route) -> RunResult:
        """把后端进程的产出收敛成 RunResult。"""
        # 优先用后端显式给出的 result 事件
        for ev in reversed(launch.events):
            if ev.type == "result":
                try:
                    return RunResult.model_validate(ev.data)
                except Exception:
                    pass

        # 没有 result 事件 → 拼一个能说明问题的失败结果
        if launch.timed_out:
            return RunResult(
                ok=False, error_kind="timeout", retryable=True,
                error=f"后端 {route.agent.backend} 超过 {route.agent.timeout}s 未完成",
                backend=route.agent.backend, model=route.model.model,
            )

        text = "".join(
            str(e.data.get("text", "")) for e in launch.events if e.type == "text"
        )
        usage = Usage()
        for e in launch.events:
            if e.type == "usage":
                with contextlib.suppress(Exception):
                    usage = usage.merge(Usage.model_validate(e.data))

        tail = "\n".join(launch.stderr_lines[-15:])
        return RunResult(
            ok=False,
            text=text,
            usage=usage,
            error_kind="no_result",
            retryable=False,
            error=(
                f"后端 {route.agent.backend} 退出码 {launch.exit_code}，"
                f"未产出 result 事件。stderr 尾部：\n{tail}"
            ),
            backend=route.agent.backend,
            model=route.model.model,
        )

    def _after_dispatch(self, task_id: str, route: Route, result: RunResult,
                        outbox: str, *, quiet: bool = False,
                        ) -> StatusReport | None:
        """派发后置动作：算成本、记流水、评估预算。

        返回状态报告（如果做了评估）。**任何异常都不能影响派发结果的返回** ——
        预算系统是辅助决策的，它自己出问题不该让任务失败。
        """
        try:
            return self._record_and_evaluate(task_id, route, result, outbox,
                                              quiet=quiet)
        except Exception:
            return None

    def _record_and_evaluate(self, task_id: str, route: Route, result: RunResult,
                             outbox: str, *, quiet: bool = False,
                             ) -> StatusReport | None:
        from . import budget as B
        from .tasks import TaskRegistry

        # ── ① 成本与算力（_enrich 已算好，写进 raw 了）──────────────────
        cost = (result.raw or {}).get("cost") or {}
        cost_units = float(cost.get("cost_units") or 0.0)
        cost_usd = float(cost.get("cost_usd") or 0.0)
        core_seconds = float(cost.get("core_seconds") or 0.0)
        priced = bool(cost.get("priced"))

        # ── ② 进展：机械信号 + agent 自报，**两者都要** ────────────────
        # 机械信号（轮数/产出量/产物数）总是有；自报补上"是在收敛还是在绕圈"。
        # 早期版本用自报整个替换掉机械信号，结果 turns 全变成 0 —— 那是丢信息。
        prog = B.Progress(
            turns=result.turns,
            output_chars=len(result.text or ""),
            artifacts=len(result.artifacts or []),
        )
        declared = (result.raw or {}).get("declared_progress")
        if declared:
            d = B.progress_from_status(declared)
            prog.completion = d.completion
            prog.confidence = d.confidence
            prog.blockers = d.blockers
            prog.summary = d.summary
            prog.declared = True

        # ── ④ 记流水 ──────────────────────────────────────────────────
        TaskRegistry(self.ws).record_run(
            task_id=task_id,
            agent_id=route.agent.id,
            backend=route.agent.backend,
            model=result.model,
            ok=result.ok,
            usage_total=result.usage.total,
            duration_s=result.duration_s,
            outbox=outbox,
            error=result.error,
            cost_units=cost_units,
            cost_usd=cost_usd,
            core_seconds=core_seconds,
            priced=priced,
            progress=prog.to_dict(),
        )

        # ── ⑤ 评估并出报告 ────────────────────────────────────────────
        store = B.BudgetStore(self.ws, self.cfg)
        rep = store.evaluate_task(task_id, agent_id=route.agent.id, progress=prog)
        rep.progress = prog
        B.write_report(self.ws, rep)

        # 只有需要注意力时才打扰指挥官 —— 每轮都报会变成噪声
        if rep.needs_attention and not quiet:
            self._print_report(rep)
        return rep

    def _cores_for(self, route: Route) -> int:
        """估这次派发用了几核。本地取本机核数；远端取缓存过的探测值。"""
        if not route.remote:
            return os.cpu_count() or 1
        cache = self.ws.remote_dir / f".cores-{route.host_name}"
        if cache.is_file():
            try:
                return max(1, int(cache.read_text().strip()))
            except (OSError, ValueError):
                pass
        return os.cpu_count() or 1

    @staticmethod
    def _print_report(rep) -> None:
        import sys
        mark = {"ok": "·", "warn": "⚠", "critical": "❗", "exhausted": "⛔"}.get(
            rep.level, "·")
        print(f"  {mark} 预算 {rep.one_line()}", file=sys.stderr)
        for r in rep.reasons[:3]:
            print(f"      {r}", file=sys.stderr)


def _run_id() -> str:
    return f"{os.getpid():x}{uuid.uuid4().hex[:6]}"
