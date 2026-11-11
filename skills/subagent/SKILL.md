---
name: subagent
description: 把一块独立的工作交给另一个会话并行做——子 agent 不是一个工具，而是一个普通会话：自己的 transcript、自己的上下文与阈值、自己的 turn。本 skill 的 `spawn` 一次做完两件必须做的事：在调用者名下起一个撞不上、也不会选中别人的名字（`<父会话>/<角色>`，`set_session --must_create`），配置照调用者那一行整份复制（走 `self-control` 的 `new_session`，只传名字和覆盖），并替调用者订阅它的 `turn_end`，所以父会话不必轮询。派活用 `send_message`，等它用一个可选的 `wait`（工具，不是一个睡眠的 shell）。当你要并行推进两条互不相干的实现、把大块验证/探查丢出去而不涨自己的上下文、或者要一个只读的侦察兵时读这个。
compatibility: 需要 Python 3、PATH 里的 gongga-cli，和同一个内容仓里的 self-control skill（spawn 复制调用者的配置走它的 new_session）
---

# 子 agent

**一个子 agent 就是一个普通会话**：跟你在同一台服务端上，有自己的 transcript、自己的
上下文与压缩阈值、自己的 turn。turn 的互斥锁是**每个会话一把**（`SessionHandle::gate`），
不是全局，所以两个会话的模型请求是真的同时在路上。

SPEC 把这个 skill 的事写成四步。动手的只有 `spawn`：它把前两步做成一条命令，因为第二步
一旦要人记得做，父会话就会退回去轮询。

| 步骤 | 谁做 | 为什么不是随手一条命令 |
| --- | --- | --- |
| 在调用者名下建一个会话 | `spawn <角色>` | 名字会撞，而 `set_session` 撞名**不报错** |
| 订阅它的 `turn_end` | `spawn` 顺手做 | 父会话要的是"它停了叫我"，不是"我每十秒问一次" |
| 派活：`send_message` | 你 | 命令行本来就够 |
| 等它：`wait`（可选） | 你 | 等是回合里的一件事，不是一个睡着的进程 |

## 起一个：`spawn` = 建会话 + 订阅

```bash
G=gongga-cli
model="$($G '{"name":"skill","args":{"name":"model_pool","args":["pick","--tier","flash"]}}')"
name="$($G '{"name":"skill","args":{"name":"subagent","args":["spawn","impl","--cwd","/path/to/its/checkout","--model","'"$model"'"]}}')"
echo "$name"          # main/impl，或者 main/impl/2
```

它是个 Python 包，公开 API 就是 `spawn(role, remote=None, cwd=None, model=None,
context_limit=None)`：命令行上第一个词是函数名，后面的词按参数名/顺序给它——所以
`--context_limit 20%` 带下划线，就是那个参数名。Python 里直接
`import subagent; name = subagent.spawn("impl", cwd="/path")`，同一个函数，不经命令行。

**它的配置就是你的配置。** `spawn` 不给参数时，子会话是**调用者那一行的整份拷贝**：模型、
思考档位、上下文预算、缓存档位与 keepalive、mode、机器、目录——一项项都不用重打，也不会
漏掉其中几项。这张复制表只存在一处（`@skill(self-control)` 的 `new_session`，§4c），
`spawn` 通过服务端的 `skill` 命令调它，只传名字、来源和你在下面点名的覆盖：

```
spawn impl                        -> <我>/impl，配置 == 我这一行
spawn impl --model <别的模型>      -> 同上，只换模型（要便宜/更难的活就点名）
spawn impl --context_limit 20%    -> 同上，只换预算
```

所以那个 skill 没装时 `spawn` 会**拒绝并说明**（不是悄悄少复制几项）：两个 skill 同属一个内容仓，
正常安装不会只有一半。

**它在哪儿干活，默认跟你一样。** 机器来自服务端给每个 shell 导出的 `GONGGA_REMOTE`，目录就是
这个 shell 所在目录（显式写成 `--cwd`，sticky 里的 `cwd` 就挪不动它）。所以远端会话里派活，
子会话默认也在那台远端、同一个目录，不用手写 `--remote`/`--cwd`；只换目录时机器照旧。

机器和目录也能改：`--remote <名字>` 换机器（`--remote none` 是服务端自己这台），`--cwd <路径>` 换目录。
**换了机器就不继承目录**——调用者的目录在那台机器上才有意义；只换目录时机器照旧。

gongga 里更短的一层是 `gongga-cli '{"name":"skill","args":{"name":"subagent","args":["spawn",…]}}'`：它按名字找到这个包、把第一个词当函数、其余交给它，
退出码就是入口的，所以 `name="$(gongga-cli '{"name":"skill","args":{"name":"subagent","args":["spawn","impl","--cwd",…]}}')"` 是同一件事。

stdout 只有一行名字（给 `$(…)` 用）；哪个名字被占了、订阅了几号，都走 stderr。

**名字是路径**，父会话是 `$GONGGA_SESSION`（服务端给每个会话的每个 shell 注入的那个）：

```
<父会话>/<角色>          例：main/impl、main/scan
<父会话>/<角色>/<n>      那个名字已被占：main/impl/2、main/impl/3
```

从 `main` 起的子 agent 叫 `main/impl`，它自己再起的叫 `main/impl/review`；跨父撞名不可能。
名字就是它在会话列表树里的位置。**没有 `--parent`**：能自己指定父会话的调用方，就能把工作
塞进别人的子树里、谎报出身。`--must_create true` 让服务端在同名已存在时**拒绝**这条命令，
所以"选名字"和"占住名字"是一步，不存在"探测完、创建前"被别人抢走的窗口。

自己用 `set_session` 建的会话也能当子 agent 用，只是没有订阅——那就自己补一条（见下一节）。

## 它每一轮结束，你会收到一条消息

`spawn` 替你发的就是这一条：

```bash
$G '{"name":"subscribe","args":{"pattern":"^turn_end:main/impl$","persistant":"true","cmd":{"name":"send_message","args":{"session":"main","message":"main/impl finished its turn"}}}}'
```

**订阅只做 SPEC 写的那一件事：匹配了就执行行里带的命令。** 服务端自己不替你说话，所以"通知"
是这条命令寄出来的——动作就是一个 `send_message`，发给你（订阅者）。它落到你的 transcript 里
就是一条普通的 user 消息：

```
main/impl finished its turn
```

**通知是一声 ping，不是它的答案。** 动作的参数在 `subscribe` 那一刻就定死了（上面那句
`main/impl finished its turn`），匹配时原样执行，**不带**这一轮的 `outcome` / `steps` / `answer`。
要读它说了什么、做了什么，用 `get_messages --session main/impl`（见下）。模式两端锚死
（`^…$`），所以 `main/impl` 不会听见 `main/impl/2` 的回合——那正是 `spawn` 替你拼的东西。

**动作直接走命令层，不起 shell。** 订阅匹配时服务端在自己的命令层里跑这个动作，所以没有
`--bash`、没有 named shell、也没有 `GONGGA_EVENT_*` 环境变量——那些都是旧机制，已经退役。
动作能调的就是这个会话能调的命令，越不过命令层；它跑在服务端里，不依赖哪台机器上有没有
`gongga-cli`。

**动作也会失败，那时它不响。** 命令层跑这个动作，失败（比如 `send_message` 被拒）只留下
一条 `kind=subscription` 的日志行（`ok=false` 加原因）。查它：

```bash
$G '{"name":"events","args":{"kind":"subscription","session":"'"$GONGGA_SESSION"'","since":"<派活前的 last_id>"}}'
```

正在停的服务端是例外：`cancel` 会等回调跑完，那时寄出的 `send_message` 变成一条 pending 行，
下次启动照常投给你。

它是**通知，不是结果**：它停了，不等于它说的话已经在你手里。要读，用 `get_messages`。

`--persistant true` 是"以后每一轮都告诉我"；不给就只有下一轮。订阅挂在**订阅者的名字**上：
`del_session` 删掉的如果是子会话，走的是子会话自己名下的订阅，你那条还在（它挂在你的名字
上）——而名字不会被重用，所以它不会再响，只有有人手写同一个名字建会话时才会。

## 派活：`send_message`

```bash
$G '{"name":"send_message","args":{"session":"'"$name"'","message":"实现 @file(docs/notes/plan.md) 里的第一步。约定见 @skill(whiting-dev)。
验收：cargo test -p gongga-server 绿。不要动 docs/。"}}'
```

brief 里有引号、反引号、多行，所以用 heredoc，不要单引号。`send_message` 在**回合被接收时
就返回**，不等回合跑完：它忙的时候拿到的是排队的一条消息，下一个 step 边界读到；
空闲时这一条直接把回合拉起来。

## 等它：`wait`（可选）

派完就接着干别的，那条通知会自己找上门。如果你这一刻**只想等这一轮**，用 `wait` 工具：

```
wait {time: 600000}
```

任何东西落到本会话它都返回（channel 结束的通知、订阅命中寄来的消息、人来消息），并且**什么都不消费**：
消息仍然在下一个 step 边界按原路进 transcript。超时不是错误，只是"还没到"。

**它会不会等空？不会，因为通知是回执。** `spawn` 在派活**之前**订阅并把回调装上，所以每一轮
`turn_end` 都必然给你寄一条：你忙，它作为一条排队的 inject 打断你正在等的 `wait`（实测：父会话
在 `wait` 里，子会话 20 ms 跑完，工具当时就回 `1 row(s) arrived … (a subscribe match)`）；
你闲，它就是把你叫起来的那条 user 消息。代价是这条回执要过一次命令层 dispatch（服务端里跑一次 send_message），不是直接写一行。所以"它早就结束了"不是 `wait` 能漏掉的事。

**但回执已经在你眼前时不要 `wait`。** `wait` 等的是"下一件"，不是"上一件"：那条通知如果已经
是这一轮的输入（它把你叫起来的，或上一步边界已经投递的），`wait` 里什么都等不到，只会坐满
超时——实测 2.5 s 的 `wait` 正是这样坐满的，而回执就在同一段上下文里。看一眼再等：它已经
说过 `turn_end`，就去 `get_messages` 读结果。

会等空的只有一种**写法**：订阅挂在事件之后。手写 `set_session` 建的会话没有订阅，事后补一条
`subscribe` 也只覆盖补上之后的回合（`--persistant false` 更只覆盖一轮）；那一条 `turn_end`
没有回执，`wait` 就只会坐到超时。自己搭的子会话要么补订阅、要么别 `wait`，直接看状态：

```bash
$G '{"name":"get_messages","args":{"session":"'"$name"'","range":"-1:"}}'     # data.status 是 idle 就已经结束
$G '{"name":"events","args":{"kind":"turn_end","session":"'"$name"'","since":"<派活前的 last_id>"}}'
```

**shell 里没有阻塞的等了。** 原先的 `wait --kind turn_end` 已经退役：一个睡在 socket 上的
进程既不进 transcript，也不会随回合取消。脚本想知道一轮什么时候结束就去问日志——
`events --kind turn_end --session <name> --since <id>`，那些行正是上面通知的正文。

## 读它：`get_messages`

```bash
$G '{"name":"get_messages","args":{"session":"'"$name"'","range":"-20:"}}'     # 它最后 20 行（负数是倒数，闭区间）
$G '{"name":"get_messages","args":{"session":"'"$name"'","range":"-1:"}}'       # 顺带看 data.status
$G '{"name":"interrupt_session","args":{"id":"'"$name"'"}}'              # 别干了，丢掉这一轮
$G '{"name":"del_session","args":{"id":"'"$name"'"}}'                    # 收尾：transcript、它名下的 channel 一起走
```

`status` 在 `cmd_result` 的 `data` 里，所以"它还忙不忙"不必查库。想一次看所有子会话
（只读打开，服务端在跑时也安全）：

```bash
python - <<'EOF'
import os, sqlite3
home = os.environ.get("GONGGA_HOME") or "~/.gongga"
db = sqlite3.connect(f"file:{os.path.join(os.path.expanduser(home), 'gongga.db')}?mode=ro", uri=True)
for name, status, tokens in db.execute("select name,status,context_tokens from sessions order by name"):
    print(f"{status:8} {tokens:>8}  {name}")
EOF
```

## 模型：会话自己的，抽一次就记住

`--model` 是 **sticky**：`set_session` 成功一次之后那个模型就存进客户端的 sticky，之后每条
不点 `--model` 的 `set_session` 自动带上。所以正常用法是**开工前给这个会话抽一次**，而不是
每条命令都写。

模型从 **model_pool** skill 拿：它在你点的那一档里、按各 provider 当前额度抽一个这台机器
真能跑、真有额度的，输出的就是 `<provider>/<model id>`：

```bash
$G '{"name":"skill","args":{"name":"model_pool","args":["pick","--tier","flash"]}}'   # 便宜活：读文件、跑测试、搬代码
$G '{"name":"skill","args":{"name":"model_pool","args":["pick","--tier","pro"]}}'     # 要判断：疑难实现、review 别人的改动
$G '{"name":"skill","args":{"name":"model_pool","args":["pick","--tier","sota"]}}'    # 一次错就全错：设计、核心 review
$G '{"name":"skill","args":{"name":"model_pool","args":["status"]}}'              # 每个模型：档位、能不能用、剩多少
```

**档是派的活决定的，不是抽出来的**；`flash` 是价格档不是能力档。档内抽到哪个由额度决定，
所以两条并行的子 agent 一般落在两个不同的 provider 上，正好不互相抢配额。它死在额度上时
（transcript 里一行 403/429）：`$G '{"name":"skill","args":{"name":"model_pool","args":["mark","<provider>/<model>"]}}'` 挡一会儿再抽，
不要自己编一个模型名。

不给 `--model` 时它跑**你**的模型（复制来的），不是服务端的默认模型：想让便宜活落到便宜档，
在这条 `spawn` 上点名（`--model "$($G '{"name":"skill","args":{"name":"model_pool","args":["pick","--tier","flash"]}}')"`）。服务端的默认模型
（`DEFAULT_SESSION_MODEL`，这台机器是 `deepseek-official/deepseek-flash`）只在你
`set_session` 一个什么都不新说的会话时才用得上；配置里的 `[model]` 是服务端自己想事情用的，
永远不是任何会话的模型。

换模型就是一次 `set_session --model`：它的 transcript 还在，下一轮就换过去了。

## brief 是它的第一句话：引用写进去

`send_message --session <name> <brief>` 的 brief 就是子 agent 的**第一条** user 消息，而它的 index 就是**那一次**写下的：
brief 里写的 `@file(...)` / `@skill(...)` 的正文进系统提示的 index，一次付费，之后每一轮都是命中；
消息里 `@file(...)` 保持字面，`@skill(...)` 留成带路径的指针。

**head 只写一次**（`prompt_in_force`：有行就再也不渲染），所以 index 里永远只有 brief 点名
的那些。之后的消息再写引用**不会**补进 index：`@file(...)` 就只是一个路径字面，
`@skill(<名字>)` 被就地换成 `@skill(<名字>) (<它 SKILL.md 的路径>)`，正文没有进去——它得
自己去读。实测（`agent::index_of` 只在那一次跑）：

```
probe 的第一条 = "Start here with @file(early.md) please."
  -> prompt row rooted = ['@file(early.md)']，index 里有 EARLY 正文
probe 的后一条 = "Now also look at @file(late.md) and @skill(late), then stop."
  -> 存下的行：@file(late.md) 原样；@skill(late) (…\late\SKILL.md)
  -> 同一轮请求的 instructions 里没有 LATE 正文
```

所以：**要它读什么，在 brief 里一次写完。** 写成路径（`plan.md`）或者让它自己 `cat`，
那份正文就躺在 transcript 中间，每一轮都跟着前缀一起重发——这就是 brief 比"到时候再说"便宜
的全部原因。拼写规则与写错的后果见 `@skill(prefix-cache)`。

两条最容易踩的：

- **括号写全。** `@file(docs/notes/plan.md)` 对；`@file: docs/notes/plan.md` 和
  `@file(docs/notes/plan.md`（漏了右括号）都不认——它们只是一段文字。括号里的路径有空格、
  有括号都没关系，解析器按配对读。
- **读不出来的引用不报错**：消息照发，index 里没有它，日志里没有一行。写错时唯一的信号是
  "模型还是自己去读了那个文件"。

## 规矩

1. **brief 必须自足。** 它看不到你的 transcript，只有你发的那条消息和它自己的 cwd：要它做
   什么、验收标准、不许动什么，都写进去。把引用写进去是同一件事的省钱版本：不是把文件内容
   贴进 brief，而是让 index 替你贴。
2. **一份工作树只有一个写者。** 两个会话同时改同一份文件会互相覆盖：给它 `git worktree add`
   一份自己的，或者让它只读。名字也一样：**不要手写会话名**，用 `spawn`——`set_session`
   撞名不会报错，会把你的 brief 注进别人的会话。
3. **它的上下文记在它自己账上。** 父会话不会因为它干活而变大，这就是它的价值：一个会话承载
   全部工作时，上下文会一路涨到阈值以上几倍。反过来，它收到 `Context notice` 时也要立刻
   开始培养继承人，和你没有区别。
4. **通知是一声 ping，不替它担保。** 那条 `send_message` 只说"它这一轮结束了"，不带答案。
   它说了什么、改了什么、走了哪条路，都要 `get_messages` 读 transcript 才知道；别把"回合结束"
   当成"活干对了"。
5. **它和你抢同一个 provider 配额。** 并发越多，单个请求越慢；开到你自己读得过来的数量，
   并用 model_pool 把它们摊到不同 provider 上。
6. **它先留着，删不删你说了算。** 子 agent 的 transcript 是**返工时唯一能读的东西**：活没干好，
   你要读它走过的路、把它叫醒重做，或者换个角色再来一遍，都靠这份 transcript。所以**不要在
   收工时顺手 `del_session`**——它连 transcript 和它名下的 channel 一起删，删了就没了。
   什么时候删由你决定：确认交付、不再需要它了，再删。
   删掉的如果是 `main/impl`，它名下的会话 `main/impl/review` 不会跟着走——它们按名字挂着，
   树只要还有一层在就仍然画得出来——但 `main/impl` 名下的**订阅**会跟着走，所以 `review`
   之后没人再听见。
7. **它是普通会话，不是过程。** 不用新进程、新服务端、新 home 或新端口；`set_session`、
   `interrupt_session`、`del_session`、`set_render_session` 对它都和对你自己一样。
