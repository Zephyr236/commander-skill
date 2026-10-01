# 目录契约细则

## 目录

- [为什么需要强制](#为什么需要强制)
- [完整目录表](#完整目录表)
- [强制机制的三道防线](#强制机制的三道防线)
- [沙箱的真实边界](#沙箱的真实边界)
- [常见违规与正确做法](#常见违规与正确做法)
- [自检](#自检)

---

## 为什么需要强制

用户需求原文：「需要严格规定每一个目录的作用，每一个agent必须要在指定的目录中工作」

如果只是文档里写"agent 只能写自己的目录"，那它只是**建议**。多智能体系统里
最典型的失败模式就是：A agent 覆盖了 B agent 的中间产物，或者某个 agent 把
半成品写进了记忆库，导致整条链路的证据被污染。

所以这里把契约做成**会抛异常的检查 + 内核级沙箱**。

---

## 完整目录表

| 路径 | 职责 | 谁能写 | 谁能读 |
|---|---|---|---|
| `.claude/skills/commander/` | 本技能定义 | 用户 | 指挥官 |
| `.claude/agents/` | Claude Code subagent 定义 | 用户 | 指挥官 |
| `bin/src/commander/` | 派发层编排逻辑 | 用户 | 全部 |
| `bin/backends/<sdk>/` | 各 SDK 独立 uv 工程 | 用户 | 全部（只读） |
| `bin/.env` | **密钥**（600 权限，gitignored） | 用户 | 指挥官（**子进程读不到**） |
| `config/*.toml` | 模型/编制/策略 | 用户 | 全部（只读） |
| `memory/` | 指挥官长期记忆 | **仅** commander + memory-scout | 全部 |
| `memory/INDEX.md` | 自动生成的索引 | commander（`memory index`） | 全部 |
| `tasks/registry.jsonl` | 任务流水，append-only | commander | 全部 |
| `tasks/BOARD.md` | 看板 | commander（`task board`） | 全部 |
| `tasks/active/<id>/` | 单任务档案 | commander | 全部 |
| `agents/<id>/work/` | **该 agent 唯一的工作区** | 该 agent | 该 agent |
| `agents/<id>/outbox/` | 交回的结果 | 该 agent（经派发层） | commander |
| `agents/<id>/logs/` | 消息历史 | 派发层 | commander |
| `agents/<id>/artifacts/` | 产物 | 该 agent | commander |
| `skills/<name>/SKILL.md` | 技能库 | 用户 / commander | 全部 |
| `remote/keys/` | SSH 专用密钥 | commander（bootstrap） | commander |
| `logs/guard-violations.jsonl` | 越权尝试记录 | guard | commander |
| `logs/patrol-latest.md` | 最近一次巡检简报 | commander | commander |

**跨 agent 写入一律拒绝** —— 包括"我只是想读一下别的 agent 的产物然后写回自己目录"，
读取可以，写入不行。

---

## 强制机制的三道防线

### 防线 1：cwd 隔离（最可靠的第一道）

子进程的 `cwd` 被 `guard.resolve_workdir()` 钉死在 `agents/<id>/work/`。
agent 用**相对路径**时天然只能写到自己的地盘。

### 防线 2：路径白名单（`guard.assert_writable`）

指挥官自己的文件操作要过检查：

```
denied_roots   = [".git/", ".claude/"]        ← 任何身份都禁止
memory/                                        ← 只有 memory_writers 白名单
agents/<id>/                                   ← 只有属主（commander 例外）
writable_roots = ["agents/", "logs/", "tasks/", "remote/logs/"]
其余                                           ← 默认拒绝
```

注意是**默认拒绝**（default-deny），不是默认允许。新增目录要显式加白名单。

### 防线 3：bwrap 沙箱（内核级，真正的强制）

前两道防线管不住子进程用**绝对路径**的情况。实测过的教训：

> mock 后端用 `Path(workspace_root)/"memory"/"_mock_probe.md"` 成功写出了文件 ——
> 逃逸了。cwd 隔离只约束相对路径。

所以子进程跑在 bubblewrap 里：

```
bwrap --ro-bind / /                    整个文件系统挂只读
      --bind  <agents/<id>>            只把这个 agent 的沙箱挂回可写
      --bind  <logs/>                  全局日志可写
      --ro-bind /dev/null <bin/.env>   密钥遮蔽成空文件
      --tmpfs /tmp
      --unshare-pid --unshare-ipc --unshare-uts
      --die-with-parent --new-session
```

实测结果（`./.commander/cmd doctor` 里的沙箱自检就是跑这个）：

| 目标 | 结果 |
|---|---|
| 自己的 workdir | ✓ 可写 |
| `memory/` | ✓ `Read-only file system` |
| 别的 agent 目录 | ✓ 被挡 |
| `.claude/` | ✓ 被挡 |
| 工作区外 `/etc` | ✓ 被挡 |
| `bin/.env` | ✓ 读出 0 字节 |

---

## 沙箱的真实边界

**必须诚实说明能防什么、不能防什么。**

### 能防

- 子进程误写工作区内的敏感目录（记忆、别的 agent、配置）
- 子进程读到密钥
- 子进程污染宿主的 `$HOME`、`/tmp`

### 不能防

- **网络访问**。agent 必须能调模型 API，所以不能 `--unshare-net`。
  一个有恶意的 agent 可以把数据发到外部。
- **自己 workdir 内的任意行为**。它能在自己的沙箱里写任何东西。
- **资源耗尽**。没有 cgroup 限额，一个跑飞的 agent 能吃到 CPU/内存上限。
  `policy.toml` 的超时是唯一的约束。
- **bwrap 不可用时的降级**。此时只剩防线 1，绝对路径可越界。

### 配置

```toml
# config/policy.toml
[guard]
sandbox = "auto"   # auto=有 bwrap 就用 | bwrap=强制(没有则报错) | none=只用 cwd
```

**生产环境建议设 `bwrap`** —— 让"沙箱不可用"变成一个显式错误，
而不是静默降级到没有保护的状态。

---

## 常见违规与正确做法

| 违规 | 报错 rule | 正确做法 |
|---|---|---|
| agent 想写记忆 | `memory_not_writable` | 把结论交回指挥官，由指挥官/`memory-scout` 写入 |
| agent 写别的 agent 目录 | `cross_agent_write` | 需要对方数据就请指挥官派发，或读对方的 outbox |
| 想改 `.claude/` 或 `.git/` | `denied_root` | 由用户手工改 |
| 写 `config/` | `not_in_writable_roots` | 配置变更属于用户决策，走 `AskUserQuestion` |
| agent_id 带斜杠/点开头 | `invalid_agent_id` | agent_id 会变成目录名，必须干净 |

所有违规都记入 `logs/guard-violations.jsonl`。**失败也是情报** ——
如果某个 agent 反复尝试越界，说明它的 BRIEF 没写清楚边界。

---

## 自检

```bash
# 环境 + 沙箱 + 后端 + 远端全检
./.commander/cmd doctor --remote

# 单独跑沙箱实测（会真的试一次越界写）
./.commander/cmd python -c "
from commander.config import Config
from commander.sandbox import Sandbox
cfg = Config()
print(Sandbox(cfg.ws, cfg.policy.guard.sandbox).self_test())
"

# 看历史违规
cat logs/guard-violations.jsonl | python3 -m json.tool --json-lines
```

巡检时 `patrol` 会自动报告违规计数。
