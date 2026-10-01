"""凭据写入 —— 让用户「用说的」就能配好。

用户需求：「ssh的账号密码和大模型的api和base url我希望可以直接以提示词的形式告诉它」

于是流程变成：用户在对话里说

    用指挥官做 X。DeepSeek 的 key 是 sk-xxx，base url 是 https://api.deepseek.com
    远端是 192.0.2.10，root/<密码>

指挥官把这些写进正确的位置，用户不用碰任何文件。

━━ 凭据怎么传 ━━
**绝不走命令行参数**。实测踩过：`ssh host 'OPENAI_API_KEY=sk-xxx ...'` 这种写法
会让密钥出现在两端的 `ps aux` 里，还会进 shell 历史。
所以本模块从 **stdin 读 JSON**，argv 里只有子命令名。

━━ 凭据写到哪 ━━
    密钥           → bin/.env        （600 权限 + gitignored，绝不进版本库）
    base_url       → bin/.env        （以 COMMANDER_<PROVIDER>_BASE_URL 覆盖）
    SSH 主机与密码 → remote/hosts.toml + bin/.env
    config/*.toml  → 绝不写密钥，那些文件是要进版本库的
"""

from __future__ import annotations

import contextlib
import json
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from .workspace import Workspace


class CredentialError(RuntimeError):
    pass


# ── 覆盖用的环境变量名 ────────────────────────────────────────────────────
def base_url_env(provider: str) -> str:
    return f"COMMANDER_{provider.upper()}_BASE_URL"


def anthropic_base_url_env(provider: str) -> str:
    return f"COMMANDER_{provider.upper()}_ANTHROPIC_BASE_URL"


def mask(secret: str | None) -> str:
    """把密钥变成可安全显示的形式。

    只留头尾各 4 位 —— 足够让用户确认"是这一把"，又不足以泄露。
    短于 12 位的直接全遮，否则遮了跟没遮一样。
    """
    if not secret:
        return "(未设置)"
    s = secret.strip()
    if len(s) < 12:
        return "•" * len(s)
    return f"{s[:4]}…{s[-4:]}（{len(s)} 位）"


@dataclass
class CredentialUpdate:
    """一次凭据写入请求。字段都可以缺省。"""

    providers: dict[str, dict] = field(default_factory=dict)
    hosts: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: dict) -> CredentialUpdate:
        if not isinstance(payload, dict):
            raise CredentialError("输入必须是一个 JSON 对象")

        providers = payload.get("providers") or payload.get("provider") or {}
        if providers and "api_key" in providers:
            # 允许简写：{"providers": {"deepseek": {...}}} 或
            # {"provider": "deepseek", "api_key": "..."} 两种都收
            name = payload.get("provider") or "deepseek"
            providers = {name: providers}

        hosts = payload.get("hosts") or {}
        if not isinstance(providers, dict) or not isinstance(hosts, dict):
            raise CredentialError("providers / hosts 必须是对象")

        return cls(providers=providers, hosts=hosts)

    def is_empty(self) -> bool:
        return not self.providers and not self.hosts


# ══════════════════════════════════════════════════════════════════════════
# .env 读写
# ══════════════════════════════════════════════════════════════════════════

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_env(path: Path) -> tuple[dict[str, str], list[str]]:
    """读 .env，返回 (键值对, 原始行)。

    保留原始行是为了写回时不丢注释和空行 —— 用户可能自己加过东西。
    """
    if not path.is_file():
        return {}, []
    values: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for ln in lines:
        m = _ENV_LINE.match(ln)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values, lines


def write_env(path: Path, updates: dict[str, str]) -> None:
    """把键值合并进 .env：已有的就地改，没有的追加。

    不重写整个文件 —— 用户加的注释和自定义键必须留着。
    """
    _values, lines = read_env(path)
    remaining = dict(updates)

    out: list[str] = []
    for ln in lines:
        m = _ENV_LINE.match(ln)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(ln)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# ── 由 commander config 写入 ──")
        out.extend(f"{k}={v}" for k, v in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

    # 密钥文件权限必须是 600 —— 组/其他用户可读等于泄露
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ══════════════════════════════════════════════════════════════════════════
# 应用
# ══════════════════════════════════════════════════════════════════════════

# 允许用户覆盖的 provider 字段 → .env 变量名后缀（None 表示是密钥，用 api_key_env）
_PROVIDER_ENV = {
    "base_url": base_url_env,
    "anthropic_base_url": anthropic_base_url_env,
}


def apply_update(ws: Workspace, update: CredentialUpdate, *,
                 dry_run: bool = False) -> list[str]:
    """把凭据写进正确的位置。返回人类可读的操作日志。"""
    from .config import Config

    log: list[str] = []
    env_updates: dict[str, str] = {}

    # ── 大模型凭据 ────────────────────────────────────────────────────
    cfg = None
    with contextlib.suppress(Exception):
        cfg = Config(ws)

    for name, spec in update.providers.items():
        if not isinstance(spec, dict):
            raise CredentialError(f"provider {name!r} 的配置必须是对象")

        provider = cfg.providers.get(name) if cfg else None
        if provider is None:
            known = ", ".join(sorted(cfg.providers)) if cfg else "(配置读取失败)"
            raise CredentialError(
                f"未注册的 provider {name!r}。models.toml 里有的：{known}\n"
                f"想接新 provider 就先在 config/models.toml 的 [providers] 下加一段。"
            )

        if key := spec.get("api_key"):
            key = str(key).strip()
            if not provider.api_key_env:
                raise CredentialError(f"provider {name!r} 没配 api_key_env，无法写密钥")
            env_updates[provider.api_key_env] = key
            log.append(f"密钥 {provider.api_key_env} = {mask(key)}  → .env")

        for field_name, env_fn in _PROVIDER_ENV.items():
            if val := spec.get(field_name):
                env_updates[env_fn(name)] = str(val).strip()
                log.append(f"{field_name} → {env_fn(name)} = {val}")

        if not provider.enabled if provider else False:
            log.append(f"⚠ provider {name} 在 models.toml 里是 enabled=false，"
                       f"需要手工改成 true 才会被使用")

    # ── SSH 主机 ──────────────────────────────────────────────────────
    host_entries: dict[str, dict] = {}
    if update.hosts:
        hosts_file = ws.hosts_toml
        existing: dict = {}
        if hosts_file.is_file():
            try:
                existing = tomllib.loads(hosts_file.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise CredentialError(
                    f"现有的 remote/hosts.toml 解析不了：{exc}\n"
                    f"先修好它，或删掉重建。"
                ) from exc

        merged = dict(existing.get("hosts") or {})

        for name, spec in update.hosts.items():
            if not isinstance(spec, dict):
                raise CredentialError(f"host {name!r} 的配置必须是对象")
            if not spec.get("host"):
                raise CredentialError(f"host {name!r} 缺少 host（地址）")

            entry = dict(merged.get(name) or {})
            entry["host"] = str(spec["host"]).strip()
            entry["user"] = str(spec.get("user") or entry.get("user") or "root")
            entry["port"] = int(spec.get("port") or entry.get("port") or 22)
            entry["workdir"] = str(spec.get("workdir") or entry.get("workdir")
                                   or "/opt/commander")
            if spec.get("identity_file"):
                entry["identity_file"] = str(spec["identity_file"])

            # 密码不写进 hosts.toml —— 那文件会进版本库。
            # 只写"去哪个环境变量取"，值放 .env。
            pw = spec.get("password")
            if pw:
                pw_env = f"COMMANDER_SSH_PASSWORD_{name.upper()}"
                entry["password_env"] = pw_env
                env_updates[pw_env] = str(pw)
                log.append(f"SSH 密码 {name} = {mask(str(pw))} → .env（{pw_env}）")
            elif entry.get("password_env"):
                log.append(f"SSH 密码 {name}：沿用已有的 {entry['password_env']}")

            merged[name] = entry
            host_entries[name] = entry
            log.append(
                f"主机 {name}: {entry['user']}@{entry['host']}:{entry['port']} "
                f"→ remote/hosts.toml"
            )

        if not dry_run:
            hosts_file.parent.mkdir(parents=True, exist_ok=True)
            # 保留原文件里的注释？tomli_w 不保留。所以先备份一次。
            if hosts_file.is_file():
                backup = hosts_file.with_suffix(".toml.bak")
                if not backup.exists():
                    backup.write_text(
                        hosts_file.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            hosts_file.write_text(
                tomli_w.dumps({"hosts": merged}), encoding="utf-8"
            )

    # ── 写 .env ───────────────────────────────────────────────────────
    if env_updates and not dry_run:
        write_env(ws.dotenv, env_updates)

    return log


# ══════════════════════════════════════════════════════════════════════════
# 展示
# ══════════════════════════════════════════════════════════════════════════

def describe(ws: Workspace) -> list[tuple[str, str, str]]:
    """当前凭据状态，密钥一律打码。返回 (类别, 名称, 说明)。"""
    from .config import Config

    rows: list[tuple[str, str, str]] = []
    cfg = Config(ws)

    for name, p in cfg.providers.items():
        if not p.enabled:
            rows.append(("provider", name, "已停用（models.toml enabled=false）"))
            continue
        key = p.api_key()
        rows.append(("provider", name, f"密钥 {mask(key)}  base_url={p.effective_base_url() or '(默认)'}"))

    for name, h in cfg.hosts.items():
        ident = h.identity_path(ws)
        if ident:
            rows.append(("host", name, f"{h.user}@{h.host}:{h.port}  密钥认证 {ident.name}"))
        elif h.password():
            rows.append(("host", name, f"{h.user}@{h.host}:{h.port}  密码认证 {mask(h.password())}"))
        else:
            rows.append(("host", name, f"{h.user}@{h.host}:{h.port}  ⚠ 无可用凭据"))

    if not cfg.hosts:
        rows.append(("host", "(无)", "未配置远端；重依赖后端会落本地或不可用"))

    return rows


def parse_stdin(raw: str) -> CredentialUpdate:
    """把 stdin 的内容解析成 CredentialUpdate。

    宽容一点：JSON 之外也接受 `KEY=value` 行式输入，因为人可能想直接粘贴
    .env 风格的片段。
    """
    text = (raw or "").strip()
    if not text:
        raise CredentialError(
            "stdin 是空的。需要一段 JSON，例如：\n"
            '  {"providers": {"deepseek": {"api_key": "sk-...", '
            '"base_url": "https://api.deepseek.com/v1"}},\n'
            '   "hosts": {"osboxes": {"host": "192.0.2.10", '
            '"user": "root", "password": "..."}}}'
        )

    try:
        return CredentialUpdate.parse(json.loads(text))
    except json.JSONDecodeError:
        pass

    # KEY=value 行式兜底
    env: dict[str, str] = {}
    for ln in text.splitlines():
        m = _ENV_LINE.match(ln)
        if m:
            env[m.group(1)] = m.group(2).strip()
    if not env:
        raise CredentialError("既不是合法 JSON，也不是 KEY=value 形式")

    providers: dict[str, dict] = {}
    for k, v in env.items():
        if k.endswith("_API_KEY"):
            providers.setdefault("deepseek", {})["api_key"] = v
        elif k.endswith("_BASE_URL") and not k.startswith("COMMANDER_"):
            providers.setdefault("deepseek", {})["base_url"] = v
    if not providers:
        raise CredentialError("KEY=value 形式里没认出任何 provider 字段")
    return CredentialUpdate(providers=providers)
