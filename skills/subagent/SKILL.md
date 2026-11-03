---
name: subagent
description: 把一块独立的工作交给另一个会话并行做——子 agent 不是一个工具，而是一个普通会话：自己的 transcript、自己的上下文与阈值、自己的 turn。当你要并行推进两条互不相干的实现、要把一个大的验证/探查丢给别人而自己不涨上下文、或者要一个只读的侦察兵时，读这个。用 gongga-cli set_session / send_message / get_messages / del_session 操作它；会话的模型自己指定（`--model` 是 sticky，设一次就一直带上），从 `@skill:model_pool` 按档抽一个能跑的（`pick --tier <flash|pro|sota>`，档由派的活决定）。brief 就是这个会话的第一条消息，所以写文件/技能一律用 `@file:`/`@skill:`（`@skill:prefix-cache`）。**不要手写会话名**：用本 skill 的 `spawn`，名字是路径 `<父会话>/<角色>`，撞了就加 `/2`，靠 `set_session --must_create` 保证不会选中别人的会话。
compatibility: 需要 Python 3、sqlite3，以及一个在 PATH 里的 gongga-cli（spawn / wait_turn）
metadata:
  entry: run
---

# 子 agent

这里没有"subagent 工具"，也不需要：**一个子 agent 就是一个普通会话**——它有自己的
transcript、自己的上下文与压缩阈值、自己的 turn，跑在同一个服务端上。2026-10-30 实测：
给 `main` 和 `main/impl` 各发一条消息，两行 `turn start` 相隔 8 ms 出现，两个模型请求并行发出，
两次 `turn end` 同时回来。并发是真的——turn 的互斥锁是**每个会话一把**
（`SessionHandle::gate`），不是全局。

## 起一个：名字是路径，撞了自动加段

**不要手写会话名。** `set_session` 是"没有就建、有就选中"，两种情况答的都是
`session is now X`——所以两个人、或者同一个人两次，都写 `sub1`，第二个 brief 会**静默注进
第一个的 transcript**：两个 agent 共用一个上下文、互相打断，而且事后看不出来。

名字由 `spawn` 决定，规则是一条路径：

```
<父会话>/<角色>          例：main/impl、main/scan
<父会话>/<角色>/<n>      那个名字已被占：main/impl/2、main/impl/3
```

父会话默认是 `$GONGGA_SESSION`，所以从 `main` 起的子 agent 叫 `main/impl`，它自己再起的
叫 `main/impl/review`。不同父会话的路径天然不同，**跨父撞名不可能**；同一个父下面重名，
段往上加。名字就是它在会话列表树里的位置。

```bash
G=gongga-cli
SKILL_DIR=<本 SKILL.md 所在目录>
S="$G skill model_pool"

name="$("$SKILL_DIR/spawn" impl --cwd /path/to/its/checkout \
        --model "$($S pick --tier flash)" --context-limit 20%)"
echo "$name"          # main/impl，或者 main/impl/2

$G send_message --session "$name" --message '<自足的 brief>'
$G get_messages --session "$name" --range -20:     # 它说到哪了
$G interrupt_session --session "$name"            # 别干了，丢掉这一轮
$G del_session --session "$name"                  # 它在你名下，收尾是你的活
```

`spawn` 只把最终名字打印到 stdout（一行，方便 `$(…)`），撞名跳过了哪几个、模型抽到哪个
都走 stderr。它靠 `set_session --must_create true` 一次完成"选名字 + 建会话"：服务端在
同名已存在时**拒绝**这条命令，所以不存在"探测完、创建前"被别人抢走的窗口，也不可能
选中别人的会话。名字用完了就再加一段，**永远不会共用**。

`--parent` 用来指定别的父会话（默认 `$GONGGA_SESSION`）；`--model` 省略就落到服务端默认。
其它可选参数：`--context-limit 20%`。

## 派完要等它做完：`wait_turn`

`send_message` 在**回合被接收时就返回**（实测 ~70 ms），不等回合跑完。所以"派出去然后接着
做别的"很容易变成"在它还写文件的时候就去读结果"。`wait_turn` 是那个"等"：

```bash
$G send_message --session "$name" --message '<brief>'
"$SKILL_DIR/wait_turn" "$name"                  # 阻塞到它这一轮结束；gongga 里是 `gongga-cli skill subagent wait_turn <name>`
```

输出一行 JSON，退出码说明状态：

```json
{"session": "main/impl", "status": "idle", "turn": "done"}
```

| 退出码 | 意思 |
|---|---|
| `0` | 会话空闲了；`turn` 是这一轮真正的结果（`done` / `cancelled` / 失败时看 `failed`）|
| `1` | 会话停在 `error` |
| `2` | 连状态都问不到：会话名打错、CLI 拒绝（版本不一致等）|
| `124` | 到了 `--timeout`（默认 1800 秒）还在跑 |

`--interval`（默认 15 秒）是"一次等待最多多久，然后重新读一次状态"。它存在的唯一原因：
服务端的事件只送往**连接之后**发生的事，所以一个刚好在两次命令之间结束的回合没有任何事件
可等。用重读兜住这个空档，比让它挂到超时诚实。

**它为什么是脚本而不是 CLI 的一条命令。** CLI 的 `wait` 是个 primitive：

```bash
$G wait --kind turn_end --session <name>   # 打印下一条这样的日志行就退出
```

它不知道一轮是不是早就完了、多长算太久、失败意味着什么——这些都是**关于活儿的判断**，
而关于活儿的判断应该待在干这件活的脚本里，改了不用重编译。`wait_turn` 就是那层判断：
先读状态（已经空了就直接返回）、再等、再读。

同理，`wait --session` 是个**文本过滤器**：名字写错不是错误，是一个永远匹配不上的过滤条件，
和 `grep` 一样。要不要把它当错误，由调用方决定。

**模型是会话自己的，而且记性跟着你。** SPEC 把 `--model` 写成 `(sticky)`：`set_session`
成功一次之后，那个模型就存进客户端的 sticky，之后每条不点 `--model` 的 `set_session`
自动带上它。所以正常用法是**开工前给这个会话抽一次**，而不是每条命令都写：

```bash
name="$("$SKILL_DIR/spawn" review --cwd <它的工作树> --model "$($S pick --tier sota)")"
```

（`spawn` 的 `--model` 只是转给 `set_session`，sticky 因此照样生效。）

不给 `--model` 也不是错误：会话落到服务端的默认模型（`DEFAULT_SESSION_MODEL`，这台机器
是 `deepseek-official/deepseek-flash`），或者落到你上次 sticky 住的那个。配置里的
`[model]` 是服务端自己想事情用的，永远不是任何会话的模型——别指望在那儿写默认值。

模型从 **model_pool skill** 拿：它在你点的那一档里、按各 provider 当前额度抽一个
这台机器现在真能跑的模型，输出的就是 `<provider>/<model id>`，可以直接喂给 `--model`。
**档是派的活决定的，不是抽出来的**，所以每次都点 `--tier`：

```bash
$S pick --tier flash             # 便宜活：读文件、跑测试、搬代码
$S pick --tier pro               # 要判断：疑难实现、review 别人的改动
$S pick --tier sota              # 一次错就全错：设计、核心 review
$S pick --tier flash --provider kimi-coding   # 还要指定 provider（比如只有它还有额度）
$S status                        # 每个模型：档位、能不能用、剩多少
```

要点：`flash` 是**价格档不是能力档**——这台机器上最聪明的模型可能就在 flash 里；
`sota` 买的是贵的前沿，不是"最划算的答案"。档内抽到哪个由额度决定，所以两条并行的
子 agent 一般落在两个不同的 provider 上，正好不互相抢配额。子 agent 死在额度上时：
`$S mark <provider>/<model>` 把它挡一会儿，再抽一个。

**它忙不忙、上下文多大**：`get_messages` 看内容；只读查库看状态（服务端在跑时也安全）：

```bash
python - <<'EOF'
import sqlite3, os
c = sqlite3.connect(f"file:{os.path.expanduser('~/.gongga/gongga.db')}?mode=ro", uri=True)
print(c.execute("select name,status,context_tokens,context_limit from sessions").fetchall())
EOF
```

## brief 是这个会话的第一句话，把引用写进去

`send_message --message` 就是子 agent 的**第一条** user 消息，所以 brief 里写的
`@file:`/`@skill:` 会进它的 index、跟着系统提示走前缀缓存：**一次**付费，之后它每一轮
都是命中。写成路径（`plan.md`）或直接让它 `cat`，那份正文就躺在 transcript 中间，
之后每次请求都重新计费，而且它后面的一切都跟着一起重算。

```bash
$G send_message --session <name> --message "实现 @file: docs/notes/plan.md 里的第一步。
约定见 @skill: whiting-dev。验收：cargo test -p gongga-server 绿。不要动 docs/。"
```

两条必须遵守的：

- **值是一个词，后面要有空白。** `@file: docs/notes/plan.md` 对；在它后面直接接中文标点
  （`@file: docs/notes/plan.md，然后`）是错的——中文标点不是分隔符，值会变成
  `docs/notes/plan.md，然后`，读不到，而且**静默**。路径有空格就换一个没空格的路径，
  引用不吃引号也不吃转义。
- **技能要在 brief 里点名。** `@skill:` 只有第一句话里的会被索引；第二轮再补一条
  `@skill: foo` 不会进 index（它被就地换成一行 metadata），那份正文每轮都要重发。

派完再补的消息同样有效，但更贵：`@file:` 会在下一次重建时进 index（写一行新 prompt 行），
`@skill:` 则永远不进。所以**要它读什么，就在 brief 里写完**。细则与拼写表见
`@skill:prefix-cache`。

## 规矩

1. **brief 必须自足。** 它看不到你的 transcript，只有你发的那条消息和它自己的 cwd：
   要它做什么、验收标准、不许动什么，都写进去。上面那条"把引用写进去"是同一件事的
   省钱版本：不是叫你把文件内容贴进 brief，而是让 index 替你贴。
2. **一份工作树只有一个写者。** 两个会话同时改同一份文件会互相覆盖：给它
   `git worktree add` 一份自己的，或者让它只读。名字也一样：**不要手写会话名**，
   用 `spawn`——`set_session` 撞名不会报错，会把你的 brief 注进别人的会话。
3. **它的上下文记在它自己账上。** 父会话不会因为它干活而变大——这就是它的价值：一个
   会话承载全部工作时，上下文会一路涨到阈值以上几倍；把两条独立的实现分出去，父会话
   就能一直待在阈值以下。反过来说，它收到 `Context notice` 时也要立即开始培养继承人，和你没有区别。
4. **没有完成通知。** turn 结束只意味着它 idle；你要自己去读（`get_messages` 或上面那张
   表）。别指望一条消息回来。
5. **它和你抢同一个 provider 配额。** 并发越多，单个请求越慢；开到你自己读得过来的数量。
   用 `model_pool` 把它们摊到不同 provider 上，是这里唯一有效的缓解：抽到同一个就
   `--provider` 换个再抽，**档不变**（档是活决定的，不是抽出来的）。
   某一轮真的死在额度上（子 agent 的 transcript 里会有一行 403/429），`model_pool mark`
   那个路由，然后重抽——不要自己编一个模型名。
6. **收尾**：`del_session` 会连它的 transcript 和 detached 作业一起删。留着不管，它就一直是
   一个占着上下文的空会话。删掉的如果是 `main/impl`，它的子会话（`main/impl/review`）
   不会跟着走——它们是按名字挂着的独立会话，上层的树只要还有一层在就仍然画得出来。
7. **换模型就是一次 `set_session --model`。** 同一个子 agent 换个模型重试用的是它的
   transcript：`set_session --session <name> --model "$($S pick --tier <档>)"`，下一轮就换过去了，
   历史还在。它跑的那段路不值得留，就 `del_session` 重开一个。

它不是新进程、不是新服务端，也不用新 home 或新端口。
