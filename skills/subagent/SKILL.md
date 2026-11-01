---
name: subagent
description: 把一块独立的工作交给另一个会话并行做——子 agent 不是一个工具，而是一个普通会话：自己的 transcript、自己的上下文与阈值、自己的 turn。当你要并行推进两条互不相干的实现、要把一个大的验证/探查丢给别人而自己不涨上下文、或者要一个只读的侦察兵时，读这个。用 gongga-cli set_session / send_message / get_messages / del_session 操作它；会话的模型自己指定（`--model` 是 sticky，设一次就一直带上），从 `@skill:model_pool` 抽一个能跑的。
---

# 子 agent

这里没有"subagent 工具"，也不需要：**一个子 agent 就是一个普通会话**——它有自己的
transcript、自己的上下文与压缩阈值、自己的 turn，跑在同一个服务端上。2026-10-30 实测：
给 `main` 和 `sub1` 各发一条消息，两行 `turn start` 相隔 8 ms 出现，两个模型请求并行发出，
两次 `turn end` 同时回来。并发是真的——turn 的互斥锁是**每个会话一把**
（`SessionHandle::gate`），不是全局。

```bash
S=~/.agents/skills/model_pool/model_pool
G=gongga-cli
$G set_session --session sub1 --cwd /path/to/its/checkout \
    --model "$($S pick)" --context_limit 20%
$G send_message --session sub1 --message '<自足的 brief>'
$G get_messages --session sub1 --range -20:     # 它说到哪了
$G interrupt_session --session sub1             # 别干了，丢掉这一轮
$G del_session --session sub1                   # 它在你名下，收尾是你的活
```

## 派完要等它做完：`wait_turn`

`send_message` 在**回合被接收时就返回**（实测 ~70 ms），不等回合跑完。所以"派出去然后接着
做别的"很容易变成"在它还写文件的时候就去读结果"。`wait_turn` 是那个"等"：

```bash
$G send_message --session sub1 --message '<brief>'
~/.agents/skills/subagent/wait_turn sub1        # 阻塞到它这一轮结束
```

输出一行 JSON，退出码说明状态：

```json
{"session": "sub1", "status": "idle", "turn": "done"}
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
$G wait --kind turn_end --session sub1     # 打印下一条这样的日志行就退出
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
$G set_session --session sub1 --cwd <它的工作树> --model "$($S pick --tier sota)"
```

不给 `--model` 也不是错误：会话落到服务端的默认模型（`DEFAULT_SESSION_MODEL`，这台机器
是 `deepseek-official/deepseek-flash`），或者落到你上次 sticky 住的那个。配置里的
`[model]` 是服务端自己想事情用的，永远不是任何会话的模型——别指望在那儿写默认值。

模型从 **model_pool skill** 拿：它按 flash / pro / sota 三档、按各 provider 当前
额度抽一个这台机器现在真能跑的模型，输出的就是 `<provider>/<model id>`，可以直接
喂给 `--model`：

```bash
$S pick                          # 随便抽一个能的
$S pick --tier flash             # 便宜活：读文件、跑测试、搬代码
$S pick --tier sota              # 硬活：设计、疑难定位
$S pick --provider kimi-coding   # 只从某个 provider 里抽（比如只有它还有额度）
$S status                        # 每个模型：档位、能不能用、剩多少
```

要点：`flash` 是**价格档不是能力档**——这台机器上最聪明的模型可能就在 flash 里；
`sota` 买的是贵的前沿，不是"最划算的答案"。抽到哪个由额度决定，所以两条并行的
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

## 规矩

1. **brief 必须自足。** 它看不到你的 transcript，只有你发的那条消息和它自己的 cwd：
   要它做什么、验收标准、不许动什么，都写进去。
2. **一份工作树只有一个写者。** 两个会话同时改同一份文件会互相覆盖：给它
   `git worktree add` 一份自己的，或者让它只读。
3. **它的上下文记在它自己账上。** 父会话不会因为它干活而变大——这就是它的价值：一个
   会话承载全部工作时，上下文会一路涨到阈值以上几倍；把两条独立的实现分出去，父会话
   就能一直待在阈值以下。反过来说，它收到 `Context notice` 时也要立即开始培养继承人，和你没有区别。
4. **没有完成通知。** turn 结束只意味着它 idle；你要自己去读（`get_messages` 或上面那张
   表）。别指望一条消息回来。
5. **它和你抢同一个 provider 配额。** 并发越多，单个请求越慢；开到你自己读得过来的数量。
   用 `model_pool` 把它们摊到不同 provider 上，是这里唯一有效的缓解：抽到同一个就再抽。
   某一轮真的死在额度上（子 agent 的 transcript 里会有一行 403/429），`model_pool mark`
   那个路由，然后重抽——不要自己编一个模型名。
6. **收尾**：`del_session` 会连它的 transcript 和 detached 作业一起删。留着不管，它就一直是
   一个占着上下文的空会话。
7. **换模型就是一次 `set_session --model`。** 同一个子 agent 换个模型重试用的是它的
   transcript：`set_session --session sub1 --model "$($S pick)"`，下一轮就换过去了，
   历史还在。它跑的那段路不值得留，就 `del_session` 重开一个。

它不是新进程、不是新服务端，也不用新 home 或新端口。
