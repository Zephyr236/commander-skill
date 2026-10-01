# commander — 指挥官派发层

把任务交给不同 Agent SDK 驱动的下属，本地或远端执行，全程留痕。

## 组成

```
bin/
├── pyproject.toml        编排层自身的依赖（很轻，只有 typer/rich/pydantic/httpx）
├── .env                  密钥（600 权限，gitignored）
├── src/commander/        编排逻辑
│   ├── workspace.py        所有目录路径的唯一定义处
│   ├── guard.py            目录契约的强制执行（三道防线）
│   ├── sandbox.py          bubblewrap 沙箱
│   ├── config.py           models/agents/policy.toml + .env 加载
│   ├── schemas.py          ★ 子进程线协议（所有 SDK 差异止步于此）
│   ├── router.py           选模型 + 决定本地/远端
│   ├── dispatch.py         派发主流程
│   ├── backends.py         后端进程启动 + 就绪度查询
│   ├── record.py           消息历史留痕（jsonl + md + outbox）
│   ├── skills.py           技能装载与注入
│   ├── memory.py           长期记忆
│   ├── tasks.py            任务管理
│   ├── patrol.py           巡检
│   ├── ssh_runner.py       远端执行
│   └── cli.py              命令行界面
└── backends/             ★ 每个 SDK 一个独立 uv 工程
    ├── _shared/commander_protocol.py   纯标准库，靠 sys.path 共享
    ├── claude/  openai/  langchain/  crewai/  autogen/  hermes/
    ├── openai_compat/                   纯 httpx，兜底
    └── mock/                            零依赖，自测
```

## 为什么每个后端独占一个工程

**不是洁癖，是实测的硬约束**：

```
crewai >=1.15.22     →  pydantic >=2.11.9,<2.13
hermes-agent 0.19.0  →  pydantic ==2.13.4
```

`uv` 直接报 unsatisfiable。三个互不兼容的 pydantic 世界，只能隔离。

## 常用命令

```bash
./.commander/cmd --help          # 全部子命令
./.commander/cmd doctor --remote # 环境自检
./.commander/cmd dispatch smoke -p "测试"   # 零成本全链路
```

## 开发提示

- 改 `_shared/commander_protocol.py` 会影响**所有**后端 —— 线协议是共同契约
- 改 `ssh_runner.sync()` 的 payload 列表时，**必须同步改 `_payload_signature()`**，
  否则会出现"改了文件但指纹没变 → 同步跳过 → 远端跑旧代码"
- `guard.audit_argv` 只做尽力而为的检查，真正的隔离在 `sandbox.py`
