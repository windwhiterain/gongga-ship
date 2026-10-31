---
name: subagent
description: 把一块独立的工作交给另一个会话并行做——子 agent 不是一个工具，而是一个普通会话：自己的 transcript、自己的上下文与阈值、自己的 turn。当你要并行推进两条互不相干的实现、要把一个大的验证/探查丢给别人而自己不涨上下文、或者要一个只读的侦察兵时，读这个。用 gongga-cli set_session / send_message / get_messages / del_session 操作它。
---

# 子 agent

这里没有"subagent 工具"，也不需要：**一个子 agent 就是一个普通会话**——它有自己的
transcript、自己的上下文与压缩阈值、自己的 turn，跑在同一个服务端上。2026-10-30 实测：
给 `main` 和 `sub1` 各发一条消息，两行 `turn start` 相隔 8 ms 出现，两个模型请求并行发出，
两次 `turn end` 同时回来。并发是真的——turn 的互斥锁是**每个会话一把**
（`SessionHandle::gate`），不是全局。

```bash
G=gongga-cli
$G set_session --session sub1 --cwd /path/to/its/checkout \
    --llm <provider>/<model> --context_limit 20%
$G send_message --session sub1 --message '<自足的 brief>'
$G get_messages --session sub1 --range -20:     # 它说到哪了
$G interrupt_session --session sub1             # 别干了，丢掉这一轮
$G del_session --session sub1                   # 它在你名下，收尾是你的活
```

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
   就能一直待在阈值以下。反过来说，它自己要交棒时走 `heir`，和你没有区别。
4. **没有完成通知。** turn 结束只意味着它 idle；你要自己去读（`get_messages` 或上面那张
   表）。别指望一条消息回来。
5. **它和你抢同一个 provider 配额。** 并发越多，单个请求越慢；开到你自己读得过来的数量。
6. **收尾**：`del_session` 会连它的 transcript 和 detached 作业一起删。留着不管，它就一直是
   一个占着上下文的空会话。

它不是新进程、不是新服务端，也不用新 home 或新端口。
