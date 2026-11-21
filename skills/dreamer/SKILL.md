---
name: dreamer
description: 收到 keepalive notice 后的两种选择——`dream()` 复制当前会话成一个一次性的 dreamer 去整理文档，并静音原会话的计时器；`mute_keep_alive()` 只静音计时器，表示当前工作不需要再提醒。任何发往原会话的消息都会解除静音。这个包的函数是 `dream()` 与 `mute_keep_alive()`，均无需参数。
---

# Keepalive 是一个决定，不是一个命令

服务端在缓存将冷时只问一次：要不要开一个一次性副本整理文档，要不要交接，或者现在不需要提醒。选哪一个由你决定；这个包提供前两种回答中的两种（交接用 `@skill(handoff)`）。

| 函数 | 做的事 |
| --- | --- |
| `dream()` | `clone_session` 复制当前会话到 `<session>/dream/<时间>`，静音当前会话的 keepalive，再把 dream notice 发给副本、启动它的 turn |
| `mute_keep_alive()` | 只静音当前会话的 keepalive；不开副本，不改上下文 |

两个函数都在当前会话的 shell 里运行：会话名取自 `GONGGA_SESSION`，命令走 `GONGGA_CLI`。不用写数据库——工具调用和它的结果之间多一行，provider 就收不到合法的 transcript。

## 什么时候用哪个

- 想让缓存继续热、同时把 `.agents/notes/` 与 `docs/notes/` 的文档合并去重：用 `dream()`。副本拿走当前 prefix，整理完就把它删掉（它是普通会话，不会自动回收）。
- 工作还会安静地持续很久，但现在没有文档要整理、也不想交接：用 `mute_keep_alive()`。
- 工作本身需要换一副更大的工作记忆：不要调用本包，改调 `@skill(handoff)`。

静音只管计时器。任何发到这个会话的 `send_message`（包括你自己随后发的）都会解除静音，下一次 turn end 会重新计时。
