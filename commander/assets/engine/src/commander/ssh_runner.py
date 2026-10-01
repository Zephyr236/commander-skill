"""SSH 远端执行 —— 把 agent 放到算力更强的机器上跑。

用户需求原文：「其中还会包含使用ssh调用远程服务器完成的情况，也就是把agent放在服务器中运行」

为什么需要它：本机 2 核 / 可用 2Gi，而 crewai 一个后端就要装 136 个包。
远端实测 40 核 / 可用 20Gi —— 差 20 倍的算力，重后端必须落那边。

做法：
    1. bootstrap  首次连接：装 uv、建目录、装专用密钥（把密码认证换掉）
    2. sync       把 bin/backends/<sdk> + config + skills 同步上去
    3. run        ssh 执行 uv run，stdout 的 JSONL 事件流原样中继回本地
    4. collect    把远端 workdir 的产物 rsync 回来

关键点：**线协议完全不变**。远端跑的和本地跑的是同一个 runner.py，
所以指挥官无需知道这次是本地还是远端 —— 这正是把 SDK 差异收敛到线协议的好处。
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config, HostConfig
from .guard import Guard
from .schemas import Event, RunResult, RunSpec, classify_error
from .workspace import Workspace

# bootstrap 时在远端安装 uv
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"


class RemoteError(RuntimeError):
    pass


class RemoteRunner:
    def __init__(self, cfg: Config, guard: Guard) -> None:
        self.cfg = cfg
        self.guard = guard
        self.ws: Workspace = cfg.ws

    # ══════════════════════════════════════════════════════════════════
    # SSH 调用基础设施
    # ══════════════════════════════════════════════════════════════════
    def _use_key(self, host: HostConfig, force_password: bool) -> bool:
        """这次连接用密钥还是密码。

        注意 bootstrap 阶段必须强制密码：那时公钥还没装到远端，
        而密钥文件已经在本地了 —— 直接用密钥会 Permission denied，
        形成「装不上公钥 → 用不了密钥 → 装不上公钥」的死循环。
        """
        if force_password:
            return False
        return host.identity_path(self.ws) is not None

    def _ssh_base(self, host: HostConfig, force_password: bool = False) -> list[str]:
        """构造 ssh 命令前缀。

        认证优先级：专用密钥 > 密码。
        本机没有 sshpass，密码认证走 OpenSSH 自带的 SSH_ASKPASS 强制路径
        （需要 setsid 脱离 tty，否则 ssh 会忽略 SSH_ASKPASS）。
        """
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", f"Port={host.port}",
        ]
        if self._use_key(host, force_password):
            ident = host.identity_path(self.ws)
            # 允许回退到密码：密钥没装成时还能救回来，否则会锁死自己
            cmd += ["-i", str(ident), "-o", "IdentitiesOnly=yes",
                    "-o", "PreferredAuthentications=publickey,password",
                    "-o", "NumberOfPasswordPrompts=1"]
        else:
            cmd += ["-o", "PreferredAuthentications=password",
                    "-o", "PubkeyAuthentication=no",
                    "-o", "NumberOfPasswordPrompts=1"]
        cmd.append(host.ssh_target())
        return cmd

    def _needs_askpass(self, host: HostConfig, force_password: bool) -> bool:
        """密码是否会参与认证 —— 决定要不要挂 SSH_ASKPASS。"""
        if self._use_key(host, force_password):
            # 密钥优先但允许回退，所以仍要备好 askpass
            return host.password() is not None
        return True

    def _ssh_env(self, host: HostConfig, force_password: bool = False) -> dict[str, str]:
        """给 ssh 子进程准备环境。密码认证时注入 SSH_ASKPASS。"""
        env = dict(os.environ)
        if not self._needs_askpass(host, force_password):
            return env

        pw = host.password()
        if not pw:
            if not self._use_key(host, force_password):
                raise RemoteError(
                    f"主机 {host.name} 既无可用密钥，也没有密码"
                    f"（检查 {host.password_env}）。"
                )
            return env

        askpass = self.ws.remote_dir / ".askpass.sh"
        askpass.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$COMMANDER_SSH_PW"\n', encoding="utf-8"
        )
        askpass.chmod(0o700)
        env["SSH_ASKPASS"] = str(askpass)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["COMMANDER_SSH_PW"] = pw
        env["DISPLAY"] = env.get("DISPLAY") or ":0"
        return env

    def _wrap(self, host: HostConfig, argv: list[str],
              force_password: bool = False) -> list[str]:
        """需要 setsid 才能让 SSH_ASKPASS 生效（没有 tty 时）。"""
        if self._needs_askpass(host, force_password) and shutil.which("setsid"):
            return ["setsid", "-w", *argv]
        return argv

    def ssh_exec(
        self, host: HostConfig, command: str, *,
        timeout: int = 120, stdin_data: str | None = None,
        stream: bool = False, force_password: bool = False,
    ) -> subprocess.CompletedProcess | subprocess.Popen:
        argv = self._wrap(host, [*self._ssh_base(host, force_password), command],
                          force_password)
        env = self._ssh_env(host, force_password)
        if stream:
            return subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", env=env, bufsize=1,
            )
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=timeout,
            input=stdin_data,
        )

    def check(self, host: HostConfig) -> tuple[bool, str]:
        """连通性自检。不抛异常，返回 (是否通, 说明)。"""
        try:
            p = self.ssh_exec(host, "echo __OK__; hostname; nproc", timeout=30)
        except (subprocess.TimeoutExpired, RemoteError, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if isinstance(p, subprocess.CompletedProcess) and p.returncode == 0 and "__OK__" in (p.stdout or ""):
            lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
            hostname = lines[1] if len(lines) > 1 else "?"
            cores = lines[2] if len(lines) > 2 else "?"
            return True, f"{hostname} ({cores} 核)"
        rc = getattr(p, "returncode", "?")
        err = (getattr(p, "stderr", "") or "")[-300:]
        return False, f"退出码 {rc}: {err}"

    # ══════════════════════════════════════════════════════════════════
    # bootstrap：把远端准备成能跑的状态
    # ══════════════════════════════════════════════════════════════════
    def bootstrap(self, host: HostConfig, *, install_key: bool = True) -> list[str]:
        """首次连接远端时的一次性准备。返回步骤日志。"""
        steps: list[str] = []

        ok, detail = self.check(host)
        steps.append(f"{'✓' if ok else '✗'} 连通性: {detail}")
        if not ok:
            raise RemoteError(f"无法连接 {host.ssh_target()}: {detail}")

        # ① 工作目录
        p = self.ssh_exec(host, f"mkdir -p {shlex.quote(host.workdir)} && echo done", timeout=60)
        steps.append(f"{'✓' if _rc(p)==0 else '✗'} 创建 {host.workdir}")

        # ② uv —— 远端实测未安装
        p = self.ssh_exec(
            host,
            "command -v uv >/dev/null 2>&1 && uv --version || "
            "{ echo '需要安装'; true; }",
            timeout=60,
        )
        out = (getattr(p, "stdout", "") or "").strip()
        if out.startswith("uv "):
            steps.append(f"✓ uv 已存在: {out}")
        else:
            steps.append("… 远端无 uv，开始安装")
            # 远端没有 curl（实测），优先用 python3 urllib 装
            install_cmd = (
                "command -v curl >/dev/null 2>&1 && "
                f"({UV_INSTALL}) || "
                "python3 -c \"import urllib.request,os;"
                "os.makedirs(os.path.expanduser('~/.local/bin'),exist_ok=True)\" && "
                "(pip install --quiet uv 2>/dev/null || "
                "python3 -m pip install --quiet --break-system-packages uv 2>/dev/null || "
                "echo 'PIPFAIL')"
            )
            p = self.ssh_exec(host, install_cmd, timeout=900)
            p2 = self.ssh_exec(
                host,
                "export PATH=$HOME/.local/bin:$PATH; uv --version 2>&1 || echo MISSING",
                timeout=60,
            )
            v = (getattr(p2, "stdout", "") or "").strip()
            if v.startswith("uv "):
                steps.append(f"✓ uv 安装成功: {v}")
            else:
                steps.append(f"⚠ uv 安装未确认（远端可能需手动装）: {v[:120]}")

        # ③ 专用密钥 —— 把 root 密码认证换掉
        if install_key:
            steps += self._install_key(host)

        steps.append(f"✓ bootstrap 完成，可用后端: {', '.join(host.allowed_backends) or '(未限制)'}")
        return steps

    def _install_key(self, host: HostConfig) -> list[str]:
        """生成 commander 专用 ed25519 密钥并装到远端。

        实测远端 root + PasswordAuthentication=yes + authorized_keys 为空 ——
        这是很宽松的配置。装上密钥后就可以关掉密码认证。
        """
        steps: list[str] = []
        keydir = self.ws.remote_keys
        keydir.mkdir(parents=True, exist_ok=True)

        # 密钥文件名直接取 hosts.toml 里 identity_file 的 basename，
        # 这样生成出来的东西天然就是配置指向的东西，不会出现对不上的情况。
        if host.identity_file:
            key = (self.ws.root / host.identity_file)
            key.parent.mkdir(parents=True, exist_ok=True)
        else:
            key = keydir / f"commander_{host.name}_ed25519"

        if not key.is_file():
            gen = subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
                 f"commander@{self.ws.root.name}", "-f", str(key)],
                capture_output=True, text=True,
            )
            if gen.returncode != 0:
                steps.append(f"✗ 密钥生成失败: {gen.stderr[:200]}")
                return steps
            key.chmod(0o600)
            steps.append(f"✓ 已生成专用密钥 {key.name}")
        else:
            steps.append(f"· 复用已有密钥 {key.name}")

        pub = Path(str(key) + ".pub").read_text(encoding="utf-8").strip()
        # 用 && 串到底，只有整条链成功才打印哨兵（用 ; 的话失败也会打印，
        # 那就等于没检查）。幂等：已存在则不重复追加。
        cmd = (
            "set -e; "
            "mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; "
            "chmod 600 ~/.ssh/authorized_keys; "
            f"grep -qF {shlex.quote(pub)} ~/.ssh/authorized_keys || "
            f"echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys; "
            f"grep -qF {shlex.quote(pub)} ~/.ssh/authorized_keys && echo __KEY_INSTALLED__"
        )
        # 强制密码认证：此刻公钥还没装上去，用密钥会 Permission denied
        p = self.ssh_exec(host, cmd, timeout=60, force_password=True)
        if "__KEY_INSTALLED__" in (getattr(p, "stdout", "") or ""):
            steps.append("✓ 公钥已装入远端 authorized_keys")
            # 装完立刻验证密钥真的能用，别等到派发时才发现不行
            probe = self.ssh_exec(host, "echo __KEYAUTH_OK__", timeout=30)
            if "__KEYAUTH_OK__" in (getattr(probe, "stdout", "") or ""):
                steps.append("✓ 密钥认证实测可用（此后可关闭密码认证）")
            else:
                steps.append(
                    f"⚠ 密钥已装但认证未通过: "
                    f"{(getattr(probe,'stderr','') or '')[-200:]}"
                )
        else:
            steps.append(f"⚠ 公钥安装未确认: {(getattr(p,'stderr','') or '')[-200:]}")

        if host.identity_file:
            steps.append(f"✓ 已装到 hosts.toml 指定的位置 {key.relative_to(self.ws.root)}")
        return steps

    # ══════════════════════════════════════════════════════════════════
    # sync：同步后端工程与配置
    # ══════════════════════════════════════════════════════════════════
    def _rsync_shell(self, host: HostConfig, force_password: bool = False) -> str:
        """构造 rsync 的 `-e` 远端 shell 命令。

        关键：**绝不能带目标主机** —— rsync 会自己拼 `user@host`。
        之前把 host.ssh_target() 也塞进来，导致 rsync 执行
        `ssh ... user@host user@host` 而失败。
        """
        parts = ["ssh",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", f"Port={host.port}"]
        if self._use_key(host, force_password):
            ident = host.identity_path(self.ws)
            parts += ["-i", str(ident), "-o", "IdentitiesOnly=yes",
                      "-o", "PreferredAuthentications=publickey,password",
                      "-o", "NumberOfPasswordPrompts=1"]
        else:
            parts += ["-o", "PreferredAuthentications=password",
                      "-o", "PubkeyAuthentication=no",
                      "-o", "NumberOfPasswordPrompts=1"]
            if self._needs_askpass(host, force_password) and shutil.which("setsid"):
                parts = ["setsid", "-w", *parts]
        return " ".join(parts)

    def _payload_signature(self, backend: str) -> str:
        """同步内容的指纹。没变就跳过，避免每次派发都全量传输。

        ⚠️ 必须和 sync() 的 payload 列表保持一致 —— 少算一项就会出现
        「改了文件但指纹没变 → 同步被跳过 → 远端跑的还是旧代码」这种
        极难排查的问题（这里就真踩过一次：漏了 _shared）。
        """
        import hashlib
        h = hashlib.sha256()
        wi = self.ws
        for root in (wi.backends_dir / backend, wi.backends_dir / "_shared",
                     wi.config_dir, wi.skills_dir):
            if not root.exists():
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file():
                    continue
                if any(part in (".venv", "__pycache__") for part in f.parts):
                    continue
                if f.suffix in (".pyc", ".lock"):
                    continue
                h.update(str(f.relative_to(wi.root)).encode())
                with contextlib.suppress(OSError):
                    h.update(f.read_bytes())
        return h.hexdigest()[:16]

    def sync(self, host: HostConfig, backend: str, *, force: bool = False) -> str:
        """把远端需要的文件同步过去。返回日志。

        只同步必要的东西：后端工程 + config + skills。
        不同步 agents/（对方有自己的 workdir）、不同步 .env（密钥走环境变量）。
        """
        rsync = shutil.which("rsync")
        if not rsync:
            raise RemoteError(
                "本机没有 rsync，无法同步到远端。安装：apt install rsync"
            )

        wi = self.ws
        target = f"{host.ssh_target()}:{host.workdir}/"
        shell = self._rsync_shell(host)
        env = self._ssh_env(host)

        sig = self._payload_signature(backend)
        stamp = self.ws.remote_dir / f".sync-{host.name}-{backend}"
        if not force and stamp.is_file() and stamp.read_text().strip() == sig:
            return f"· 内容未变，跳同步（{sig}）"

        payload = [
            (wi.backends_dir / backend, f"backends/{backend}/"),
            # 共享协议必须一起同步 —— runner.py 靠它做 stdin/stdout 编解码
            (wi.backends_dir / "_shared", "backends/_shared/"),
            (wi.config_dir, "config/"),
            (wi.skills_dir, "skills/"),
        ]

        # rsync 只能建最末一级目录，父目录不存在会报
        # `mkdir ... failed: No such file or directory` —— 先手工铺好
        dirs = " ".join(
            shlex.quote(f"{host.workdir}/{dst}") for _, dst in payload
        )
        mk = self.ssh_exec(host, f"mkdir -p {dirs} && echo MKDIR_OK", timeout=60)
        if "MKDIR_OK" not in (getattr(mk, "stdout", "") or ""):
            raise RemoteError(
                f"远端 {host.name} 无法创建同步目录: "
                f"{(getattr(mk, 'stderr', '') or '')[-300:]}"
            )

        logs: list[str] = []
        for src, dst in payload:
            if not src.exists():
                logs.append(f"· 跳过不存在的 {src}")
                continue
            argv = [
                rsync, "-az", "--delete",
                "--exclude", ".venv", "--exclude", "__pycache__",
                "--exclude", "*.pyc", "--exclude", "uv.lock",
                "-e", shell,
                str(src) + "/", target + dst,
            ]
            p = subprocess.run(argv, capture_output=True, text=True,
                               env=env, timeout=600)
            if p.returncode == 0:
                logs.append(f"✓ 同步 {src.name} -> {dst}")
            else:
                logs.append(f"✗ 同步 {src.name} 失败: {(p.stderr or '')[-300:]}")
                raise RemoteError("\n".join(logs))

        stamp.write_text(sig, encoding="utf-8")
        return "\n".join(logs)

    def ensure_remote_env(self, host: HostConfig, backend: str,
                          *, force: bool = False) -> str:
        """确保远端有该后端的 venv。首次会 uv sync（crewai 136 个包可能要几分钟）。

        用远端标记文件记住「这个后端已经装好了」，否则每次派发都要重跑 uv sync
        —— 那是纯粹浪费，而且会拖慢每一次派发。
        """
        wd = shlex.quote(host.workdir)
        marker = f"{host.workdir}/.ready-{backend}"
        path_prefix = "export PATH=$HOME/.local/bin:$PATH; "

        if not force:
            # 哨兵必须互不为子串：之前用 "READY"/"NOTREADY"，
            # 而 "READY" in "NOTREADY" 恒为真 —— 于是一次都没真装过，
            # 每次都被误判为"已就绪"跳过。这类 bug 极难排查，哨兵要选干净。
            chk = self.ssh_exec(
                host,
                f"test -f {shlex.quote(marker)} && test -d "
                f"{shlex.quote(host.workdir)}/backends/{shlex.quote(backend)}/.venv "
                f"&& echo __BACKEND_READY__ || echo __NOT_READY__",
                timeout=60,
            )
            if "__BACKEND_READY__" in (getattr(chk, "stdout", "") or ""):
                return f"· {backend} 远端依赖已就绪，跳过 uv sync"

        cmd = (
            f"{path_prefix}cd {wd} && "
            f"uv sync --project backends/{shlex.quote(backend)} 2>&1 | tail -8; "
            f"rc=${{PIPESTATUS[0]}}; "
            f"if [ $rc -eq 0 ]; then touch {shlex.quote(marker)}; echo SYNC_RC=$rc; "
            f"else echo SYNC_RC=$rc; fi"
        )
        p = self.ssh_exec(host, cmd, timeout=2400)
        out = getattr(p, "stdout", "") or ""
        if "SYNC_RC=0" not in out:
            raise RemoteError(
                f"远端 {host.name} 上后端 {backend} 依赖安装失败:\n"
                f"{out[-1500:]}\n{(getattr(p,'stderr','') or '')[-800:]}"
            )
        return f"✓ {backend} 远端依赖已安装"

    # ══════════════════════════════════════════════════════════════════
    # run：执行
    # ══════════════════════════════════════════════════════════════════
    def remote_workdir(self, host: HostConfig, agent_id: str) -> str:
        return f"{host.workdir}/agents/{agent_id}/work"

    def run(
        self, host: HostConfig, backend: str, spec: RunSpec,
        on_event=None, on_stderr=None,
    ) -> RunResult:
        """在远端跑一次派发。线协议与本地完全一致。"""
        self.guard.assert_valid_agent_id(spec.agent_id)

        try:
            self.sync(host, backend)
            self.ensure_remote_env(host, backend)
        except RemoteError as exc:
            kind, retryable = classify_error(exc)
            return RunResult(ok=False, error=str(exc), error_kind=kind,
                             retryable=retryable, backend=backend)

        rwd = self.remote_workdir(host, spec.agent_id)
        # 规格里的 workdir 改写成远端路径 —— 后端在远端按这个路径工作
        remote_spec = spec.model_copy(update={
            "workdir": rwd,
            "workspace_root": host.workdir,
        })

        path_prefix = "export PATH=$HOME/.local/bin:$PATH; "
        env_exports = self._remote_env_exports(remote_spec)
        # 远端加一层 timeout 兜底。
        # 实测问题：本地 kill 掉派发进程后，**远端 runner 会变成孤儿继续跑** ——
        # SSH 断开并没有回收它（uv run 的子进程脱离了会话），
        # 实测看到过同一个 agent 上挂着两个 runner，一个已经跑了 12 分钟。
        # 本地超时管不到远端，所以远端必须有自己的生命期上限。
        remote_timeout = max(60, spec.timeout + 120)
        cmd = (
            f"{path_prefix}"
            f"mkdir -p {shlex.quote(rwd)} && cd {shlex.quote(rwd)} && "
            f"{env_exports}"
            f"cd {shlex.quote(host.workdir)} && "
            f"timeout --signal=TERM --kill-after=30 {remote_timeout} "
            f"uv run --project backends/{shlex.quote(backend)} --no-sync "
            f"python backends/{shlex.quote(backend)}/runner.py"
        )

        start = time.time()
        events: list[Event] = []
        stderr_lines: list[str] = []

        try:
            proc = self.ssh_exec(host, cmd, stream=True)
        except Exception as exc:
            kind, retryable = classify_error(exc)
            return RunResult(ok=False, error=f"SSH 启动失败: {exc}",
                             error_kind=kind, retryable=retryable, backend=backend)

        assert isinstance(proc, subprocess.Popen)
        timed_out = False
        try:
            assert proc.stdin is not None
            proc.stdin.write(remote_spec.wire() + "\n")
            proc.stdin.close()

            # ⚠️ 读循环必须自己有截止时间，不能只靠后面 proc.wait 的超时。
            #
            # 实测：browser-use 在远端拉起 Chrome（detached），Chrome 继承了
            # ssh 那条 stdout 管道。runner 退出后 Chrome 还活着，远端管道不关，
            # ssh 本地这头就永远读不到 EOF —— **结果事件早就收到了，进程却卡死**，
            # outbox 一直写不出来，本地也留下一堆孤儿 ssh。
            #
            # 用「读线程 + 队列」绕开 TextIOWrapper 的缓冲层（fd 层的 select
            # 看不到缓冲里的数据，两者混用会出各种诡异问题）。
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
                             name=f"ssh-reader-{spec.agent_id}").start()

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
                        break
                    continue

                if line is EOF:
                    break
                if not line.strip():
                    continue
                try:
                    import json
                    ev = Event.model_validate(json.loads(line))
                except Exception:
                    if on_stderr:
                        on_stderr(line.rstrip())
                    continue
                events.append(ev)
                if on_event:
                    on_event(ev)

        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
        finally:
            if proc.stderr is not None:
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
                    if on_stderr:
                        on_stderr(line.rstrip())
            if proc.poll() is None:
                proc.kill()

        self.collect(host, spec.agent_id)

        for ev in reversed(events):
            if ev.type == "result":
                try:
                    r = RunResult.model_validate(ev.data)
                    r.raw["remote_host"] = host.name
                    return r
                except Exception:
                    pass

        if timed_out:
            return RunResult(ok=False, error_kind="timeout", retryable=True,
                             error=f"远端 {host.name} 超过 {spec.timeout}s 未完成",
                             backend=backend)

        tail = "\n".join(stderr_lines[-12:])
        return RunResult(
            ok=False, error_kind="no_result", retryable=False,
            error=f"远端后端退出码 {proc.returncode}，无 result 事件。stderr:\n{tail}",
            backend=backend,
        )

    def _remote_env_exports(self, spec: RunSpec) -> str:
        """远端命令行的环境变量前缀。

        ⚠️ **绝不在这里传凭据**。命令行参数会出现在两端的 `ps aux` 里，
        也会进 shell 历史 —— 实测就是这么泄露过一次。
        凭据随 RunSpec 经 stdin 传给 runner，由 apply_credentials() 注入环境。
        """
        return (
            f"COMMANDER_AGENT_ID={shlex.quote(spec.agent_id)} "
            f"COMMANDER_WORKDIR={shlex.quote(spec.workdir)} "
            f"COMMANDER_MAX_TOKENS={spec.max_tokens} "
            f"PYTHONUNBUFFERED=1 "
        )

    def collect(self, host: HostConfig, agent_id: str) -> str:
        """把远端 workdir 的产物拉回本地 agent 目录。"""
        rsync = shutil.which("rsync")
        if not rsync:
            return "⚠ 本机无 rsync，产物未回传"

        src = f"{host.ssh_target()}:{self.remote_workdir(host, agent_id)}/"
        dst = self.ws.agent_workdir(agent_id)
        dst.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(
            [rsync, "-az", "--exclude", ".venv", "--exclude", "__pycache__",
             "-e", self._rsync_shell(host), src, str(dst) + "/"],
            capture_output=True, text=True, env=self._ssh_env(host), timeout=600,
        )
        # 远端 rsync 在源目录为空时返回 23，不算失败
        return ("✓ 产物已回传" if p.returncode in (0, 23)
                else f"✗ 回传失败: {(p.stderr or '')[-200:]}")


def _rc(p) -> int:
    return int(getattr(p, "returncode", 1) or 0)
