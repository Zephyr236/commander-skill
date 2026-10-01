# 远端执行细则

## 目录

- [为什么需要远端](#为什么需要远端)
- [本工作区的远端](#本工作区的远端)
- [首次使用：bootstrap](#首次使用bootstrap)
- [认证](#认证)
- [同步机制](#同步机制)
- [执行与回传](#执行与回传)
- [排障](#排障)

---

## 为什么需要远端

用户需求原文：「其中还会包含使用ssh调用远程服务器完成的情况，也就是把agent放在服务器中运行」

原因不是"分布式很酷"，而是**算力差 20 倍**：

| | 本地（指挥官所在） | 远端 osboxes |
|---|---|---|
| CPU | 2 核 | **40 核** |
| 可用内存 | ~2Gi | **~20Gi** |
| 磁盘可用 | — | 449G |
| pip / venv | ✗ 无 pip | ✓ pip 24.0 |
| uv | ✓ 0.12.19 | 需 bootstrap 安装 |
| curl | ✓ | **✗ 无**（用 `python3 urllib`） |
| claude CLI | ✓ 2.1.283 | ✓ 2.1.283 |

`crewai` 一个后端就要 136 个包（实测远端 venv **771M**）。
本地 2Gi 内存装不下也不该装。

**落点规则**（`router.py` 自动执行）：

```python
HEAVY_BACKENDS = {"crewai", "autogen", "hermes", "langchain"}   # → 远端
LIGHT_BACKENDS = {"claude", "openai", "openai_compat", "mock"}  # → 本地
NEEDS_BROWSER  = {"browser_use"}                                 # → 看哪台有浏览器
```

`agents.toml` 里 `target` 可显式覆盖：`local` / `remote` / `auto`。

**`auto` 有三条规则**（不是一条）：

| 后端 | 判据 |
|---|---|
| `HEAVY_BACKENDS` | 依赖装不下 → 远端 |
| `LIGHT_BACKENDS` | 轻量 → 本地 |
| **`NEEDS_BROWSER`** | **哪台机器有浏览器就落哪台**；两边都没有 → 明确报错，不硬跑 |

`NEEDS_BROWSER` 是独立的第三条，因为它跟依赖体积无关 —— `browser_use` 才 226M
本地装得下，但**没有浏览器就是跑不了**。这条在 `router._pick_host_for_browser()`。

**远端不可达时自动降级本地，绝不因此拒绝服务**（除非 agent 显式写了
`target="remote"` —— 那是明确意图，冲突时硬报错）。

---

## 本工作区的远端

**以 `remote/hosts.toml` 的实际内容为准 —— 没配就是没有，不要假设有。**

```bash
./.commander/cmd doctor --remote       # 远端总览
./.commander/cmd remote check <主机名>  # 单机连通性
```

重依赖后端（crewai / autogen / hermes / langchain）设计上就该落远端。
**没配远端时它们保持"未装"，这是正常状态，不是故障** —— 别去硬装，
那会在本地塞进 1GB+ 依赖。

---

## 首次使用：bootstrap

```bash
./.commander/cmd remote check              # 只看连通性
./.commander/cmd remote bootstrap --host osboxes
```

bootstrap 做四件事：

1. **连通性检测**（顺带报出核数）
2. **建工作目录**（`hosts.toml` 的 `workdir`）
3. **装 uv**（远端实测没有 uv；有 curl 用官方脚本，没有则退到 pip）
4. **生成并安装专用 ed25519 密钥**，装完**立刻实测密钥认证是否真的可用**

第 4 步有个鸡生蛋陷阱，代码里专门处理了：密钥文件一旦在本地生成，
`_ssh_base` 就会优先用它 —— 但此时公钥还没到远端，会 `Permission denied`。
所以安装公钥那一步**强制走密码认证**（`force_password=True`）。

---

## 认证

优先级：**专用密钥 > 密码**。

```toml
# remote/hosts.toml
[hosts.osboxes]
identity_file = "remote/keys/commander_ed25519"
password_env  = "COMMANDER_SSH_PASSWORD"    # 兜底，bootstrap 后建议清空
```

### 关于密码认证

本机没有 `sshpass`，密码路径走 OpenSSH 自带的 `SSH_ASKPASS`：

```python
SSH_ASKPASS=<临时脚本>  SSH_ASKPASS_REQUIRE=force  setsid -w ssh ...
```

`setsid` 是必须的 —— 没有 tty 时 ssh 会忽略 `SSH_ASKPASS`。

即使配了密钥，`PreferredAuthentications` 也写成 `publickey,password`（允许回退），
否则密钥一旦失效就会把自己锁死。

### 安全建议

实测远端原状：`PermitRootLogin yes` + `PasswordAuthentication yes` +
`authorized_keys` 是 0 字节空文件 —— 相当宽松。

bootstrap 装上密钥并**实测可用**之后，建议：

1. 清空 `hosts.toml` 里的 `password_env`
2. 远端 `/etc/ssh/sshd_config` 改 `PasswordAuthentication no` 后重启 sshd

---

## 同步机制

```bash
./.commander/cmd remote sync crewai      # 手工同步 + 装依赖
```

自动同步在每次远端派发前发生，内容是：

```
bin/backends/<sdk>/    该后端工程
bin/backends/_shared/  共享协议（漏了它 runner 起不来）
config/                模型/编制/策略
skills/                技能库
```

**不同步**：`agents/`（远端有自己的 workdir）、`bin/.env`（密钥走环境变量传）。

### 两个易错点（都实际踩过）

1. **rsync 的 `-e` 参数绝不能带目标主机** —— rsync 自己会拼 `user@host`。
   塞进去会变成 `ssh ... root@host root@host` 而失败。
2. **父目录要先建** —— rsync 只能建最末一级，
   `/opt/commander/backends/` 不存在时报 `mkdir failed: No such file or directory`。

### 增量跳过

同步前算一次内容指纹（`_payload_signature`），没变就跳过。
指纹覆盖的目录**必须与 payload 列表严格一致** ——
曾经漏算 `_shared`，导致"改了文件但指纹没变 → 同步跳过 → 远端跑的还是旧代码"，
这类问题极难排查。

远端依赖安装也有标记文件 `/opt/commander/.ready-<backend>`，装过就不重装。

> ⚠️ 哨兵字符串要选互不为子串的。曾经用 `"READY" in stdout` 判断，
> 而输出是 `NOTREADY` —— `"READY"` 是它的子串，于是**一次都没真装过**。
> 现在统一用 `__BACKEND_READY__` 这类形式。

---

## 执行与回传

远端执行时：

1. 用 `uv run --project backends/<sdk>` 跑 **同一个 `runner.py`**
2. 线协议**完全不变** —— 指挥官无需知道这次在本地还是远端
3. 模型凭据通过**环境变量**传给远端进程，**不落盘到远端**
4. `workdir` 改写成远端路径：`<host.workdir>/agents/<id>/work`
5. 跑完 `rsync` 把产物拉回本地 `agents/<id>/work/`

第 2 点是整个设计的收益：新增后端或改协议，本地远端同时生效。

### 远端日志

远端执行的事件流中继回本地，落在同一个 `agents/<id>/logs/` 下，
和本地执行**格式完全一致**。`result.raw.remote_host` 会标记它来自哪台机器。

---

## 浏览器准备（browser-use 用）

`browser` agent 需要一个**已经在跑的、开着调试端口的 Chromium**。它自己不下载浏览器，
所以这件事得有人做 —— 三个命令管它（**先看本机，本机有就起本机**）：

```bash
./.commander/cmd browser up       # 起（幂等，已在跑就直接返回）
./.commander/cmd browser status   # 看
./.commander/cmd browser down     # 停
```

**为什么不让后端自己拉**：Chrome 的生命周期比一次派发长得多（反复起停既慢又要
重新登录），而且**孤儿 Chrome 会占住 ssh 的 stdout 管道**导致派发卡死（踩过）。
显式管理更可控。

### 先跑官方诊断

`browser-use` 自带一个诊断工具，**比手写检查信息全**（它会报 Chrome 状态、
daemon、连接数、云端鉴权）：

```bash
./bin/backends/browser_use/.venv/bin/browser-use doctor
```

（没有 `commander python` 这个子命令 —— `cmd` 只转发给 commander 的 typer app。）

输出形如：

```
platform          Linux 6.8.0-31-generic
python            3.12.3
version           0.1.13 (pypi)
[ok  ] chrome running          ← 这一项是重点
[FAIL] daemon alive            ← CLI 模式的 daemon，派发模式用不到
[FAIL] active browser connections
```

出问题时**先跑它**，不要凭猜。

### 前置条件

机器上要有 Chromium。**原生 .deb 版最省事**，或者让指挥官下一个
（见下）—— 两条路都行，但**snap 版不行**：

```bash
google-chrome-stable --version
```

⚠️ **snap 装的 Chromium 不行** —— 它的沙箱挡了 DevTools 端口，连不上。
实测远端装的是 .deb 版（`/opt/google/chrome/chrome`），可用。

### 配置

`config/agents.toml` 里 `browser` agent 的 `options`：

```toml
options = { use_vision = false, headless = true,
            cdp_url = "http://127.0.0.1:9222" }
```

`use_vision = false` 是**必须的** —— 默认它会走截图 + 视觉模型，
而我们用的是文本模型。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `Project directory backends/x does not exist` | 同步没跑或失败 | `remote sync <backend>` 看输出 |
| `mkdir ... failed: No such file or directory` | 父目录不存在 | 已修（sync 里预建）；否则手工 `mkdir -p` |
| `ModuleNotFoundError: No module named 'commander_protocol'` | `_shared` 没同步 | 检查指纹是否覆盖 `_shared` |
| 依赖装了但 import 失败 | 标记文件误判为已就绪 | `remote sync <backend>`（带 `--install`）强制重装 |
| `Permission denied (publickey,password)` | 公钥没装成 | 重新 `bootstrap`；它强制走密码装公钥 |
| 远端慢 | 每次同步 + 装依赖 | 检查指纹与标记文件是否生效 |
| 远端无 `curl` | 该机确实没装 | 探测脚本一律用 `python3 -c "import urllib.request..."` |

### 手工验证远端状态

```bash
./.commander/cmd python -c "
from commander.config import Config
from commander.ssh_runner import RemoteRunner
from commander.guard import Guard
cfg = Config(); rr = RemoteRunner(cfg, Guard(cfg.ws, cfg.policy.guard))
h = cfg.hosts['osboxes']
for cmd in ['ls /opt/commander/', 'ls /opt/commander/.ready-* 2>/dev/null',
            'du -sh /opt/commander/backends/*/.venv 2>/dev/null']:
    p = rr.ssh_exec(h, cmd, timeout=60)
    print(f'--- {cmd}'); print((p.stdout or '').strip())
"
```
