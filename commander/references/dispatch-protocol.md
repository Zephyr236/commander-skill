# 派发协议细则

## 目录

- [一次派发发生了什么](#一次派发发生了什么)
- [子进程线协议](#子进程线协议)
- [为什么要一个 SDK 一个 venv](#为什么要一个-sdk-一个-venv)
- [九个后端速查](#九个后端速查)
- [调参指南](#调参指南)
- [新增一个后端](#新增一个后端)
- [错误分类与重试](#错误分类与重试)

---

## 一次派发发生了什么

```
./.commander/cmd dispatch <agent> -p '<指令>'
  │
  ├─① 路由    router.route()   选模型 + 决定本地/远端，附带理由
  ├─② 技能    SkillLibrary     读 skills/<name>/SKILL.md，剥 frontmatter
  ├─③ 参数    resolve_max_tokens()  套用模型的 max_tokens 下限
  ├─④ 契约    guard.resolve_workdir()  钉死 cwd；Sandbox.wrap() 套 bwrap
  ├─⑤ 构造    RunSpec          序列化成一行 JSON
  ├─⑥ 启动    uv run --project <引擎目录>/backends/<sdk> python runner.py
  ├─⑦ 中继    stdout JSONL → RunRecorder（流式落盘）
  └─⑧ 收尾    finalize() → outbox/*.json + 任务流水
```

失败也走 ⑧ —— **失败必须留下原因**，那是下次决策的依据。

---

## 子进程线协议

指挥官与所有 SDK 之间**唯一的接口**。所有 SDK 差异止步于此。

```
父 → 子  stdin   一行 JSON：RunSpec
子 → 父  stdout  JSONL 事件流，最后一行必须是 result
子 → 子  stderr  自由文本，原样落到 agents/<id>/logs/
```

### 事件类型

| type | 含义 |
|---|---|
| `start` | 后端就绪，附工作目录与契约说明 |
| `thought` | 推理过程（`reasoning_content` / thinking） |
| `text` | 正文增量 |
| `tool_call` / `tool_result` | 工具调用与返回 |
| `artifact` | 产出文件 |
| `usage` | token 用量 |
| `error` | 可恢复错误 |
| `log` | 自由日志 |
| `result` | **最终结果，必须是最后一行** |

### RunResult 归一化字段

任何 SDK 的结果都被压成同一结构，指挥官只读这个：

```python
ok: bool                 # 成败
text: str                # 正文（不含推理）
reasoning: str           # 推理过程，已分离
tool_calls: list         # 工具调用
usage: Usage             # input/output/reasoning/cached tokens
artifacts: list[str]     # 相对 workdir 的产物路径
error / error_kind / retryable
model / backend / turns / duration_s
raw: dict                # SDK 原始信息（如 session_id）
```

**`reasoning` 必须与 `text` 分开** —— 混在一起会污染最终结论。
这是实测踩过的：`deepseek-flash` 是推理模型，某些调用方式下推理内容会混进正文。

---

## 为什么要一个 SDK 一个 venv

这不是洁癖，是**实测出来的硬约束**：

```
crewai >=1.15.22     →  pydantic >=2.11.9,<2.13
hermes-agent 0.19.0  →  pydantic ==2.13.4
```

两者**无法共存**，`uv` 直接报 unsatisfiable。实测各后端解析结果：

| 后端 | 包数 | pydantic |
|---|---|---|
| claude / openai / langchain / autogen | 28–44 | 2.13.5 |
| **crewai** | **136** | 2.12.5 |
| **hermes** | 62 | 2.13.4 |

三个互不兼容的 pydantic 世界。所以每个后端独立成 uv 工程：

```
bin/backends/
├── _shared/commander_protocol.py   ← 纯标准库，靠 sys.path 共享（零依赖风险）
├── claude/{pyproject.toml,runner.py,_probe.py}
├── openai/  langchain/  crewai/  autogen/  hermes/
├── openai_compat/   ← 纯 httpx，兜底任何 OpenAI 兼容端点
└── mock/            ← 零依赖，端到端自测
```

顺带的好处：可按需安装、能整份同步到远端执行、依赖互不污染。

---

## 九个后端速查

| 后端 | 包名 | 关键坑 |
|---|---|---|
| `claude` | `claude-agent-sdk` (import `claude_agent_sdk`) | 本质是驱动 Claude Code CLI 子进程，CLI 随包捆绑；用 `CLAUDE_CONFIG_DIR` 隔离，避免读到指挥官全局配置 |
| `openai` | `openai-agents` | **必须** `set_tracing_disabled(True)`（否则连 OpenAI tracing 端点然后 401）；**必须**走 `OpenAIChatCompletionsModel`；base_url 挂 `AsyncOpenAI` 实例上 |
| `langchain` | `langchain` + `langgraph` | `create_react_agent` **已废弃** → 用 `langchain.agents.create_agent`；接兼容端点**必须显式** `use_responses_api=False` |
| `crewai` | `crewai` | 136 个包，pydantic 钉 2.12.5；**必须落远端** |
| `autogen` | `autogen-agentchat` + `autogen-ext[openai]` | ⚠️ **上游已进入维护模式**（微软转向 MAF），`pyautogen` 已废弃。仍然可用但优先选别的 |
| `hermes` | `hermes-agent[acp]` + `hermes-acp-sdk` | 本体只暴露 CLI，程序化驱动走 ACP 子进程 + 事件流；本项目里唯一未能实测跑通的 |
| `browser_use` | `browser-use` | **不下载浏览器**，通过 CDP 连已运行的 Chromium。落点由「哪台机器有浏览器」决定；都没有可用 `browser up --install-browser` 下一个。拉进 57 个钉死的依赖。接非 OpenAI 端点要 `dont_force_structured_output=True` + `use_vision=False` |
| `openai_compat` | 纯 `httpx` | 无 SDK 依赖，最便宜的健康检查与对照组 |
| `mock` | 无 | 零成本验证链路 |

### 后端就绪检查

```bash
./.commander/cmd backend list          # 哪些装了
./.commander/cmd backend probe crewai  # 真能 import 吗
./.commander/cmd backend install claude
```

`.venv` 存在 ≠ 依赖装全了。`probe` 会真的 import 一次。

---

## 调参指南

### max_tokens —— 有自动下限

实测坑：`deepseek-flash` 是**推理模型**，给 16 个 token 会被 `reasoning_content`
吃光，返回空正文 + `finish_reason=length`。

所以 `router.resolve_max_tokens()` 会自动兜底：

```python
val = max(requested_or_agent_default, model.max_tokens_floor, 1024)
val = min(val, model.max_output)
```

`max_tokens_floor` 在 `config/models.toml` 里按模型配置（推理模型设 4096）。

### timeout

优先级：`--timeout` > `agents.toml` 的 `timeout` > `policy.budget.default_timeout`。

经验值：轻量问答 300s；带工具的多轮 1800s；CrewAI 多角色流水线 5400s。

### 技能注入成本

`./.commander/cmd skills` 会显示每个技能的估算 token 数（正文长度/4）。
技能正文会完整注入 system prompt，**别把巨型技能赋给便宜 agent**。

### 并行度

```toml
[concurrency]
local  = 2    # 本机 2 核 / 可用 2Gi，保守取值
remote = 8    # 远端 40 核 / 可用 20Gi
```

`fanout --parallel` 默认取 `concurrency.remote`。

---

## 新增一个后端

1. `bin/backends/<sdk>/pyproject.toml` —— 只写这个 SDK 的依赖
2. `bin/backends/<sdk>/runner.py` —— 三段式：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))
from commander_protocol import Spec, Usage, Buffer, emit, fail, ok, run_main

def main(spec: Spec) -> None:
    ...   # 把 SDK 调用翻译成 emit() / ok() / fail()

if __name__ == "__main__":
    run_main(main, "<sdk>")     # 兜住一切异常并转成合法的 result 事件
```

3. `bin/backends/<sdk>/_probe.py` —— 打印 `OK ...` 并返回 0
4. `config/agents.toml` 里加一个用它的 agent
5. `./.commander/cmd backend install <sdk>` 然后 `./.commander/cmd backend probe <sdk>`

**指挥官侧零改动** —— 这就是协议收敛的价值。

---

## 错误分类与重试

```python
classify_error() → (error_kind, retryable)
```

| error_kind | 重试 | 说明 |
|---|---|---|
| `timeout` / `rate_limit` / `server_error` / `connection` | ✓ | 网络与负载类，重试有意义 |
| `auth` | ✗ | 密钥问题，重试只会重复失败 |
| `bad_request` | ✗ | 参数错，重试纯烧钱 |
| `context_overflow` | ✗ | 输入太长，要换策略不是重试 |
| `budget` | ✗ | 超预算 |
| `guard_violation` | ✗ | 目录契约违规，是设计问题 |
| `backend_missing_dep` | ✗ | 依赖没装 |
| `no_result` | ✗ | 后端退出但没给 result，看 stderr |

重试策略在 `config/policy.toml` 的 `[retry]`：最多 2 次，指数退避。
重试时会换新 `run_id`，所以每份尝试都有独立的记录文件。
