# 任务协议细则

## 目录

- [两份记录，各司其职](#两份记录各司其职)
- [状态流转](#状态流转)
- [任务档案](#任务档案)
- [拆解角度的方法](#拆解角度的方法)
- [预算控制](#预算控制)
- [收尾与归档](#收尾与归档)

---

## 两份记录，各司其职

用户需求原文：「其中还需要包含任务管理，也是记录在文件中」

| 文件 | 形态 | 用途 | 可变性 |
|---|---|---|---|
| `tasks/registry.jsonl` | append-only 流水 | 机器读：审计、统计、回溯 | **永不改写历史** |
| `tasks/BOARD.md` | 看板 | 人读：一眼看全貌 | 由脚本重新生成 |
| `tasks/active/<id>/task.md` | 单任务档案 | 指挥官读：目标、角度、派发记录 | 随进展更新 |
| `tasks/active/<id>/task.json` | 同上，结构化 | 脚本读 | 同上 |

**为什么流水要 append-only**：任务状态会被改（pending → done），
但"它什么时候被改成 done 的、谁改的"这个信息不能丢。
`registry.jsonl` 只追加，`task.md` 反映当前态。

---

## 状态流转

```
pending ──► active ──► done
              │  ▲
              ▼  │
           blocked          （有外部依赖，等条件满足）
              │
              ▼
        abandoned / failed
```

| 状态 | 含义 | patrol 会怎么处理 |
|---|---|---|
| `pending` | 已建未开始 | info |
| `active` | 进行中 | 超过 1h 没更新 → **action**（卡住了） |
| `blocked` | 被外部条件阻塞 | **action**，提示阻塞原因 |
| `done` | 完成 | 建议归档 |
| `failed` | 确认走不通 | 建议写 `memory/attempts/` |
| `abandoned` | 主动放弃 | 无 |

**`blocked` 必须附原因**：`./.commander/cmd task update T-001 -s blocked -n "等 X 接口上线"`。
没有原因的 blocked 等于没信息。

---

## 任务档案

```bash
./.commander/cmd task new T-001 \
  -g "把查询 P99 降到 100ms 以内" \
  -a "验收：压测 100 QPS 下 P99 ≤ 100ms，且 P50 不退化超过 10%" \
  --angles "定位瓶颈,评估索引方案,评估缓存方案,评估改造代价"
```

- **`-g` 目标**：一句话说清要做成什么
- **`-a` 验收标准**：**必填心智**。没有它，任务永远无法判断何时结束，
  patrol 也无法提示"该收尾了"
- **`--angles`**：拆出的角度，会显示在档案里，也是 `fanout` 的输入

档案里自动累积：

- 每次派发的时间 / agent / 后端 / 模型 / 成败 / tokens / 耗时 / outbox 路径
- 累计 tokens（patrol 据此提示预算）

---

## 拆解角度的方法

「分发多个角度，交给不同的agent，交给不同的大模型去完成」——
关键是**角度之间要有真正的差异**，否则并行只是把同一件事做多遍。

好的角度组合通常是这几个维度交集：

| 维度 | 例子 |
|---|---|
| **事实** vs **判断** | 摸清现状 vs 评估方案 |
| **乐观** vs **悲观** | 可行性 vs 失败模式 |
| **局部** vs **全局** | 单个模块 vs 系统影响 |
| **短期** vs **长期** | 快速见效 vs 可维护性 |
| **成本** vs **收益** | 改造代价 vs 收益上限 |

### 反例

```
❌ {"agent":"scout","prompt":"调研 X"},
   {"agent":"scout","prompt":"看看 X 的情况"},
   {"agent":"scout","prompt":"X 是什么"}

✓ {"agent":"scout",   "prompt":"摸清 X 的现状：谁在用、依赖什么、改动影响面"},
   {"agent":"analyst", "prompt":"从性能角度：瓶颈在哪、上限是多少、证据是什么"},
   {"agent":"analyst", "prompt":"从可维护性角度：改造代价、风险点、回滚难度","model":"pro"},
   {"agent":"reasoner","prompt":"给出两条可选路径并做权衡，指出各自的失败模式"}
```

角度数默认 3（`policy.selection.default_parallel_angles`）。
超过 5 个时先问自己：是不是有的角度可以合并？

---

## 预算控制

```toml
# config/policy.toml
[budget]
max_tokens_per_run  = 200000     # 单次派发
max_tokens_per_task = 1000000    # 单任务累计
```

- `patrol` 在任务用量超过 80% 时给 **warn**
- 任务档案里能看到累计 tokens 与每次派发的明细
- 想省钱：先 `flash`，只在确实需要深推理时才 `pro`

**派发前先估**：一个模糊的大任务是预算黑洞。
先派一个便宜的 `scout` 摸清范围，再决定要不要展开。

---

## 收尾与归档

```bash
# 收结果
./.commander/cmd outbox list --uncollected
./.commander/cmd outbox show agents/scout/outbox/T-001.json
./.commander/cmd outbox collect --all

# 下判断并记录
./.commander/cmd task update T-001 -s done -n "结论：选方案 B，理由见 memory/decisions/"

# 沉淀
./.commander/cmd memory write "..." -k decisions --tags ...

# 归档
./.commander/cmd task archive T-001        # 会自动置为 done 并移入 archive/
./.commander/cmd task board                # 重建看板
```

**归档不等于删除** —— `tasks/archive/<id>/` 保留完整档案，
包含所有派发记录与 outbox 引用。这是审计的最后一环。
