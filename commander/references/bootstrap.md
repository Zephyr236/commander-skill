# 自举安装清单

## 目录

- [这是什么](#这是什么)
- [给主会话的指令](#给主会话的指令)
- [给安装 subagent 的完整指令](#给安装-subagent-的完整指令)
- [检查清单](#检查清单)
- [失败处理](#失败处理)

---

## 这是什么

指挥官技能被复制进一个项目时，**只有技能文件本身**（`SKILL.md` + `references/` +
`assets/`），引擎还没落到项目里。这份文档就是让 Claude 自己把它装好。

> **`{技能目录}` 指技能被安装后的位置**，形如 `<项目>/.claude/skills/commander/`。
> 那里面应该同时有 `SKILL.md`、`references/` 和 `assets/`。
> 如果只有前两个（例如你在看**源工作区**而不是安装后的副本），
> `assets/` 在打包产物里 —— 用 `make dist` 生成，或从
> `dist/commander/` 里取。

触发条件：**`./.commander/cmd` 不存在**（且向上也找不到 `.commander-root`）。

用户不需要跑任何安装脚本 —— 他只需要把技能目录拷进 `.claude/skills/`，
然后在 Claude Code 里说一句话。

---

## 给主会话的指令

当 `./.commander/cmd` 不存在时，**不要自己一步步装**。安装过程输出冗长
（uv 的下载日志、编译输出），会污染你的上下文，而你还要留着上下文当指挥官。

正确做法：

1. 确定两个路径：
   - `技能目录` = 本技能所在目录的绝对路径（`references/` 的上一级）
   - `项目根` = 当前工作目录（若向上能找到 `.git/`，用它的位置）

2. 用 Agent 工具派**一个** subagent，`subagent_type: general-purpose`，
   prompt 用下面「给安装 subagent 的完整指令」那一节，把 `{技能目录}` 和
   `{项目根}` 填成实际路径。

3. 收到 subagent 报告后：
   - 全部通过 → 告诉用户装好了，并直接开始当指挥官
   - 有失败项 → 把失败项原样转述给用户，不要自己重试（重试前先搞清原因）

---

## 给安装 subagent 的完整指令

> 以下内容整段作为 subagent 的 prompt。`{技能目录}` 与 `{项目根}` 由主会话填入。
> 如果用户在主对话里给了凭据（API key / base url / SSH 账号密码），
> **也一并填进 `{用户提供的凭据}`** —— 第 10 步会用到。没有就给空。

你是**指挥官安装手**。把技能自带的引擎装进目标项目，并逐项验证。

**目标路径**
- 技能目录：`{技能目录}`
- 项目根：`{项目根}`
- 工作区：`{项目根}/.commander`
- 脚本：`{项目根}/.commander/cmd`

按顺序执行，**每步做完立刻验证**。任何一步失败就停止，报告已完成的步骤和
失败原因，不要继续往下做，也不要自己发明别的做法。

### 步骤

**1. 建目录骨架**

```
{工作区}/
├── bin/  config/  skills/
├── memory/{facts,attempts,decisions,entities,journal}
├── tasks/{active,archive}
├── agents/  logs/
└── remote/{keys,staging,logs}
```

**2. 拷引擎**：`{技能目录}/assets/engine/` → `{工作区}/bin/`
- 排除 `__pycache__/`、`*.pyc`、`.venv/`
- 验证：`{工作区}/bin/pyproject.toml` 存在

**3. 拷配置**：`{技能目录}/assets/config/*` → `{工作区}/config/`
- 验证：`models.toml` `agents.toml` `policy.toml` 都在

**4. 拷技能库**：`{技能目录}/assets/skills/*` → `{工作区}/skills/`
- 验证：至少 8 个技能目录，每个含 `SKILL.md`（analyze/critique/plan/recon/summarize/vet-solution/web-research/write）

**5. 写启动器**：`{技能目录}/assets/cmd` → `{工作区}/cmd`，`chmod 755`
- 验证：文件可执行

**6. 写工作区标记**：新建 `{工作区}/.commander-root`，内容随意一行文字
- 这个文件决定工作区根在哪，**不能漏**

**7. 空目录占位**：给上面每个空目录放一个空的 `.gitkeep`

**7b. 远端主机清单**：写一份空的 `{工作区}/remote/hosts.toml` 模板。

- 不写它 `commander remote` 就没有主机清单（`check`/`bootstrap`/`sync` 全不可用）
- **要写空模板，不要编造主机** —— 用户没给凭据就没有主机
- 内容：一段注释说明格式 + `[hosts.<名字>]` 的样例（注释掉的）
- 验证：文件存在且能被 `tomllib` 解析

**8. 装引擎依赖**
```bash
uv sync --project {工作区}/bin
```
- 前提：`uv` 已安装。若没有 → **先想办法把它装上**，按下面的顺序试：

  ```bash
  # ① 有 curl 且 astral.sh 可达（最常见）
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # ② 有 wget 但没 curl —— 或者 astral.sh 被墙（实测见过 403）
  #    改从 GitHub Releases 拿静态二进制
  U=https://github.com/astral-sh/uv/releases/latest/download
  wget -qO /tmp/uv.tgz "$U/uv-x86_64-unknown-linux-gnu.tar.gz"
  tar xzf /tmp/uv.tgz -C /tmp
  install -m755 /tmp/uv-x86_64-unknown-linux-gnu/uv  /usr/local/bin/uv
  install -m755 /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx

  # ③ ② 也失败时（实测见过 download 端点返回 HTTP 500）
  #    走 GitHub **API** 的 asset 端点 —— 它和 download 端点不是同一条链路
  python3 - <<'PY'
  import json, urllib.request
  api = "https://api.github.com/repos/astral-sh/uv/releases/latest"
  rel = json.load(urllib.request.urlopen(api, timeout=30))
  a = next(x for x in rel["assets"] if "x86_64-unknown-linux-gnu.tar.gz" in x["name"])
  urllib.request.urlretrieve(a["browser_download_url"], "/tmp/uv.tgz")
  print("下载完成，字节数", a["size"])   # 可与实际大小对照，确认没被截断
  PY
  tar xzf /tmp/uv.tgz -C /tmp
  install -m755 /tmp/uv-x86_64-unknown-linux-gnu/uv  /usr/local/bin/uv
  install -m755 /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx

  # ④ 验证
  uv --version
  ```

  ⚠️ **实测踩过**：有台机器既没 `curl` 也没 `pip`，`astral.sh` 返回 403，
  **而且 GitHub 的 `releases/latest/download` 端点也返回 HTTP 500**。
  三条路要**依次试**，任一条通了就停 —— 别只写一条。**先探测再选**：
  ```bash
  command -v curl wget; python3 -c "import urllib.request; \
    print(urllib.request.urlopen('https://astral.sh/uv/install.sh',timeout=10).status)"
  ```

  装 uv 是**改变用户机器状态**的操作，动手前跟主会话说一声。
- 验证：`{工作区}/bin/.venv/bin/commander` 存在

**9. 装本地后端（全装）**

```bash
for b in mock openai_compat claude openai browser_use; do
  uv sync --project {工作区}/bin/backends/$b
done
```

- 这五个是**默认本地后端**，都得装，否则编制表里一半 agent 用不了
- 体积：`mock` 零依赖；`openai_compat` 3M；`openai` 55M；`claude` 263M；`browser_use` 226M
- `browser_use` 也在列 —— 它能不能跑取决于**有没有浏览器**，不取决于依赖体积
- **不要装** `crewai` / `autogen` / `hermes` / `langchain`：合计 1GB+，
  本来就该跑在远端（`remote bootstrap` + `remote sync`）
- 若磁盘紧张，只装 `mock` + `openai_compat` 也能跑，但要在报告里说明
- 验证：`mock` / `claude` / `browser_use` 的 `.venv` 都存在

**9b. 准备浏览器（`browser_use` 需要）**

⚠️ **`uv sync` 装 browser-use 不会带任何浏览器。** 它的 61 个依赖里没有
playwright，venv 里也导不进来 —— 它连的是**一个已运行的 Chromium 系浏览器**。

一条命令搞定，它自己判断该做什么：

```bash
./.commander/cmd browser up --install-browser
```

| 情况 | 它做什么 |
|---|---|
| 系统有 Chrome / Chromium / Edge / Brave | 直接起 |
| 只有 playwright 缓存的 chromium | 直接起 |
| **什么都没有** | 下 chromium（~187MB）再起 |

下载走 `uvx playwright install chromium`，装到 `~/.cache/ms-playwright`，
**不需要 root、不动系统包**。

**验收**：`./.commander/cmd browser status` 显示 `✓ 本机: UP Chrome/...`。

**为什么不能指望 browser-use 自己的兜底**：它确实内置了一个
（`local_browser_watchdog.py` 里会跑 `uvx playwright install chromium --with-deps`），
但**只给 60 秒超时**，而冷启动要下 233MB + apt 装系统依赖 —— 实测必然超时。

**跑不起来就跳过这步**，在报告里说明 `browser` agent 暂不可用，**别卡在这**。

**如果这台机器不适合装浏览器**（磁盘紧、要 root、没网），
可以在远端准备浏览器，让 `browser` agent 落远端：

```bash
./.commander/cmd remote bootstrap
./.commander/cmd remote sync browser_use
./.commander/cmd browser up --host <主机名>    # 在远端起
```

`browser` agent 的 `target = "auto"` —— **落点由「哪台机器有浏览器」自动决定**，
不写死。`backend list` 会告诉你缺什么（它真的去查浏览器，不只看依赖装没装）。

**10. 生成 .env（并在用户已提供凭据时写入）**

先铺一份空模板：

```bash
cp {工作区}/bin/.env.example {工作区}/bin/.env
chmod 600 {工作区}/bin/.env
```

然后分两种情况：

**A. 用户在主对话里已经给了凭据**（key / base url / SSH 账号密码）——
主会话会把这些转达给你。用 `config apply` 从 stdin 写入：

```bash
cd {项目根} && ./.commander/cmd config apply <<'JSON'
{"providers": {"deepseek": {"api_key": "sk-...", "base_url": "..."}},
 "hosts": {"osboxes": {"host": "192.0.2.10", "user": "root", "password": "..."}}}
JSON
```

写完跑 `./.commander/cmd config check` 确认真的能用。

**B. 用户没提凭据** —— 保持空模板，在报告里列为"需要用户做的事"。
**绝不自己编造或填入任何密钥。**

- 验证：权限是 `600`；写了凭据的话 `config check` 全绿

**11. 装 subagent**
`{技能目录}/assets/agents/*.md` → `{项目根}/.claude/agents/`
- 目录不存在就建
- **同名文件跳过，不要覆盖** —— 用户可能改过

**12. 合并 Claude Code 设置**（可选，先问主会话）

> ⚠️ **合并后本次会话不会立即生效**。Claude Code 只在会话启动时读配置文件，
> 所以权限与钩子要**下次启动**才激活（或让用户打开一次 `/hooks` 菜单重载）。
> 做完这步要**明确告诉用户要重启**，否则他会以为没生效。
>
> **权限语法不用怀疑**：片段里的 `Bash(cmd:*)` 与 `Read(path)` 都是官方支持的写法
> （文档原话：`:*` suffix is an equivalent way to write a trailing wildcard；
> `path` or `./path` 都表示相对当前目录）。两者都已核对过，别改成别种写法。
>
> 另外：deny 规则 `Read(bin/.env)` 一旦生效，**你自己也读不了那个文件** ——
> 这是设计如此（保护密钥），不是故障。要确认密钥状态用 `config show`（自动打码）。

`{技能目录}/assets/settings-fragment.json` 里的权限与钩子合并进
`{项目根}/.claude/settings.json`：
- 权限取**并集**，已有的条目一条不能丢
- 钩子按 `command` 去重后追加
- 文件不存在就新建
- 原文件解析不了 → 备份成 `.json.broken` 再重建，**绝不静默覆盖**
- 这一步会改动用户的配置，**做之前先问**

**13. 端到端验证（最重要的一步）**

```bash
cd {项目根} && ./.commander/cmd dispatch smoke -p "安装自检" -t bootstrap-check
```
- 必须看到「成功」字样
- 验证产物：`{工作区}/agents/smoke/outbox/bootstrap-check.json` 存在
- 这一步同时验证了：引擎能跑、目录契约生效、留痕链路通、outbox 写得进去

**14. 收尾，让工作区处于干净状态**

```bash
./.commander/cmd memory index      # 建记忆索引（空库也要建，否则巡检会提示）
./.commander/cmd task board        # 建任务看板
./.commander/cmd brief             # 给每个 agent 生成 BRIEF.md（巡检会查这个）
./.commander/cmd outbox collect --all   # 收掉第 13 步自测产生的 outbox
```

- 第 13 步的 smoke 派发会留下一条待收结果和一条任务记录，**必须收掉** ——
  否则用户第一次打开就看到"有待处理"，会以为是装坏了
- 验证：`./.commander/cmd patrol --oneline` 输出应以 `✅` 开头
  （或只有与"未填密钥"相关的提示）

**15. 检查清单自检**

逐项对照下面「检查清单」一节，把每项的实际结果列出来。

### 报告格式

```
## 安装结果

状态：成功 / 部分成功 / 失败

| # | 步骤 | 结果 | 证据 |
|---|---|---|---|
| 1 | 建目录骨架 | ✓ | 12 个目录 |
| 8 | 装引擎依赖 | ✓ | .venv 16 个包 |
| 13 | 端到端验证 | ✓ | outbox/bootstrap-check.json |

## 需要用户做的事

- [ ] 填密钥：编辑 {工作区}/bin/.env，把 DEEPSEEK_API_KEY 填上
- [ ] （可选）配远端：./.commander/cmd remote bootstrap

## 未做的事 / 不确定的

- 没装 claude/openai 后端（等你确认）
- ...
```

---

## 检查清单

安装完成后，**每一项都要有实际验证过的证据**，不能靠"应该没问题"。

| # | 检查项 | 怎么验 | 通过标准 |
|---|---|---|---|
| 1 | 工作区标记 | 读 `{工作区}/.commander-root` | 文件存在且非空 |
| 2 | 引擎源码 | 读 `{工作区}/bin/pyproject.toml` | 存在，且 `[project] name = "commander"` |
| 3 | 启动器 | `ls -l {工作区}/cmd` | 存在且有执行位 |
| 4 | 三份配置 | 读 `{工作区}/config/` | `models.toml` `agents.toml` `policy.toml` 都在 |
| 5 | 技能库 | 读 `{工作区}/skills/` | ≥8 个目录，每个含 `SKILL.md` |
| 6 | 引擎 venv | `ls {工作区}/bin/.venv/bin/commander` | 存在 |
| 7 | 五个本地后端 | 逐个 `ls -d {工作区}/bin/backends/<b>/.venv` | mock / openai_compat / claude / openai / **browser_use** 都存在 |
| 7a | 远端清单 | `test -f {工作区}/remote/hosts.toml` | 存在且是合法 TOML（空 `[hosts]` 也算通过） |
| 7b | 浏览器可用 | `./.commander/cmd browser status` | 显示 `✓ 本机: UP Chrome/...`（跑不起来则须在报告里说明） |
| 8 | 密钥文件权限 | `stat -c '%a' {工作区}/bin/.env` | `600` |
| 8b | 凭据可用（若用户提供了） | `./.commander/cmd config check` | 每个 provider 都 ✓ |
| 9 | 密钥没被带进来 | 逐个读 `*_KEY` 与 `*_PASSWORD` 的值 | 全为空。**注意别用 `grep '^[A-Z_]+=.+'` 这类宽模式** —— 它会匹配到 `COMMANDER_LOG_LEVEL=info` 而误报 |
| 10 | subagent | `ls {项目根}/.claude/agents/` | 能看到 `memory-scout.md` 等 |
| 11 | 技能可被加载 | `ls {项目根}/.claude/skills/commander/SKILL.md` | 存在 |
| 12 | 端到端 | `./.commander/cmd dispatch smoke -p '自检' -t bootstrap-check` | 输出含「成功」+ outbox 文件存在 |
| 13 | 目录契约生效 | 读 `{工作区}/logs/guard-violations.jsonl` | 不存在，或存在但里面没有 `rule: memory_not_writable` 之外的意外项 |
| 14 | 巡检干净 | `./.commander/cmd patrol --oneline` | 以 `✅` 开头，或只剩"未填密钥"相关提示。**不应有"待收结果"/"索引落后"** —— 那说明第 14 步没做 |

---

## 失败处理

| 失败点 | 常见原因 | 怎么办 |
|---|---|---|
| 找不到 `uv` | 机器没装 | 按第 8 步的三条路依次试**自己装上**；三条都不通就停下来问用户 |
| `uv sync` 失败 | 网络不通 / PyPI 被墙 | 报出原始错误，让用户决定 |
| `uv sync` 卡很久 | 装到了重依赖后端 | 检查是不是误装了 crewai（不该装） |
| 端到端验证失败 | 引擎起来了但链路有问题 | 读 `{工作区}/agents/smoke/logs/*.md` 的尾部，那里有 stderr |
| 权限不够写 `.claude/` | 沙箱或用户设置 | 跳过该步，在报告里列为"需用户手工完成" |
| 目标已有 `.commander/` | 装过了 | **不要覆盖**。检查它是否可用（跑第 13 步），可用就报告"已安装" |

**通用原则**：装不上就如实报告，不要伪造"成功"。用户第一次用这个技能，
你的报告就是他唯一的判断依据。
