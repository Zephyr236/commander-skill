# 预算协议细则

## 目录

- [核心理念](#核心理念)
- [四维资源](#四维资源)
- [三层预算](#三层预算)
- [判定规则](#判定规则)
- [Agent 自报进度](#agent-自报进度)
- [指挥官的决策动作](#指挥官的决策动作)
- [命令速查](#命令速查)
- [决策历史与复盘](#决策历史与复盘)

---

## 核心理念

**预算不是刹车，是信号。**

硬上限谁都会设。难的是：任务跑到一半，花了不少，你到底该继续投、
加钱、换路子、还是砍掉？——这个判断不该靠感觉。

所以系统做三件事：

1. **持续测量** —— 每次派发都记下四维消耗，不靠估算
2. **结构化上报** —— 到水位就生成状态报告，把"该做决策了"主动摆到你面前
3. **给出建议** —— 基于「消耗 vs 进展」算出建议动作，并说明理由

### ★ 一条最重要的规则

> **消耗大而进展小时，重规划，不要加预算。**

给一个走错路的策略加预算，只是让它错得更贵。钱能解决的是"路径对但不够花"，
解决不了"路径本身有问题"。系统会在这种情况下明确建议 `replan`，
而不是把预算翻倍了事。

---

## 四维资源

| 维度 | 含义 | 单位 |
|---|---|---|
| `tokens` | token 消耗（输入+输出） | 个 |
| `cost_units` | **经济成本代理量** | 成本单位 |
| `wall_seconds` | 墙钟时间 | 秒 |
| `core_seconds` | 算力（墙钟 × 核数） | 核秒 |

判定时取**最紧张的那一维**——只要有一维逼近上限，就该做决策了。

### 关于经济成本

不要求你提供精确单价。按模型的 `cost_tier` 折算成「成本单位」：

```
cost_units = tokens / 1M × 权重
权重：free=0  cheap=1  standard=3  premium=9
```

这一维**永远可用、跨模型可比**，足以支撑「用贵的模型值不值」这类判断。

如果 `config/models.toml` 里给某个模型填了真实单价：

```toml
[models.flash]
pricing = { input = 0.14, output = 0.28, cached_input = 0.014 }   # $/1M tokens
```

就自动换成真钱（`cost_usd`），决策逻辑不变——只是数字从相对量变成绝对量。

权重可在 `config/policy.toml` 的 `[budget.cost_weights]` 里改。

---

## 三层预算

| 层 | 范围 | 在哪配 |
|---|---|---|
| `global` | 跨所有任务的总盘 | `policy.toml` 的 `[budget.global]` |
| `task` | 单个任务 | `policy.toml` 的 `[budget.task]`，可被单个任务覆盖 |
| `agent` | 单个下属 | `policy.toml` 的 `[budget.agent]`，可被 `agents.toml` 覆盖 |

每层四维独立设限，**某一维省略 = 该维不限**。

```toml
[budget.task]
tokens       = 3000000
cost_units   = 9
wall_seconds = 14400
runs         = 60
# core_seconds 省略 → 算力这一维不限
```

单独给某个任务放宽：

```bash
./.commander/cmd budget set T-001 --tokens 8000000 --runs 120
./.commander/cmd budget set T-001 --clear      # 回到默认
```

> ⚠️ **读完状态报告再做这个决定。** 如果报告说「消耗大、进展小」，
> 正确答案是重规划，不是放宽限额。

---

## 判定规则

按优先级从上往下，**第一条命中的生效**：

| 条件 | 建议 | 为什么 |
|---|---|---|
| 预算耗尽 + 进展 < 50% | **重规划** | 路径问题，加预算只会更贵 |
| 预算耗尽 + 进展 ≥ 50% | 增加预算 | 快到了，值得追加 |
| 首轮且无进展信号 | 继续 | 样本不足，不下结论 |
| **已用 ≥ 50% 且进展 < 25%** | **重规划** | ★ 核心规则 |
| 烧钱效率 > 2.5x 且已用 > 35% | 重规划 | 单位产出的代价太高 |
| agent 自报置信度 < 35% 且已用 > 40% | 重规划 | 它自己都没把握，继续投是在赌 |
| 有明确障碍 且已用 ≥ 30% | 调整策略 | 针对障碍调整即可，不必推倒重来 |
| 进展 ≥ 60% 且已用 ≥ 80% | 增加预算 | 路径对，只是不够花 |
| 其余 | 继续 | — |

**烧钱效率** = 已用预算比例 ÷ 有效进展。`1.0x` 是理想值（花一半做完一半），
`2.5x` 以上认为路径有问题。

阈值 `warn=0.60` / `critical=0.85` 可在 `policy.toml` 的 `[budget.thresholds]` 调。

---

## Agent 自报进度

机械信号（轮数、产出字数、产物数）**总是有**，但它分不清「在收敛」和「在绕圈」。
所以还会要求 agent 在回复末尾附一段状态块：

```
<<<COMMANDER_STATUS
{"completion": 0.6, "confidence": 0.7,
 "blockers": ["缺少压测环境"],
 "summary": "已完成接口梳理，还差压力测试"}
>>>
```

- 这段由派发层从正文里**剥掉**，不会混进交付物
- 解析结果存进 `result.raw.declared_progress` 和任务流水
- 原始记录在 `agents/<id>/logs/*.jsonl` 里完整保留（审计用）
- **当前不可关闭** —— 「消耗大进展小」这个判断依赖它。
  拿不到自报时系统会退化为按产出字数猜进展（`Progress.effective`）

机械信号与自报**两者都保留**，不是二选一。

---

## 指挥官的决策动作

五个选项，含义不同：

| 动作 | 什么时候用 | 具体做什么 |
|---|---|---|
| `continue` | 一切正常 | 不动，继续派发 |
| `adjust` | 有明确障碍，路径本身没错 | 针对障碍调整：换技能、加 token、拆细一步 |
| **`replan`** | **消耗大而进展小** | 换角度、换 agent、换模型、换解法。**不是加钱** |
| `increase` | 路径被证明是对的（进展良好），只是预算不够 | 放宽限额，或拆成子任务分摊 |
| `abort` | 投入产出不划算 | 停掉，把教训写进 `memory/attempts/` |

### 记录决策

```bash
./.commander/cmd budget decide T-001 replan \
  -r "两轮都在原地绕，完成度停在 10%。换 analyst 从失败模式反推。"
```

系统会把你选的动作用它自己的建议做对比（历史上标 ✓/✗），
并快照当时的现场（水位、进展、效率）。

### 做完之后回填结果

```bash
./.commander/cmd budget decide T-001 replan -r x \
  --outcome "换 analyst 后一轮定位到根因，完成度从 10% 跳到 70%"
```

**这一步别省。** 没有 outcome，决策历史只是流水账；
有了它，才能回答「这类判断我是不是总做错」。

---

## 命令速查

```bash
# 看水位（含三层 + 建议）
./.commander/cmd budget show
./.commander/cmd budget show -t T-001 -a scout
./.commander/cmd budget show -t T-001 --history 10    # 顺带列最近状态报告

# 单独设某个任务的预算
./.commander/cmd budget set T-001 --tokens 5000000

# 记录决策 / 回填结果
./.commander/cmd budget decide T-001 replan -r "为什么"
./.commander/cmd budget decide T-001 replan -r x --outcome "后来怎样了"

# 决策历史
./.commander/cmd budget history
./.commander/cmd budget history -t T-001
```

`patrol` 会自动检查预算，到水位时以 `action` 项提示你。
状态报告落盘在 `tasks/active/<id>/budget/`，历史在 `logs/budget-decisions.jsonl`。

---

## 决策历史与复盘

两份记录，用途不同：

| 文件 | 内容 | 用途 |
|---|---|---|
| `tasks/active/<id>/budget/history.jsonl` | 状态报告流水 | 单任务回溯：「当时水位是多少」 |
| `logs/budget-decisions.jsonl` | 决策流水 | 跨任务复盘：「我的判断准不准」 |

决策流水里同时记着**系统建议**和**你实际选的**。这两者不一致时值得看一眼：
- 你选了 `increase` 而系统建议 `replan` → 你真的确认过路径没问题吗？
- 你选了 `replan` 而系统建议 `continue` → 你看到了什么系统没看到的？

这种分歧是最好的复盘材料，也是把经验沉淀进 `memory/` 的入口。
