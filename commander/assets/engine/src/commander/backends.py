"""后端进程的启动与通信。

每个 SDK 一个独立 uv 工程（bin/backends/<sdk>/），因为实测它们**无法共存**：
    crewai >=1.15.22  要求 pydantic >=2.11.9,<2.13
    hermes-agent 0.19.0 要求 pydantic ==2.13.4     ← 硬冲突

所以不能用一个 venv 装所有 SDK。隔离成独立工程反而是好事：
依赖互不污染、可按需安装、能整份同步到远端执行。

启动命令形如：
    uv run --project bin/backends/crewai python bin/backends/crewai/runner.py

进程 cwd 被 guard 钉死在 agents/<id>/work/ —— 这是最可靠的一道隔离。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .guard import Guard
from .sandbox import Sandbox
from .schemas import Event, RunSpec

# 后端 → (目录名, 需要的额外说明)
BACKEND_DIRS = {
    "claude": "claude",
    "openai": "openai",
    "langchain": "langchain",
    "crewai": "crewai",
    "autogen": "autogen",
    "hermes": "hermes",
    "browser_use": "browser_use",
    "openai_compat": "openai_compat",
    "mock": "mock",
}


class BackendError(RuntimeError):
    pass


def _assert_registry_in_sync() -> None:
    """BACKEND_DIRS 与 config.BACKENDS 必须一致。

    ⚠️ 实测踩过：加了新后端只改了 config.BACKENDS 而忘了 BACKEND_DIRS，
    结果 `backend list` 里看不到它、派发时报「未知后端」——
    而两处代码隔了老远，改的人（我）压根没想到还有第二份清单。

    这种「同一事实维护两份」的结构必然漂移。加个启动断言，
    让它**在导入时就炸**，而不是等到派发时才莫名其妙地失败。
    """
    from .config import BACKENDS as _BACKENDS
    missing = set(_BACKENDS) - set(BACKEND_DIRS)
    extra = set(BACKEND_DIRS) - set(_BACKENDS)
    if missing or extra:
        raise BackendError(
            f"后端清单漂移 —— config.BACKENDS 与 BACKEND_DIRS 不一致。"
            f"仅在 config 里: {sorted(missing)}；仅在 DIRS 里: {sorted(extra)}。"
            f"两处都要改。"
        )


_assert_registry_in_sync()


@dataclass
class LaunchResult:
    exit_code: int
    events: list[Event]
    stderr_lines: list[str]
    duration_s: float
    timed_out: bool = False


def uv_binary() -> str:
    p = shutil.which("uv")
    if not p:
        raise BackendError(
            "找不到 uv。本项目全部 Python 调用依赖 uv（本机没有 pip）。"
            "安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
        )
    return p


class BackendLauncher:
    """启动后端进程，把 stdout 的 JSONL 事件流解析出来。"""

    def __init__(self, ws, guard: Guard) -> None:
        self.ws = ws
        self.guard = guard
        self.sandbox = Sandbox(ws, getattr(guard.policy, "sandbox", "auto"))

    # ── 工程位置与就绪检查 ────────────────────────────────────────────
    def project_dir(self, backend: str) -> Path:
        if backend not in BACKEND_DIRS:
            raise BackendError(
                f"未知后端 {backend!r}。已注册: {sorted(BACKEND_DIRS)}"
            )
        return self.ws.backends_dir / BACKEND_DIRS[backend]

    def is_installed(self, backend: str) -> bool:
        d = self.project_dir(backend)
        return (d / "runner.py").is_file() and (d / ".venv").is_dir()

    def ensure_installed(self, backend: str, *, sync: bool = True) -> None:
        """确保后端工程存在且依赖已装。首次会 uv sync（可能较慢）。"""
        d = self.project_dir(backend)
        if not (d / "runner.py").is_file():
            raise BackendError(
                f"后端 {backend!r} 尚未生成（缺 {d / 'runner.py'}）"
            )
        if (d / ".venv").is_dir():
            return
        if not sync:
            raise BackendError(
                f"后端 {backend!r} 依赖未安装。先跑：\n"
                f"  uv sync --project {d}"
            )
        log = self.ws.logs_dir
        log.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [uv_binary(), "sync", "--project", str(d)],
            capture_output=True, text=True, timeout=1800,
        )
        (log / f"backend-sync-{backend}.log").write_text(
            f"$ uv sync --project {d}\n\n{proc.stdout}\n{proc.stderr}",
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise BackendError(
                f"后端 {backend!r} 依赖安装失败（详见 logs/backend-sync-{backend}.log）:\n"
                f"{proc.stderr[-2000:]}"
            )

    # ── 启动 ──────────────────────────────────────────────────────────
    def command(self, backend: str, spec: RunSpec) -> list[str]:
        d = self.project_dir(backend)
        return [
            uv_binary(), "run",
            "--project", str(d),
            "--no-sync",                      # 已 ensure_installed，避免每次重解析
            "python", str(d / "runner.py"),
        ]

    def sandboxed_command(
        self, backend: str, spec: RunSpec, workdir: Path
    ) -> list[str]:
        """命令 + 沙箱包装。

        注意：uv 需要一个可写的 cache 目录，而沙箱把全局挂成只读 ——
        所以 HOME 指向 agent 自己的 .home（已在 sandbox 计划里挂为可写），
        uv 的 cache 就落在那里，不污染宿主。
        """
        argv = self.command(backend, spec)
        if not self.sandbox.available:
            if self.guard.policy.sandbox == "bwrap":
                raise BackendError(
                    "policy.guard.sandbox = 'bwrap' 但系统中找不到 bwrap。"
                    "请安装 bubblewrap，或把该值改为 'auto'/'none'。"
                )
            return argv
        return self.sandbox.wrap(argv, spec.agent_id, workdir)

    def launch(
        self,
        backend: str,
        spec: RunSpec,
        *,
        on_event=None,
        on_stderr=None,
    ) -> LaunchResult:
        """跑一次派发。

        事件是**流式**回调出去的 —— 后端跑到一半崩了，
        已经产生的事件也已经落到调用方的记录器里了。
        """
        workdir = self.guard.resolve_workdir(spec.agent_id)
        argv = self.sandboxed_command(backend, spec, workdir)
        self.guard.audit_argv(argv, actor=spec.agent_id, cwd=workdir)

        env = self.guard.sandbox_env(spec.agent_id, workdir)
        env["PYTHONUNBUFFERED"] = "1"
        # uv 的 cache 落到 agent 自己的 HOME 里 —— 沙箱下全局是只读的
        env.setdefault("UV_CACHE_DIR", str(self.ws.agent_dir(spec.agent_id) / ".home" / ".cache" / "uv"))

        start = time.time()
        events: list[Event] = []
        stderr_lines: list[str] = []
        timed_out = False

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workdir),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        try:
            assert proc.stdin is not None
            proc.stdin.write(spec.wire() + "\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        # ⚠️ 读循环必须自己有截止时间，不能只靠 proc.wait 的超时。
        #
        # 实测踩过的坑：browser-use 会拉起 Chrome 子进程，**Chrome 继承了
        # stdout 管道**。runner 退出后 Chrome 还活着，管道两端都开着，
        # 于是 `for line in proc.stdout` 永远读不到 EOF —— 结果事件早就收到了，
        # 进程却卡在那不动，outbox 一直写不出来。
        #
        # 用「读线程 + 队列」而不是 selectors：
        # TextIOWrapper 有自己的缓冲，fd 层的 select 看不到缓冲里的数据，
        # 两者混用会出各种诡异问题。线程读取 + 队列投递绕开整个缓冲层，
        # 且事件处理仍只在主线程做（recorder 不是线程安全的）。
        import queue
        import threading

        q: queue.Queue = queue.Queue()
        EOF = object()

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    q.put(raw)
            except Exception:
                pass
            finally:
                q.put(EOF)

        threading.Thread(target=_reader, daemon=True,
                         name=f"reader-{spec.agent_id}").start()

        deadline = start + spec.timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                timed_out = True
                break
            try:
                line = q.get(timeout=min(remain, 5.0))
            except queue.Empty:
                if time.time() >= deadline:
                    timed_out = True
                    break
                if proc.poll() is not None and q.empty():
                    break          # 进程退了、队列也空了 → 收工
                continue

            if line is EOF:
                break              # 正常结束
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # 非 JSON 输出当日志，不能丢 —— 往往是 SDK 的报错
                if on_stderr:
                    on_stderr(line.rstrip())
                continue
            try:
                ev = Event.model_validate(obj)
            except Exception:
                if on_stderr:
                    on_stderr(f"[protocol] 无法解析的事件: {line[:300]}")
                continue
            events.append(ev)
            if on_event:
                on_event(ev)

        # 收尾：不管从上面哪条路退出来的，都要确保子进程不会变成孤儿。
        # 之前漏了这段，结果是超时中断后 ssh 进程一直挂着。
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)

        return LaunchResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            events=events,
            stderr_lines=stderr_lines,
            duration_s=time.time() - start,
            timed_out=timed_out,
        )

    # ── 环境自检 ──────────────────────────────────────────────────────
    def probe_env(self, backend: str) -> dict:
        """在指定后端 venv 里跑一句 python，确认依赖真的能 import。

        比 is_installed 更进一步 —— .venv 存在不等于依赖装全了。
        """
        d = self.project_dir(backend)
        if not d.is_dir():
            return {"backend": backend, "ok": False, "error": "工程目录不存在"}
        probe = d / "_probe.py"
        if not probe.is_file():
            return {"backend": backend, "ok": False, "error": "缺 _probe.py"}
        try:
            proc = subprocess.run(
                [uv_binary(), "run", "--project", str(d), "--no-sync",
                 "python", str(probe)],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            return {"backend": backend, "ok": False, "error": "探测超时"}
        ok = proc.returncode == 0
        return {
            "backend": backend,
            "ok": ok,
            "detail": (proc.stdout or proc.stderr).strip()[:500],
        }


# ══════════════════════════════════════════════════════════════════════════
# 后端就绪度 —— doctor 与 patrol 共用的唯一实现
# ══════════════════════════════════════════════════════════════════════════


def _sync_hint(launcher: BackendLauncher, backend: str) -> str:
    """给出「怎么装这个后端」的命令，路径要真的对得上。

    ⚠️ 两个坑：
      · 不能写死 `bin/backends/<b>` —— 内嵌布局下引擎在 `.commander/bin/`，
        那句话会把人指向一个不存在的路径
      · 也不能只用「相对工作区根」的路径 —— 内嵌布局下工作区根**就是**
        `.commander/`，相对路径会把这一层前缀吞掉，结果同上

    所以统一算「相对项目根」的路径（项目根 = 内嵌时的工作区父目录），
    那正是技能里所有命令的锚点。
    """
    from .workspace import EMBED_DIR

    d = launcher.project_dir(backend)
    root = launcher.ws.root
    project_root = root.parent if root.name == EMBED_DIR else root

    for base in (Path.cwd(), project_root):
        try:
            return f"uv sync --project {d.relative_to(base)}"
        except ValueError:
            continue
    return f"uv sync --project {d}"



def _prereq_problem(backend: str, ws, host, rr, remote_ok: bool) -> str | None:
    """检查后端有没有「依赖装了但跑不起来」的前置条件问题。

    browser_use 是唯一一个：它**不下载浏览器**，需要一个已运行的 Chrome。
    .venv 在不在跟能不能跑完全是两回事 —— 实测本机装了 228M 的依赖，
    但机器上压根没有 Chrome，派发时才炸。

    返回问题描述；没问题返回 None。
    """
    if backend != "browser_use":
        return None

    from .config import HEAVY_BACKENDS  # noqa: F401  (保持导入语义清晰)
    local_chrome = _find_chrome()
    remote_chrome = None
    if host is not None and remote_ok and rr is not None:
        remote_chrome = _remote_has_chrome(ws, host, rr)

    parts = []
    if not local_chrome:
        parts.append("本机无 Chrome")
    if remote_chrome is False:
        parts.append(f"远端 {host.name} 无 Chrome")

    if remote_chrome is True:
        return None
    if not local_chrome and remote_chrome is not True:
        return ("缺 Chrome —— browser_use 不下载浏览器，必须连一个已运行的 Chrome。"
                "先 `./.commander/cmd browser up`，"
                "或确认远端装了 .deb 版 Chrome（snap 版连不上 DevTools）")
    return None


def _find_chrome() -> str | None:
    """本机有没有可用的 Chromium 系浏览器。

    两档：系统装的优先，其次 playwright 缓存的 chromium。
    第二档很重要 —— browser-use **自己不下载浏览器**，但 `uvx playwright
    install chromium` 装的那个它认得（它的 _find_installed_browser_path
    把 playwright 缓存的优先级排得比系统 Chrome 还高）。
    实测：本机无任何系统浏览器，装了 playwright chromium 后它能直接用。
    """
    import glob
    import shutil

    for b in ("google-chrome-stable", "google-chrome", "chromium",
              "chromium-browser", "chrome", "microsoft-edge"):
        if p := shutil.which(b):
            return p
    for p in ("/opt/google/chrome/chrome", "/usr/lib/chromium/chromium"):
        if Path(p).exists():
            return p
    cache = Path.home() / ".cache" / "ms-playwright"
    for pat in ("chromium-*/chrome-linux*/chrome",
                "chromium_headless_shell-*/chrome-linux*/headless_shell"):
        if hits := sorted(glob.glob(str(cache / pat))):
            return hits[-1]
    return None


def _remote_has_chrome(ws, host, rr) -> bool | None:
    """远端有没有 Chrome。结果缓存，避免每次巡检都 ssh 一次。"""
    cache = ws.remote_dir / f".chrome-{host.name}"
    if cache.is_file():
        return cache.read_text().strip() == "yes"
    try:
        p = rr.ssh_exec(
            host,
            "for b in google-chrome-stable google-chrome chromium chromium-browser; do "
            "command -v $b >/dev/null 2>&1 && { echo YES; exit 0; }; done; echo NO",
            timeout=45,
        )
    except Exception:
        return None
    out = (getattr(p, "stdout", "") or "").strip()
    if "YES" in out:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("yes", encoding="utf-8")
        return True
    if "NO" in out:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("no", encoding="utf-8")
        return False
    return None

def backend_readiness(cfg, *, check_remote: bool = True) -> list[dict]:
    """查每个在用的后端到底能不能跑。

    关键点：**重后端本来就该在远端跑**。只看本地会得出"未安装，建议本地装"
    这种把人引向错误方向的结论 —— crewai 136 个包，本地 2Gi 根本装不下。

    返回 [{"backend", "ok", "where", "detail"}]。
    """
    from .config import HEAVY_BACKENDS
    from .guard import Guard

    ws = cfg.ws
    launcher = BackendLauncher(ws, Guard(ws, cfg.policy.guard))
    host = cfg.default_host() if check_remote else None

    rr = None
    remote_ok = False
    if host is not None:
        try:
            from .ssh_runner import RemoteRunner
            rr = RemoteRunner(cfg, Guard(ws, cfg.policy.guard))
            remote_ok, _ = rr.check(host)
        except Exception:
            rr, remote_ok = None, False

    out: list[dict] = []
    for b in sorted({a.backend for a in cfg.agents.values()}):
        # 两个字段各司其职，**不要混用**：
        #   ok       这个后端现在能用吗（决定 UI 显示"就绪"还是"未装"）
        #   expected 没装是正常的吗（决定巡检要不要报警）
        #
        # ⚠️ 实测踩过：一度把"没装是正常的"写成 ok=True，结果
        #    `backend list` 把没装的重后端也报成「就绪」——
        #    用户照着它以为能用，实际派发就炸。这是子 agent 在真机上发现的。
        entry = {"backend": b, "ok": False, "where": "local", "detail": "",
                 "expected": True}
        try:
            local_ready = launcher.is_installed(b)
        except Exception as exc:
            entry["detail"] = str(exc)[:80]
            out.append(entry)
            continue

        if b not in HEAVY_BACKENDS:
            entry.update(ok=local_ready,
                         detail="已就绪" if local_ready
                         else f"未安装：{_sync_hint(launcher, b)}")
            out.append(entry)
            continue

        # 重依赖里有一类额外要求系统级东西（browser_use 要 Chrome）。
        # 光看 .venv 在不在会误判成"已就绪"，而它其实根本跑不起来。
        prereq = _prereq_problem(b, ws, host, rr, remote_ok)
        if prereq:
            entry.update(ok=False, detail=prereq)
            out.append(entry)
            continue

        # 重依赖：看远端
        entry["where"] = "remote"
        if local_ready:
            entry.update(ok=True, where="local",
                         detail="本地已装（但重依赖，建议走远端）")
        elif host is None:
            # 压根没配远端 —— 重后端没装是**预期状态**，不是故障。
            # 全新安装的工作区默认就没有远端，此时报警纯属噪声。
            # 但它依然**不能用**，所以 ok=False、expected=False。
            entry.update(ok=False, expected=False,
                         detail="未装（重依赖，需要时再 remote bootstrap）")
        elif remote_ok and rr is not None:
            # 哨兵必须互不为子串 —— 详见 ssh_runner 里的同款说明
            chk = rr.ssh_exec(
                host,
                f"test -f {host.workdir}/.ready-{b} && echo __R__ || echo __N__",
                timeout=45,
            )
            if "__R__" in (getattr(chk, "stdout", "") or ""):
                entry.update(ok=True, detail=f"远端 {host.name} 已就绪")
            else:
                entry["detail"] = f"远端未装：commander remote sync {b}"
        else:
            # 配了远端但连不上 —— 这才是真问题
            entry["detail"] = f"远端 {host.name} 不可达，重后端暂时用不了"
        out.append(entry)

    return out
