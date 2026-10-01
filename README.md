# 指挥官 Commander

把 Claude Code 主会话变成一个**指挥官**：它不亲自干活，而是把任务拆成角度、
派给不同 SDK 驱动的下属 agent、在本机或远端执行，并全程留痕、沉淀记忆。

```
用户 ──► 指挥官（Claude Code 主会话）
            │  规划 · 分解 · 派发 · 收集 · 沉淀
            │  commander dispatch / fanout
            ▼
      ┌─────────────────────────────────────┐
      │  Python 派发层 (bin/)               │
      │  路由 → 契约 → 技能 → 线协议        │
      └─────────────────────────────────────┘
            │                    │
      ┌──────────────┐     ┌──────────────────────┐
      │  本地         │     │  远端 40 核 / 20Gi    │
      │ claude       │     │  langchain           │
      │ openai       │     │  crewai (136 包)      │
      │ openai_compat│     │  autogen / hermes    │
      │ browser_use ★│     │                      │
      │ mock         │     │                      │
      └──────────────┘     └──────────────────────┘

  ★ browser_use 的落点由「哪台机器有浏览器」决定，不是看依赖体积
```

## 拿到别的项目里用

### 推荐：技能包（用户零操作）

将commander移动到claude code的skills目录中


然后在 Claude Code 里说一句「用指挥官帮我做 X」。**剩下的它自己会做**：

1. 发现引擎没装（`./.commander/cmd` 不存在）
2. 读技能里的 `references/bootstrap.md`
3. **派一个 subagent** 按清单完成安装 —— 建目录、拷引擎、装依赖、生成 `.env`
4. 逐项验证并给你一份检查清单报告
5. 告诉你唯一需要手工做的一件事：**填 API 密钥**

**技能包不含安装脚本** —— 装什么、怎么装、怎么验证，全写在技能里由 Claude 执行。
（想要脚本式的，见下面「备选：install.sh」。）

技能包结构：

```
commander/
├── SKILL.md              技能定义（Claude 读这个）
├── references/           9 份细则，含 bootstrap.md（安装清单）
└── assets/               ← 不加载进上下文，只被 subagent 引用
    ├── engine/           引擎源码（自包含的关键）
    ├── config/  skills/  配置模板与技能库
    ├── agents/           3 个 subagent 定义
    ├── cmd               启动器
    └── settings-fragment.json
```

`assets/` 里是完整引擎，所以这个包**真正自包含** —— 拷过去就能装，
不需要先有别的什么东西。



### 两种布局共用同一套命令

```bash
./.commander/cmd doctor
./.commander/cmd patrol --oneline
./.commander/cmd dispatch scout -p "调研 X"
```

启动器自己判断引擎在哪，所以技能文档只写一套命令。


---

## 快速开始

**如果你是用技能包装的**（推荐）：什么都不用做 —— 在 Claude Code 里说一句
「用指挥官帮我做 X」，它会自己把下面这些跑完。

**如果你想自己装**（或在改这套东西）：

```bash
uv sync --project bin                    # 引擎（很轻，几秒）

# 五个本地后端（缺一个编制表里就有 agent 用不了）
for b in mock openai_compat claude openai browser_use; do
  uv sync --project bin/backends/$b
done
# 重依赖（crewai/langchain/autogen/hermes）约 1GB，建议丢远端：
#   ./.commander/cmd remote bootstrap && ./.commander/cmd remote sync crewai

./.commander/cmd browser up --install-browser   # browser_use 需要浏览器

cp bin/.env.example bin/.env && chmod 600 bin/.env   # 密钥，然后编辑填入
./.commander/cmd doctor --remote        # 自检
./.commander/cmd dispatch smoke -p "测试"   # 零成本验证全链路

# 真实派发
./.commander/cmd dispatch scout -p "调研 XXX 的现状"

# 多角度并行
./.commander/cmd fanout -t T-001 --angles '[
  {"agent":"scout",   "prompt":"摸清现状"},
  {"agent":"analyst", "prompt":"从性能角度分析"},
  {"agent":"analyst", "prompt":"从可维护性角度评估","model":"pro"}
]'
```

> `make help` 列出全部常用操作。

## 目录职责

**每个目录只有一件事。写错地方会被代码拒绝，不是靠自觉。**

| 目录 | 职责 | 谁能写 |
|---|---|---|
| `.claude/` | 技能定义、subagent、loop.md、settings | 用户 |
| `bin/src/commander/` | 派发层编排逻辑 | 用户 |
| `bin/backends/<sdk>/` | **每个 SDK 一个独立 uv 工程**（依赖互斥） | 用户 |
| `bin/.env` | 密钥，600 权限 | 用户（**子进程读不到**） |
| `config/` | 模型 / 编制 / 策略 | 用户 |
| `memory/` | 长期记忆 | **仅**指挥官 + memory-scout |
| `tasks/` | 任务管理（流水 + 看板 + 档案） | 指挥官 |
| `agents/<id>/work/` | 该 agent **唯一**的工作区 | 该 agent |
| `agents/<id>/outbox/` | 交回的结果 | 该 agent |
| `agents/<id>/logs/` | 完整消息历史 | 派发层 |
| `skills/` | 技能库，按需赋予下属 | 用户 / 指挥官 |
| `remote/` | SSH 主机清单与专用密钥 | 指挥官 |
| `logs/` | 全局日志、巡检简报、契约违规 | 派发层 |

完整说明见 `.claude/skills/commander/references/directory-contract.md`。

## 强制机制

三层，从弱到强：

1. **cwd 隔离** — 子进程 cwd 钉死在 `agents/<id>/work/`
2. **路径白名单** — `guard.assert_writable()`，默认拒绝
3. **bwrap 沙箱** — 文件系统挂只读，只有该 agent 的目录可写

> ⚠️ 第 1 层**只约束相对路径**。实测过：mock 后端用绝对路径成功写出了
> `memory/_mock_probe.md`。所以第 3 层不是锦上添花，是必需品。
> `commander doctor` 会报告沙箱实际状态。

## 已实测验证

| 后端 | 落点 | 状态 |
|---|---|---|
| `mock` | 本地 | ✓ 零成本全链路 |
| `openai_compat` | 本地 | ✓ 真实 API |
| **`browser_use`** | 本地 / 远端 | ✓ 无系统浏览器的机器上跑通（用 playwright chromium） |
| `claude` | 本地 | ✓ Claude Agent SDK → DeepSeek |
| `openai` | 本地 | ✓ OpenAI Agents SDK → DeepSeek |
| `langchain` | 远端 | ✓ LangGraph → DeepSeek |
| `crewai` | 远端 | ✓ 多角色 Crew → DeepSeek |
| `autogen` | 远端 | 见 `logs/` 下最近记录 |
| `hermes` | 远端 | ⚠️ 未实测（见下） |

## 已知限制

- **`hermes` 后端未实测跑通**。`hermes-agent` 只暴露 CLI，程序化驱动走
  `hermes-acp-sdk`，但其 ACP 事件类的确切 Python 名称未能从公开文档完整核实。
  代码写成三级降级（ACP SDK → CLI → 可诊断失败），首次真机跑时看 outbox
  的失败信息就知道该走哪条路。
- **沙箱不防网络访问**。agent 必须能调模型 API，所以没有 `--unshare-net`。
- **无资源限额**。没有 cgroup，一个跑飞的 agent 能吃到机器上限，
  唯一的约束是 `timeout`。
- **`/loop` 有 7 天硬过期**，且错过的触发不补。长任务的状态必须落在文件里。

## 长期循环

```
/loop 20m 跑 `./.commander/cmd patrol --oneline`，有 action 就处理
/loop                    # 读 .claude/loop.md
```

`patrol` 用代码把该看的检查一遍（卡住的任务、未收的结果、落后的索引、
契约违规、预算超支），输出结构化简报。**让每轮唤醒的思考成本恒定**，
不随会话增长而膨胀。

## 安全提示

### 凭据传递

密钥**绝不能出现在命令行参数里** —— `ps aux` 在两端都能看到，还会进 shell 历史。
本地路径本来就是经 stdin 的，远端也统一成同一条路：

```
父进程 ──stdin(JSON RunSpec，含 api_key)──► runner.py
                                              └─ apply_credentials() 注入环境变量
```

`claude-agent-sdk`（驱动 Claude Code CLI）和 `hermes` 是从环境变量读凭据的，
所以 runner **内部**必须设环境变量 —— 只是不能在父进程的命令行上设。

改完必须验证：

```bash
ps -eo args | grep -c '<密钥前缀>'                    # 应为 0
ssh <host> 'grep -c <密钥前缀> ~/.bash_history'      # 应为 0
```

### 其他

- `bin/.env` 权限应为 600，且已在 `.gitignore` 中
- `remote/keys/` 里的私钥同样不入库
- 后端进程在 bwrap 沙箱里读不到 `bin/.env`（遮蔽为 `/dev/null`）
- 远端 bootstrap 会装专用密钥并**实测验证**；装好后建议清空 `password_env`
  并在远端关闭 `PasswordAuthentication`
- **本项目开发期间用过的 DeepSeek key 与远端 root 密码曾以明文出现在对话中，
  建议轮换**

### 沙箱的能力边界

能防：误写工作区内敏感目录、读到密钥、污染宿主 `$HOME`/`/tmp`。

**不能防**：网络访问（agent 必须能调模型 API，所以没有 `--unshare-net`）、
自己 workdir 内的任意行为、资源耗尽（无 cgroup 限额，只有 `timeout` 约束）。
