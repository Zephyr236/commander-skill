# 现成方案发现协议

## 目录

- [什么时候该去找](#什么时候该去找)
- [三条接入路径](#三条接入路径)
- [完整流程](#完整流程)
- [把一个外部框架接成新后端](#把一个外部框架接成新后端)
- [接到之后：让它变成常备部队](#接到之后让它变成常备部队)
- [反面清单](#反面清单)

---

## 什么时候该去找

用户需求原文：

> 「寻找网上的开源项目是否可以直接使用来完成任务的」
> 「用于寻找agent框架，例如假如是一个代码审计项目，则可以搜索github中
> 开源的代码审计agent，然后派发去使用」
> 「去网上寻找相关的知识 skills 等等用于帮助完成任务」

**触发条件**（满足任一条就该考虑）：

- 任务需要的**能力**现有 agent 都不具备，而且**自建成本高**
- 你在做的是一件"肯定有人做过"的事（代码审计、爬虫、文档转换、特定格式解析）
- 任务里出现了陌生的领域术语，你需要先搞清楚这个领域的标准做法
- 你已经试过一轮自研，效果不好 —— 先看看别人怎么解决的

**先别造轮子，但也别迷信轮子。** 找一个外部依赖引入的长期成本，
经常高于自己写 50 行。

### 标准动作

```bash
./.commander/cmd dispatch browser -t <task> -p "
调研：<具体问题>。
优先找官方文档和源码，不要只看博客。
产出：结论 + 可点击来源 + 时效性说明。"
```

用 `browser`（browser-use 后端）或 `web-scout` 模板。
**落点由「哪台机器有浏览器」决定** —— 本机有就本地跑，没有才落远端。
本机没浏览器时先跑一次 `./.commander/cmd browser up --install-browser`。

---

## 三条接入路径

找到候选之后，判断能怎么接。**只有三种**，不要发明第四种：

| 路径 | 适用 | 判断方法 | 代价 |
|---|---|---|---|
| **装成新后端** | Python 库，有稳定编程接口 | 能 `import`、能调一个函数拿结果 | 中：要写 runner.py |
| **当子进程调** | 有 CLI 或 HTTP 服务 | 有命令行入口、输出可解析 | 低：写个适配器 |
| **直接抄做法** | 太大 / 依赖脏 / 不兼容 | —— | 最低，但要求你看懂它的思路 |

### 关键约束：依赖冲突不是问题

我们的每个后端是**独立 uv 工程**（实测过 `crewai` 和 `hermes` 的 pydantic
要求互斥，只能隔离）。所以"依赖太重"和"钉死版本"**不构成阻塞**。

真正的阻塞点是：

- **它要求独占资源**（比如必须占用某个端口、必须接管进程）
- **它强制用某个模型能力**（比如必须要视觉模型，而我们默认文本）
- **它要求系统级依赖**而你装不上（比如必须要 Chrome 而机器上没有）

---

## 完整流程

```
① 明确问题
   └─ 把模糊需求写成一个可被搜索回答的句子

② 派人去查（browser agent，落有浏览器的那台）
   └─ 产出：候选清单 + 每个的来源与维护状态

③ 评估（vet-solution 技能）
   ├─ 能不能接？→ 三条路径选一条
   ├─ 能不能用我们的模型？→ 检查 base_url / 结构化输出 / 视觉要求
   ├─ 维护状态？→ commit 日期、issue 响应、有没有进维护模式
   └─ 代价？→ 依赖重量、系统要求、license

④ 决策
   ├─ 采用 → 走下面的接入步骤
   ├─ 只抄做法 → 自己实现，把它写进 memory 作为参考
   └─ 否掉 → **也要写 memory**，记下否掉的理由，避免下次重新调研

⑤ 接入并验证
   └─ 端到端跑通一次，再纳入编制

⑥ 沉淀
   └─ memory/facts 记「什么工具适合什么场景」
      memory/attempts 记「试过但没采用，为什么」
```

---

## 把一个外部框架接成新后端

以「发现了一个开源代码审计 agent」为例，完整步骤：

### 1. 先摸清它的接口

```bash
./.commander/cmd dispatch browser -t T-001 -p "
找出 <项目> 的 Python API：
1. PyPI 包名与最新版本
2. 最小可用示例（从 README 或 examples/ 抄原文）
3. 是否支持自定义 base_url / 非 OpenAI 模型
4. 是否强制结构化输出或需要视觉模型
5. 核心依赖有多少、有没有系统级要求
把源码里 LLM 适配层的关键行贴出来，不要只转述 README。"
```

**第 3、4 点是重点。** 大多数 agent 框架默认假设你用 OpenAI，
接自定义端点时会踩两个坑：强制 `response_format`、默认要截图。

### 2. 建工程

```bash
mkdir -p bin/backends/<名字>
# pyproject.toml：只写这个 SDK 的依赖
# runner.py：实现 stdin/stdout 线协议
# _probe.py：验证能 import
```

`runner.py` 的骨架照抄任何一个现成后端（`openai_compat` 最简单）。
核心是三段：

```python
from commander_protocol import Spec, Usage, emit, fail, ok, run_main

def main(spec: Spec) -> None:
    ...           # 把它的调用翻译成 emit() / ok() / fail()
    ok(text, usage=usage, model=spec.model_id, backend="<名字>")

if __name__ == "__main__":
    run_main(main, "<名字>")
```

**指挥官侧零改动** —— 这就是把 SDK 差异收敛到线协议的收益。

### 3. 注册

- `bin/src/commander/config.py` 的 `BACKENDS` 加一项
- 重依赖加进 `HEAVY_BACKENDS`（→ 远端）；**需要浏览器的加进 `NEEDS_BROWSER`** ——
  这类后端的落点由「哪台机器有浏览器」决定，跟依赖体积无关
- `cli.py` 里 `backend list` 的 SDK 名称映射加一项

### 4. 装依赖并验证

```bash
./.commander/cmd remote sync <名字>        # 重依赖落远端
./.commander/cmd backend probe <名字>      # 真的能 import 吗
./.commander/cmd agent-new <agent-id> --backend <名字> --model flash -d "职责"
./.commander/cmd dispatch <agent-id> -p "端到端验证"
```

### 5. 沉淀

```bash
./.commander/cmd memory write "<名字> 接入要点" -k facts \
  --tags 后端,<名字>,接入

./.commander/cmd memory attempt "评估 <名字> 作为 <用途>" \
  -a "查了它的 API 与依赖" \
  -o "接成了后端 / 只抄了做法 / 否掉了" \
  --why "..." \
  --retry-when "..."
```

---

## 接到之后：让它变成常备部队

一条完整的正反馈回路：

```
browser 上网找
   ↓
发现一个代码审计 agent
   ↓
评估：能接（有 Python API + 支持 base_url）
   ↓
写 runner.py，接到远端
   ↓
./.commander/cmd agent-new code-auditor --backend <它> --model pro \
    --skills critique,analyze -d "专攻代码审计，产出必须带可复现的 PoC"
   ↓
以后遇到审计任务直接 dispatch code-auditor
   ↓
它的经验进 memory/attempts
```

**这正是「按需定制 agent」的完整形态** —— 不只是换后端换模型，
而是**为新领域引入新能力，然后固化成编制**。

同理，找到现成的**技能包**（SKILL.md）时：

```bash
./.commander/cmd skill-new <名字> -d "<从哪抄的、做什么>"
# 把它的内容整理进 skills/<名字>/SKILL.md
# 然后赋给相关 agent：dispatch <agent> --extra-skills <名字>
```

---

## 反面清单

| 别做 | 为什么 |
|---|---|
| 只看 README 就下结论 | README 是宣传材料。去看源码里的 LLM 适配层和 changelog |
| 引入一个 1 年没更新的项目 | 现在能用，但出问题没人修 |
| 为了一个一次性任务引入重依赖 | 长期维护成本高于自己写 |
| 不检查 license | GPL 会传染，商用项目要避开 |
| 引进来就不管了 | 接完要写 memory，否则下次还要重新调研一遍 |
| 拿三手来源当依据 | AI 生成的技术文章经常是编的。要一手来源 |
| 否掉之后不记录 | **否掉的理由和采用的理由一样有价值** —— 不记，下次会重新调研同一个东西 |
