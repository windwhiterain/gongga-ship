---
name: self-control
description: 控制"自己"的运行——本回合、自己起的进程、自己的上下文、自己的会话。当你要停/续自己的回合、要起一个活得比自己久的作业、要跨重启或压缩保留意图、要问另一个会话（mentor）一个问题并等它答、要开一个复制我自己配置的新会话、或者发现自己的输出/进程/状态不对劲时，读这个。只讲怎么用这个 harness：开第二个会话并行干活是 subagent skill，改它的源码、把它换成新的自己，是 evolve skill 的事。
compatibility: 需要 Python 3 和 home 里的 lib/gongga.py（服务端自己写的客户端，§0b）；入口是本包的 ask / new_session，不需要 PATH 上有 gongga-cli
---

# 自我控制

harness 给你四类杠杆：**回合**（turn）、**进程**（谁能收尸）、**上下文**（什么会活下来）、
**身份**（跑着你的那个 exe）。每个都有确切的作用域，其中三个是单向门。

这份文件只讲**怎么用**这个 harness。**改它的源码、把它换成新的自己**，是 `evolve` skill 的事，
不在这里混着写。

先记住贯穿全文的一个问题：**谁回来收这活？** 有主的东西才谈得上"停"；无主的东西只有
`del_bash` 停得住，而本回合被换掉时没有任何人回来。

---

## 0. 一个 shell，一条命令层

系统提示只留了身份和一句话：能跑命令、别问、别留垃圾、程序去 skill 里找。下面这些是它
原来顺带说的事，现在住在这里。

**shell 是持久的，且跨调用**：`bash` 每次调用都在同一个 shell 里跑，工作目录、环境变量、
shell 函数、导出的东西都留着，第一次调用就已经在会话的工作目录（也就是项目所在处）。
所以要 `pwd` 一次、之后写相对路径；要改环境就 `export`，下一个调用还在。
两个例外会把它换掉：回合被取消、命令超时——那时同名 shell 是新的一个，cwd 回到会话目录
（§1、§3 有细节），别假设 export 还在。

**`gongga-cli` 是这台服务端的命令层**，就在 `PATH` 首位，给人敲的。一条命令打印
**每行一个 JSON 事件**，最后一行是 `cmd_result`——所以 `| jq`、`| grep` 只拿你要的那部分，
不必整段读。**脚本不要起这个进程**：同一个模块 import 进来就是一个 `Reply`（§0b）。
`$GONGGA_SESSION` 是你自己的会话名，
`$GONGGA_SERVER`、`$GONGGA_HOME` 都在环境里；`gongga-cli` 裸跑列出这个服务端发布了什么。

**别把 `/xxx` 当命令**：交互输入行里的 `/` 是另一层语法，脚本和 bash 里只写裸名字
（§9 有代价记录）。

---

## 0b. 脚本要的是 Python 面，不是命令行

**服务端把客户端写在 home 里，脚本 import 它。** `$GONGGA_HOME/lib/gongga.py`
（`docs/notes/server-pylib.md`）：一个命令一个方法，每条答复一个 `Reply`，被拒时一个异常。
本 skill 的 `_client.py` 找到这个目录——skill 运行器给 `GONGGA_SKILL_LIB`，服务端起的
shell 给 `GONGGA_HOME`——然后 `import gongga`。

```python
import gongga
server = gongga.connect()          # 从 GONGGA_SERVER / GONGGA_KEY，或 home 的 server.key
server.sessions                    # greeting 里每个会话的那一行
server.models                      # roster 里现在真能用的 provider/model
server.catalog                     # 这个服务端发布了哪些命令、各收什么参数
reply = server.call("get_messages", session="x", range="-1:-1")
server.get_messages(session="x", range="-1:-1")     # 同一个东西：名字就是命令名
```

| 拿到的 | 是什么 |
|---|---|
| `reply.ok` / `.message` / `.data` / `.events` | 一条命令的答复：成了没有、服务端的话、它带的数、这段流上它看到的事件 |
| `reply.raise_for_status()` | 拒绝变成 `gongga.GonggaError`，句子里是服务端原话 |
| `server.<命令>(**参数)` | 名字来自 greeting；服务端没发布的名字在这里是 `AttributeError`，根本不发出去 |
| `gongga.Unreachable` | 一个字都没答（端口没人、key 不对）；这跟"被拒绝"是两件事 |
| `server.pump(seconds)` / `server.close()` | 读掉这段时间流上还到的 / 收摊（`with gongga.connect() as s:` 也行） |

**规矩：skill 的脚本里不出现 `subprocess` + `gongga-cli`。** 命令行是"人敲一行、看一行"
的形状，脚本要的是返回值；而 `Reply` 已经把成功/拒绝、结构化数据、事件分了类。把打印出来的
JSON 行再解析一遍，是把同一份契约抄第二遍，抄的那份一定会落后——`gongga-cli` 自己就是这份
模块的外壳（`python/gongga_cli.py` 第一件事也是 `import gongga`）。

**cli 不够方便的地方，用 Python 包一层再暴露 Python API，别拐回 cmd。** 本 skill 的两个
入口都是这么来的，而且都能 import：

| 需要 | cli 里没有 | 包一层之后 |
|---|---|---|
| 问一句并等它答完 | 没有"送到 + 等这一轮结束 + 取它的原话"这个动词 | `ask_session.ask(session, question)`：用 `get_messages --replay true` 读回它的行（§4b） |
| 复制我自己的调参 | greeting 的会话行不带 `cache_tier` / `cache_keepalive` / `mode` | `new_session.new_session(...)`：只读打开 `$GONGGA_HOME/gongga.db` 取来源那一行，再一次 `set_session`（§4c） |

```python
import sys
sys.path.append(r"C:\...\skills\self-control")   # 本 skill 的目录：`@skill(...)` 展开里那个 path 的父目录
from ask_session import ask
from new_session import new_session
answer = ask("mentor", "我当时是怎么写的？")
```

脚本形态（`"$SKILL_DIR/ask_session.py" <会话> "<问题>"`）和
`gongga-cli skill self-control ask …` 只是同一批函数的外壳。

---

## 1. 进程：三种 ownership

同一个 `bash` 工具，`background` 改的不是"等不等"，是**所有权**。

| 模式 | 谁拥有 | 输出去哪 | 谁结束它 | 服务端停时 |
|---|---|---|---|---|
| 默认（前台） | 本回合 | 工具结果，流式 | 命令结束 / 回合取消（连进程树一起杀） | 被中断，shell 换新 |
| `background`（remote） | 那个 shell | shell 的未读缓冲，`check_bash` 取走 | `del_bash` 随 shell 一起消失 | 随服务端死：它是 shell 的 job，不是独立进程 |
| `background`（本机） | **没人** | 只有它自己的日志文件 | **只有 `del_bash`** | **`cancel` 放生并被下次启动 adopt；signal / 关窗 / kill 都杀死** |

实测（本会话）：

- `background` 起了个每 1s 追加一行的循环 → `del_bash` 之后行数还在涨（13→15）；
  `list_bash` 把它单列在 `background[]` 里，带 `pid` / `log` / `running`。
- `del_bash --name_regex '^demo-detached$'` → 行数 15→15 停住，`background[]` 变空。
- `del_bash` 的回执会明说：「1 background job(s) from them are still running and were not
  stopped」——它不会假装帮你停。

推论：

- `background` 的生死绑在 **shell** 上，不在回合上：回合结束后它还在跑；`del_bash`、
  服务端退出、或"取消时正卡在这条 shell 的前台命令里"（那条命令连同整棵进程树被杀）
  才会带走它。
- 想"后台跑、但我一收工就死" → `background`，然后别再 `del_bash` 那个 shell。
- 想"活得比我久"（守护进程、替身服务端、长跑基准）→ `background`，然后**记下 job 号或 pid**，
  否则下一任的你会看到一个孤儿。
- 想"卡住就回来" → 前台 + `timeout_ms`（见 §3），不要自己 `&` 加轮询。

`background` 的内部约束（读码）：独立进程组、`kill_on_drop(false)`、pid 写进 `detached_jobs` 表；
新服务端启动时 `adopt()`，但**绝不**接管 pid 等于自己或自己祖先的行——否则 `del_bash`
就成了服务端自杀按钮。

---

## 2. 回合：三种"停"，区别全在"谁记得这活"

| 动作 | 语义 | transcript | `pending_tasks` 行 | background 作业 | 谁接着干 |
|---|---|---|---|---|---|
| `interrupt_session <n>` | 别干了，丢掉 | 停在断点 | **删** | 不动 | 没人 |
| `cancel`（服务端命令） | 停，剩下的下次接着干 | 停在断点 | **留** | 不动 | 下一个启动的服务端 |
| 从外面 `taskkill` / 杀进程 | 硬死 | 停在断点 | 留 | 不动 | 下一个启动的服务端 |

### 最关键的一条：resume 不给任何提示

`resume_pending` 起的 turn 是 `turn(session, user_text = None)` —— **不写任何 instruction**，
不追加"你被中断了，继续"。它只是拿 transcript 再请求一次模型。

所以：**cancel 之前，把意图写进 transcript 或写进文件。** 否则接着干的那个你，手上只有
半截工具结果，它不知道你想干什么。可用的两种写法：

1. 发一条消息给自己（见 §5），它会在下一个 step 边界落进 transcript；
2. 写文件（`.agents/notes/`、或待办文件），并在 cancel 前用一句话把它读进 transcript。

### pending 行就是"欠下的活"

`pending_tasks` 里 `kind=turn` 表示"消息已在 transcript，只欠一个 turn"；`kind=inject`
表示"消息还没落库，谁消费谁负责写"。表里有行 = 下次启动欠着。判断自己会不会被重放：

```bash
python - <<'EOF'
import sqlite3, os
c = sqlite3.connect(f"file:{os.path.expanduser('~/.gongga/gongga.db')}?mode=ro", uri=True)
for r in c.execute("select id, session, kind, created_at from pending_tasks"): print(r)
EOF
```

### `cancel` 不会立刻杀你

`cancel` 先 `cancel_and_preserve()`，等 250ms 让回执发出去，再触发 shutdown token；监听循环
随后等 `active` 归零，**没有上限**（每 10s 打一条 warn，是观测不是砍断）。你在 select 点
（模型流、工具调用、重试退避、压缩）看到 token 才退出，所以：

- 你正卡在前台 `bash` 里 → 那条命令连同进程树被杀，你带着「已取消、退出码未知」的结果退出；
- 你正在写文件 → 那一步会跑完，然后退出。

想"立刻死、不要了"用 `interrupt_session`；想"停在这里，下次从完整 transcript 原样恢复"用 `cancel`。

---

## 3. 时间：等待可以被两件事打断，命令都不会被杀

前台 `bash` 的等待有两个触发源，**两个都只结束等待，不结束命令**：

| 触发 | 回执开头 | 谁等的 |
|---|---|---|
| `timeout_ms` 到（默认 120s） | `still running after <n>ms, so the wait ended` | 你自己设的期限 |
| **有人给本会话发消息** | `a message arrived for this session, so the wait ended and it was handed to the background` | 不是你能预知的 |

第二种是 2026-10-24 加的，也是更常见的那个。实测：先让一个 background 作业 5 秒后
`send_message` 给我，同时前台跑 `sleep 40` —— 调用 **3 秒就返回了**，40 秒的命令自己跑满，
输出（`done after 40s`）之后作为 inbox 消息投回来。

所以：**你不需要为了"能被打断"去开短 timeout**。它就是会断。反过来说，
长命令被交给后台是**正常现象**，不是故障——不要因为它提前返回就以为命令失败了。

两种情况下，**同名 shell 都换成了新 spawn 的一个，cwd 回到会话目录**。所以之后不要假设
`cd` 还在；长任务的前缀请写成绝对路径或一句 `cd` 重来。

由此：

- 等待不是预算，也不是 kill。要真停就在前台等它被杀（取消），或 `del_bash`（background）。
- 默认 `timeout_ms` 是 120s；`background` 忽略它。
- **没有步数上限**：一个 turn 跑到活干完。所以"我会不会失控"这件事上，harness 不给你预算，
  自我控制只能来自你：小步验证、及时落盘、别开无法收尾的长链。

---

## 4. 上下文：什么活下来，什么蒸发

| 事件 | transcript | 摘要 | 你的"当前意图" |
|---|---|---|---|
| 培养继承人 | 服务端**从不改写**行；只有 agent 自己会 | `heir` 把整份 transcript 存进 mentor 会话后**删空当前会话**（删前逐行校验副本），只留继承人的第一课 | 由第一课明确保留，继承人继续当前工作 |
| 回合结束 | 保留 | — | 丢（除非写下来了） |
| `cancel` + 重启 | 保留 | 保留 | **丢**（resume 无提示，见 §2） |
| `del_session` | 全删 | 删 | 全没 |

压缩阈值默认是上下文窗口的 **70%**（本会话模型 `deepseek-flash` 窗口 1,000,000），
`set_session --ctx_limit 30%` 可调，`0` 关闭；设百分比要求模型在 roster 里有
`context`，否则命令当场报错而不是悄悄忽略。

### 记忆写在文件里，不写在上下文里

- 服务端只读**一个** `AGENTS.md`：会话 cwd 里的那个（2026-11-02 起）。它作为 index 的
  第一个 root 进 `instructions`，随前缀缓存，所以写在那里的话每轮都算一次、改一次才重算；
  父目录的、`~/.agents/AGENTS.md` 都不读。机器自己的常驻规矩仍是
  `~/.agents/skills/whiting-dev`。别的约定要么放进 `instructions`，要么写成 skill 让
  `skill`（不带 `name`）列出来——否则新会话根本不会看见它。
- WIP 按仓库约定写 `.agents/notes/<date>-<slug>.md`。
- 长输出溢出的全文在 `~/.gongga/outputs/`，文件名是 `<session>-<shell>-<description>-<callid>`
  ——所以 `bash` 的 `description` 参数是给"未来的自己"和读 dump 的人的索引，不是装饰：
  它还会印在那一行工具调用上（`bash·check the state`），所以写的是这次要干嘛，不是命令本身。
  默认超过 8000 字符就只回头尾，回执里有路径：用 `sed -n` / `grep` 读，**别 cat**。

---

## 4a. 你的"第一句话"要写成带引用的 prompt

**引用（meta reference）不是省字，它是前缀缓存规划唯一的入口。** 一个请求的最前面是
instructions + index；`index` = 会话**第一条** user 消息里点到的 `@file(...)` / `@skill(...)`
的正文，按 `prefix_cache` 算出的最省顺序拼起来。那个顺序不是随手排的：模拟出来的缓存树按
**会话目录**共享，同一目录里先计划过的会话留下一条链，后来的会话沿它走——两条请求的
首部逐字节相同，provider 才付得出缓存价。

"先 `cat` 一遍再问"和"把文件引用进第一句话"的差别不是风格：

| 写法 | 正文落在哪 | 之后怎么付费 |
|---|---|---|
| `bash: cat plan.md` | 一条 tool_result，在 transcript 中间 | 每轮跟着前缀重发，而且排在 index **之后**：首部一变，它和它后面整段重付 |
| 第一句话里写 `@file(plan.md)` | 进 index，紧跟 instructions | 一次进系统提示，之后每轮命中；之后再改那个文件也不再重发 |

所以"自我控制"里每个**第一句话**的时刻，都要写成带引用的 prompt：

| 时刻 | 谁是第一句话 | 写什么 |
|---|---|---|
| spawn 一个子 agent | `send_message` 的 brief | 它要读的文件、它该用的技能全写成引用（见 `@skill(subagent)`） |
| 培养继承人 | `heir` 的第一课 | 下一步要动的文件、要守的规矩写成引用：继承人的下一次请求就带着正文 |
| `new_session` 之后 | 那个新会话的第一条消息 | 整轮工作都要看的东西，第一轮就引用掉 |
| `ask_session` 问一句 | **不算**：它是轮中注入的一条消息，index 早已写定 | 那里的 `@skill(...)` 只会换成指针（路径），正文得对方自己去读 |

两条边界，写错都**不报错**：

- 只有**第一条** user 消息能进 index。第三轮才补一条 `@skill(foo)`，foo 的正文没有进来。
- 引用是 `@kind(值)`，括号必须配对。漏了右括号、写错大小写，消息照发、词照留，
  index 里没有它，也没有一行日志。

拼写、递归和代价记录都在 `@skill(prefix-cache)`。

---

## 4b. 问另一个会话：`ask_session`

**别的会话——尤其是 mentor——手里有你已经忘掉的东西。** 自己再摸一遍是把整场搜索再付一次；
问一句是一个回合。

它是本 skill 的一个函数，也是一个脚本（§0b 的规矩：走 in-process 客户端，不起进程）：

```python
from ask_session import ask                 # 目录进 sys.path 之后（§0b）
answer = ask("mentor", "我当时是怎么写的？")   # 答复就是它的返回值
```

```bash
SKILL_DIR=<本 SKILL.md 所在目录>          # `@skill(...)` 展开里括号中的 `<path>` 的父目录
"$SKILL_DIR/ask_session.py" <会话名> "<问题>"
```

gongga 里更短的一层是 `gongga-cli skill self-control ask <会话名> "<问题>"`：`self-control`
是个 Python 包，第一个词是函数名（`ask` 是本包的函数），参数原样传过去，退出码就是入口的。

它做两件事，一次调用做完：**把问题递过去**，然后**等答复**。答复是对方会话自己 transcript 里
的话，按窗口读法读回来（`get_messages --replay true`），所以操作者的消息和它的输出是分开的行，
不用去猜哪一段是谁说的。等的是"问题那一行之后、它这一轮结束"，不是"最后一行输出"。

**会话名是位置参数，问题在它后面。** 这不是随意的：`send_message` 的位置参数是
`--message` 在前、`--name` 在后，所以 `send_message <会话名> "…"` 会被读成「问题=会话名」，
问题发给了空名字。`ask_session` 把会话名放在前面，正是为了躲开这个坑。

**问的时候把问题说具体**：要什么答案、为什么现在要。"当时能不能这么写"只能换来一句"能"，
把你卡住的那一步写进去，才有东西可答。

答复会到你的会话里（`send_message` 给对方，对方 `send_message` 回来），所以**问完可以继续
干别的**，消息到了再读。函数自己等的那份是同一条答复，只是它先替你挡住了这次等待。

退出码：0 拿到了答复；2 问题没送到，或等超时（默认 900 秒，`--timeout` 可改）。

---

## 4c. 开一个新会话：复制我自己的配置 `new_session`

**新会话是空的，配置是我的。** `new_session` 拿我自己的会话行（`$GONGGA_SESSION`，或
`--source <会话名>` 指定的另一行）当来源，逐项复制配置，开一个全新会话。答复是新会话名，
打印在 stdout。

```bash
gongga-cli skill self-control new_session <新名字> [--source <来源会话>] \
    [--model <model>] [--thinking <level>] [--ctx_limit <n%>] [--cache_tier <name>] \
    [--cache_keepalive <bool>] [--mode <work|explore>] [--remote <name>|none] [--cwd <dir>] \
    [--dry_run true]
```

参数名就是字段名，`--name value` 或按位置传。`--dry_run true` 只把将要发出去的参数渲染成一行
`set_session` 打印出来，什么都不建——真正建会话那一步是一次 `server.call("set_session", ...)`
（§0b），不经过命令行：同一张参数表既决定发什么，也决定 dry run 显示什么，所以两者不会走散。先看清再动手。

**三条不变量**（写在这里，不靠脚本注释代言）：

1. **机器与目录成对：目录只在目标机器 == 来源机器时复制。** 来源行的 `remote` 是它运行
   的机器；不给 `--remote`，新会话落在来源那台机器上，目录随配置一起复制。`--remote` 指了
   另一台机器（或来源在远端、而 `--remote none` 回本机）时，新会话落在那台机器上，**不复制
   目录**——一个目录只在它存在的那台机器上有意义。显式 `--cwd` 总是生效，且它在目标机器上
   被解释：给了一个远端机器上不存在的目录，错在创建这一刻，不在第一个 turn。
2. **新会话是空的。** 只有配置被复制：transcript 从空开始，不带消息、不带订阅、不带
   pending。想要孩子会话（路径挂在调用者名下、订阅它的 turn-end、重名自动加段）用
   `subagent` skill——那是另一种关系，见下。
3. **名字不复制。** 配置之外什么都不带走：新会话应答你给的名字，`--must_create true`
   保证名字被占了就拒绝，绝不加入别人的 transcript。

逐项字段表（默认 = 不给对应参数时发生什么；NULL = 来源行没设过，按服务端的默认走，
复制的办法就是不发这个参数）：

| 字段 | 默认 | 覆盖 | 绝不复制 |
|---|---|---|---|
| 名字 | —（必填的新名字） | 位置参数 | 来源会话名 |
| model | 复制来源的 | `--model` | — |
| thinking | 来源有则复制；NULL 不发 | `--thinking`（`default` 清除已设的） | — |
| ctx_limit | 来源有则复制；NULL 不发 | `--ctx_limit` | — |
| cache_tier | 来源有则复制；NULL 不发 | `--cache_tier` | — |
| cache_keepalive | 复制（来源必有值，true/false） | `--cache_keepalive` | — |
| mode | 来源有则复制；NULL 不发 | `--mode` | — |
| remote | 复制来源的机器（本机则不发参数） | `--remote` / `--remote none` | — |
| cwd | 目标机器 == 来源机器时复制 | `--cwd`（在目标机器上解释） | 机器不同时绝不复制 |
| transcript | — | — | 绝不复制（新会话是空的） |

失败路径（退出码都是 2，服务端的话原样透传，库里没有新行）：

- 新名字已存在：`--must_create` 拒绝加入已有会话。
- `--remote` 的名字不在服务端的名单里：服务端拒绝，发生在创建之前。
- 没有来源（`$GONGGA_SESSION` 未设且没给 `--source`），或 `--source` 的名字不是会话：
  拒绝，不退化成"建一个没配置的会话"。

与 `subagent spawn` 的边界：`spawn` 开的是**孩子**——路径、订阅、命名都绑在调用者的
子树上；`new_session` 开的是**平级**——只复制配置，不订阅、不带上下文、不建路径关系。
要并行干活、要被告知完工，用 `subagent`；要一个"配置和我一样、但从零开始"的会话，
用 `new_session`。

退出码：0 建成（打印新会话名），或 `--dry_run` 打印计划行；2 上述任一拒绝。

---

## 4d. 模型名是一条 path：层级管理就写在名字里

会话行里的 `model` 存的是**整条** `provider/model_name`，不是裸名字——一个名字不说明是谁在
服务它（`SessionRow::model` 的原话）。而模型名自己还能再带 `/`：服务端只在**第一根** `/`
处切开，剩下的整段就是模型 id。于是模型名天然是一条 path，段就是层级：

| 段 | 这一层写的是什么 |
|---|---|
| 第一段 `provider` | 密钥（`key_env`）、端点与 `base_url`、额度查询、缓存 ttl 与它接受的档位、`prompt_cache_key` 一类请求头 |
| 后面几段 `model_name` | 模型自己：窗口大小、模态、`tier`（flash / pro / sota）、价格、思考档位、内置工具 |

**规划按层做，写的时候也按层写：**

- 要"这一家整体更便宜 / 换端点 / 换 key"→ 那是 provider 段的事，这一家下所有模型一起跟着走。
- 要"这一个要更难、要更长的窗口"→ 换叶子段。
- **裸名字把 provider 段藏起来**：`--model` 先按整条 path 匹配，匹配不上才在 roster 里找
  **第一个**带这个名字的 provider——落在谁家由文件顺序决定，而且那家没有 key 时命令直接失败，
  不会替你换一家。所以凡是要**写下一个会话跑什么**的地方（`set_session --model`、
  `new_session --model`、spawn 的 brief），写整条 path。
- **`--cache_tier` 校验的是 provider 声明的档位**（报错句说 "not a tier this model declares"，
  roster 却写在 `[[provider]]` 上）：provider 不动，换模型不会让档位失效；换 provider 就可能。
  `new_session` 会照抄来源的 `cache_tier`，所以跨 provider 的 `--model` 最先被拒的地方就是这里。

会话名是同一套规矩：`<调用者>/<角色>` 是一条 path，名字按前缀渲染成可折叠的层级，
子 agent 全挂在调用者下面（见 `@skill(subagent)`）。

---

## 5. 用 `send_message` 给自己留话

会话正忙时 `send_message` 不排队第二个 turn，而是写一条 `kind=inject` 的行，由当前 turn
在**下一个 step 边界**读进 transcript。这就是"给自己留一句不会丢的话"。

你自己（agent）在 bash 里直接敲 `gongga-cli` —— 服务端把这条命令装进 `<home>/bin`，并把它
放进你 shell 的 `PATH` 最前面：

```
gongga-cli send_message --name test1 "SELF-NOTE: ..."
```

回执「message queued for test1; it will be read at the next step」，下一个 step 就以
一条 user 消息出现。另起一个 shell 或 launcher 也一样写 `gongga-cli`：`PATH` 首位就是它，
那是同一个程序，不需要路径也不需要第二个变量。**Python 脚本不这么写**：`server.call("send_message", …)`
是同一个动作，而且知道有没有送到（§0b）。

**它现在还能叫醒你**（2026-10-24 起）：前台 `bash` 正等一条长命令时，消息一到，等待就结束、
命令交给后台继续跑（见 §3）。所以这不是"留言"，是一条**打断**——`sleep 40` 实测 3 秒返回。
两面的用法：

- 想让长命令早点放手 → 从别处发一条消息，不用干等。
- 小心：你自己发的消息同样会打断自己正在等的东西，别在关键写入中途插自己。
  而"叫醒自己正在等的那条前台命令"现在是**做得到**的 —— 把要等的东西放进后台，再发那条消息：
  `sleep 8 & gongga send_message $GONGGA_SESSION "醒一下"`。
  实测：这条 `bash` 0.4 秒返回，日志写着「handed a command to the background: a message is
  waiting」，八秒后它的输出作为完成通知自己回来。工具时代做不到 —— 一个 turn 一次只能调一个工具。

规矩：

- 它是**用户消息**，会一直留在 transcript 里，还会被折进笔记。不要当 printf 用。
- 只能用在这一处合法位置：`function_call` 和它的 `function_call_output` 之间插 user 消息
  会被 provider 整单拒绝，所以注入点在 step 边界。
- 消息自己也是 pending 行，`cancel` 前后都能活下来 —— 这是跨重启留意图最省事的办法。

### 子 agent：一块独立的工作交给另一个会话

多开一个会话就有第二个上下文，可以并行做另一件事。怎么开、brief 要写什么、什么时候不能
并发写同一份树，都在 **`subagent` skill**。

一条和"自己"直接相关的：`send_message` 发出去的 brief 就是那个会话的**第一条** user 消息，
所以 brief 里写的 `@file(...)`/`@skill(...)` 会进它的 index，随前缀缓存只收一次费——这也是
"派活时把要读的东西一次说清"比"派完再补一条"便宜的原因（见 `@skill(prefix-cache)`）。



## 6. 改源码不是用 harness

换掉跑着自己的那个 exe、改它的源码、用它自己的开发环，都是另一件事，在 **`evolve` skill**
（`@skill(evolve)`）。这里的每一条都是**使用**这个 harness 的规矩；
要动它的源码，先去读那个 skill。

## 7. 单向门：做之前先看范围

| 动作 | 不可逆的部分 |
|---|---|
| `del_session <n>` | 删 transcript、删 pending，**并停掉该会话的 background 作业**（`cancel` 不这么干） |
| 从外面杀服务端 | 本回合没人接，等于丢了一次未落盘的判断 |
| 没有继任者就 `cancel` | home 空着，直到有人手动起服务端才 `resume_pending` |
| `del_bash` | 进程没了；日志文件还在，但状态没了 |
| `del_bash` | 那个 shell 的 cwd / export / 函数全清零（后台作业一起死） |

---

## 8. 看自己

| 想知道 | 怎么看 |
|---|---|
| 我有几个 shell、谁还有没读的输出、有哪些 background | `list_bash`（`unread_bytes`、`background_jobs`、`detached[]`） |
| 后台作业跑到哪了 | `check_bash --name <shell>`，**读即清空**，同一段不会给两次 |
| 我刚才到底做了什么、上一条命令的原文 | `get_messages --session <s> <a>:<b>`（1 基、两端含，`-1` 最新一条，如 `-5:` 取最后五条；省略范围是全部） |
| 会话状态 / 模型 / 压缩阈值 / 消息数 | 只读查库（下面） |
| 我哪些工具调用在失败 | `tool_calls` 表按 `name,status` 汇总 |

只读查库（服务端在跑时也安全，`mode=ro`）：

```bash
python - <<'EOF'
import sqlite3, os
c = sqlite3.connect(f"file:{os.path.expanduser('~/.gongga/gongga.db')}?mode=ro", uri=True)
print(c.execute("select name,status,model,context_limit,context_tokens from sessions").fetchall())
print(c.execute("select name,status,count(*) from tool_calls group by name,status").fetchall())
print(c.execute("select ID,session,kind from pending_tasks").fetchall())
EOF
```

注意服务端日志是 **stdout/stderr**，不落文件——想要日志就在起服务端时重定向。

---

## 9. 别做这些（都有代价记录）

- **一个命令一个 shell 名。** 一个名字 = 一个常驻进程；每次新名字会留下一个空转进程。
  默认就用 `default`，并在 `description` 里写清这次在干嘛。
- **把 `timeout_ms` 当 kill。** 它不杀；命令还在那个 shell 里跑，输出稍后自己回来。
- **超时后假设 cwd 没变。** 同名 shell 已经换成新的、回到会话目录。
- **`cat` 溢出文件。** 它们可能很大；用 `sed -n` / `grep`。
- **第二次 `check_bash` 期待同样输出。** 读即清空；第二次只会拿到新的部分。
- **在 bash 里 `gongga-cli /cancel`。** `/` 只属于交互式输入行；这里 `/cancel` 会被
  Git Bash 当路径改写成 `C:/.../Git/cancel`。argv 已经是 token，起这个进程的人已经写下了
  命令名，没有第二层语法要再解一遍：写裸名字 `gongga-cli cancel`（Python 脚本里是 `server.call("cancel")`，§0b）。
  带 `/` 或带路径分隔符的第一个 token 会被命令层拿最后一段去目录里查，命中就告诉你正确写法。
- **`bash` 传 `cmd`。** 参数名是 `command`。多余键被忽略，缺 required 会被拒并提示
  「did you mean `command`?」。
- **在 bash 里 `kill <pid>` 停 background。** MSYS pid 不等于 Windows pid，`taskkill` 认不出来；
  用 `del_bash`。
- **用 `send_message` 记日志。** 它会永久留在 transcript 里并进入笔记，而且**会打断
  你正在等的命令**（见 §5）。

- **在 skill 脚本里 `subprocess` 跑 `gongga-cli` 再解析它的输出。** 命令行是给人看的形状，
  脚本要的是 `Reply`；再解析一遍等于把契约抄第二份，抄的那份会落后（§0b）。
- **以为写了 skill 文件就等于能被发现。** 发现靠提示词里那句索引；索引不在（旧服务端、
  cwd 不在仓库里），文件就是死的。先跑一遍索引命令再指望别人读到。

---

## 10. 决策清单

动手前问四句：

1. **这活谁收？** 我死/被停之后，谁还需要它？→ 决定要不要 `background`。
2. **我要停的是哪一种？** 丢掉（`interrupt_session`）、留着下次干（`cancel`）、还是永不回来
   （外部杀进程）？
3. **接着干的那个我，凭什么知道该干什么？** 意图进 transcript 或进文件了吗？
4. **这动作可逆吗？** 看 §7 的表；不可逆的话，范围和回退路径写在旁边。

---

## 11. 本技能的验证状态

| 结论 | 怎么来的 |
|---|---|
| 三种 bash 模式的存活语义（含 `del_bash` 后 background 继续、`del_bash` 停住） | 本会话实测 |
| 超时只结束等待、输出随后回投、shell 被替换 | 本会话实测 |
| **消息打断等待**、命令不被杀、输出随后回投 | 本会话实测（`sleep 40` / 5 秒后发消息 → 3 秒返回，命令跑满 40s 并回投） |
| 忙时 `send_message` 在下一个 step 边界投递 | 本会话实测 |
| 两个会话的 turn 真的并行（互斥锁每会话一把） | 2026-10-30 实测：`main` 与 `sub1` 相隔 8ms 起 turn、两次请求并行、两次 turn end 同时回来 |
| `cancel` 保留 pending、`interrupt_session` 删 pending、`del_session` 连带停 background | 读 `crates/server/src/commands.rs` |
| resume 不带任何 instruction | 读 `agent.rs::turn_inner`（`user_text: None` 不写行） |
| 阈值 70% / transcript 不改写 / 只有 `heir` 培养继承人时移动 | 读 `agent.rs`、`store.rs` |
| `evolve.sh swap` 的完整换手 | **实跑 9 次**，其中 02:24 那次**丢了回合**（pending 行被 `resume_pending` 的收尾无条件删除），修好后 3 次全部 resume 成功 |
| 只读 sqlite 自查 | 本会话实测 |
| 包只发布 `ask` / `new_session`；`--dry_run` 只打印不建会话；重名、未知 remote、无来源都退出 2 且库里无新行；新行逐字段等于来源行 | 2026-11-06 实跑：临时 home + 真服务端 + fake_llm 探针，外加 `test_new_session.py` 表驱动单测 |
| `ask` / `new_session` 走 in-process 客户端（`import` home 的 `lib/gongga.py`，不起 `gongga-cli`），包形态与脚本形态都答出对方原话 | 2026-11-07 实跑 `scripts/selfcontrol_selftest.sh`：临时 home + 真服务端 + fake_llm，PASS |

---

## 12. 技能本身是怎么被发现的

发现机制是 **`skill` 工具**（不带 `name`）：一次调用返回一张表，每行是
`name` / `description` / `path` / `bucket`。先只看这张表，命中才读正文——
这就是全部成本。别再 `grep` 目录：那等于让每个会话重新实现一遍这个工具。

系统提示（`instructions`）是 ship 仓库里的一段**静态文本**：不渲染、不拼任何东西、
没有占位符，所以每个会话收到的字节都一样——它开在请求最前面，一样才谈得上被 provider
的前缀缓存覆盖。它只留身份、一句授权（能跑命令、别问、别留垃圾）、一句"程序去 skill 里找"。
会话名靠 `$GONGGA_SESSION`，不靠提示词里的插值；其余的说明都在 skill 里，也就是这一份。

为什么这么切：任何随会话变化的东西都会让那条会话拥有自己的前缀，缓存一点也留不住；
而一份要读三段的常驻规矩，只有真需要它的人付得起。

三个桶，优先级就是顺序（同名以先出现的为准）：

- **项目桶** → `<cwd>/.agents/skills/<name>/`。会话正在干活的那个 checkout 自己的，
  最具体，所以排第一：一个仓库说"here, do it this way"就压过它所在的 home。
- **harness 桶** → `<home>/skills/<name>/`。跟着 home 走，随服务端实例存在。
  本文件、`heir`、`image`、`remote` 在这个桶。
- **通用桶** → `~/.agents/skills/<name>/`。跟着机器走，和 `providers.toml` 做邻居：机器自己的
  常驻规矩也放这儿当一个 skill——`instructions` 里没有任何东西是从磁盘自动读进来的。
  `computer_use`、`latency`、`model_pool`、`polity`、`proxy`、`web-search` 在这个桶。

**桶的路径不是调用方式。** 跑一个 skill 自己的脚本，一律从**它自己的目录**出发：
`@skill(...)` 展开里括号中的 `<path>` 的父目录就是那个目录（`skill` 的 `path` 列同源）。
home 会搬、桶会变，把桶的路径写进正文，别的 harness 读到的就是一个不存在的命令。
gongga 里更短的一层是 `gongga-cli skill <name> [args…]`，它按同样的三根优先级解析。
本技能就是这样一个包：入口是它发布的函数 `ask` / `new_session`——package 优先于
frontmatter 的 `metadata.entry`（即使写了也不读，所以本文件不写）；第一个词是函数名，
其余按位置或 `--name value` 绑定，返回值是答复（字符串打印、整数作退出码、其它作 JSON）。

`evolve` 不在任何桶里：它要从自己的路径往上找 `Cargo.toml` 并 stage 二进制，装进 home
就找不到仓库，所以它住在 gongga checkout 自己的 `.agents/skills/evolve/`。

home 的两个桶来自一个独立的 ship 仓库，服务端 pin 它的一个 commit：启动时把内容装进去，
用 git 三方合并把上游更新和你的本地修改合起来——你改过的不被覆盖，合不上就报错而不是硬合。

要点（这几条从旧机制继承，仍然成立）：

- **别把 skill 列表写进 Rust 字符串**。现在它连文件都不在这个仓库里：加一个 skill
  不用重新编译服务端，也不用换服务端。
- 正文要能独立成立：读它的人只有任务和这份文件，没有你的上下文。
- 附带脚本就写在 SKILL.md 旁边，让自己直接跑，而不是每次重敲命令。
- 路径写 `$GONGGA_HOME/...` 或 `~/...`，不要写相对仓库根的路径：会话的 cwd 不保证
  在那个仓库里，而 skill 现在本来就不在仓库里。

历史：懒发现是 2026-10-24 加的（那时 skill 文件存在但没人会看见，包括本文件第一版）；
2026-10-28 提示词进模板、`AGENTS.md` 开始注入；同日 skill 移出项目目录、
发现改成 `index_skill`；还是同日，注入被撤销——常驻规矩活了一天就当上了 skill。
2026-11-02 回来的只有 cwd 那一个，而且走 index 而不是拼进 `instructions`。
