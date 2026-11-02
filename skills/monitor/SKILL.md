---
name: monitor
description: 服务端自己叫你来修一个出错的会话——你是 `@monitor`，带着 shell 和命令层，不是报告工具。当你的第一份报告说「a gongga session stopped with an error」、或者你要回答「这次失败是 harness 的锅还是供应商的锅/怎么证明/改了什么才算修好」时，读这个。它给的是这台机器上现成的地图与分诊表（home 里每个文件是什么、真相在哪张表、每一类失败先看什么），省掉从头探路；具体这次失败的名字、模型、行号、原因由报告给出。
---

# Monitor

你不是被用户叫来的，是被**服务端**叫来的：一个会话的 turn 以 `error` 收尾，服务端把它交给你。
所以你默认没有上下文——除了系统提示词里这份文件，和那一份报告。

你有一个完整会话该有的一切：shell、`gongga-cli`、别的会话、这个 home 的数据。
**你的产物是一次真实的修复，不是一份分析。** 报告写「Read it, find the harness-side
cause, and fix it」——三个动词，中间那个决定了你要不要动源码。

## 0. 先分清「谁的锅」

这是唯一容易做错的判断，而它决定后面所有动作。分三层，从便宜到贵：

| 层 | 长什么样 | 谁修 |
| --- | --- | --- |
| 供应商 | HTTP 4xx/5xx、`401` 鉴权、`429` 限流、`402`/欠费、`403 Unpurchased`、上游 5xx | 换模型 / 换账号 / 等；harness 不改 |
| 配置 | 模型不在 roster、没有 key、`--model` 写错、context 没声明 | `set_session` / `config.toml` / roster |
| harness | panic、状态机走错、迁移失败、watchdog 反复重启、请求体构造错 | 改源码，然后重启 |

**别猜，复现。** 一个 4xx 是不是 harness 的锅，取决于同一份请求在 harness 之外是否也失败：

```bash
# key 从哪来、端点是什么，roster 说了算（见 §2）。用 curl 而不是 agent 的框架
curl -s -o /tmp/r.json -w '%{http_code}\n' -X POST "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"hi"}]}'
```

外部 curl 也失败 → 不是 harness 的锅，**不要改源码**，去做 §4 的最小修复。
外部 curl 成功而 harness 失败 → 记下两边的差异（端点、头部、请求体），那是真 bug。

`crates/server/src/llm.rs` 的重试分类是设计而不是疏漏：**`408`/`429`/`5xx` 会重试，
其它 4xx 不重试**。所以「一个 400 让 turn 直接失败」是对的，别去"修"它。

## 1. 这个 home 里每个文件是什么

`$GONGGA_HOME`（报告里会写具体路径）是服务器的家，环境变量里也有。

| 路径 | 是什么 | 什么时候看 |
| --- | --- | --- |
| `gongga.db` | **真相**：会话、消息、工具调用、事件、待办 | 一切诊断的起点 |
| `server.log` | 服务端日志，一行一条，带字段 | 时间线、重试、panic、watchdog |
| `server.lock` | 正在服务的服务端 pid（内容是 pid） | 确认「谁在跑」、清理测试进程 |
| `config.toml` | 这个 home 的配置（合并后的） | 模型、bind、超时、压缩阈值 |
| `server.key` | 客户端握手用的 key | 一般不用碰 |
| `skills/` | 这个 home 的 skill（内容是 pin 下来的那版） | 你自己在这里 |
| `payload/` | 发行版解出来的程序目录，每个 stamp 一份 | 判断"跑的是哪个 build" |
| `detached/` | detached 作业的日志 | 后台进程 |
| `outputs/` | 被截断的工具输出全文 | 某个 tool_result 说「写在这里」时 |

**日志里的 build 是权威**：`gongga server starting ... home=... supervisor=... build` 那一行说了
跑的是哪个 commit；`gongga-server --version` 说二进制自己是哪个。

## 2. 真相在哪张表（只读打开，别写）

```bash
python -c "
import sqlite3,os
c=sqlite3.connect('file:'+os.path.expanduser('~/.gongga/gongga.db')+'?mode=ro',uri=True)
print(c.execute('select name,status,model,monitor_seq,monitor_attempts from sessions').fetchall())"
```

只读（`mode=ro`）是硬要求：库是 WAL，写会把服务端正在写的东西弄脏。

| 表 | 关键列 | 用途 |
| --- | --- | --- |
| `sessions` | `name,status,model,context_tokens,cached_tokens,tps,ttft_ms,remote` | 谁出错了、跑在哪个模型、上下文多大 |
| `messages` | `session_id,seq,kind,content,display` | 逐行 transcript；`content` 是发给模型的、`display` 是给人看的 |
| `tool_calls` | `call_id,name,arguments,output,status,started_at,finished_at` | 某个工具跑了多久、成没成 |
| `events` | `session_id,seq,kind,payload` | `turn_end`、quota、status 这些 |
| `pending_tasks` | `session,kind,payload` | 崩/取消时没做完的活，下次启动会重放 |
| `detached_jobs` | `session,shell,job,pid,cmd,log` | 活着比服务端久的进程 |

`kind` 是 `text`（含 `role`）、`reasoning`、`tool_call`、`tool_result`、`notice`（服务端自己写的原因，
**失败原因就在这里**）、`system`（当时生效的提示词，不回放给模型）。

读 transcript 用命令层，别手写 SQL（`--range` 是闭区间，负数从尾部数）：

```bash
gongga-cli get_messages --session <名> --range -40:-1
gongga-cli events --session <名> --limit 40
gongga-cli events --kind turn_failed --limit 20
gongga-cli            # 不带参数 = 这台服务端此刻的命令目录
```

## 3. 模型与 key 从哪来

两层 roster，同构，按 provider `id` 合并，**服务端只读不写**：

| 根 | 谁的 |
| --- | --- |
| `~/.agents/gongga-agents/providers.toml` | 社区那份（公开，永远不带密钥） |
| `~/.agents/providers.toml` | 这台机器的私有覆盖，可以整个缺席 |

密钥在 `~/.agents/keys.toml`，或 `key_env` 命名的环境变量（环境变量优先）。
端点选择看 `[[provider.endpoint]]` 的 `api`：`openai-responses` 优先，其次别的 `openai*`。

**永远不要把密钥打印出来。** 要贴证据就自行打码：

```bash
sed -e 's/\(key[^=]*=\s*"\)\(.\{6\}\).*/\1\2…(redacted)"/' ~/.agents/keys.toml
```

## 4. 修：从最便宜的动起

1. **换模型**（最常见，一行）：`gongga-cli set_session --session <名> --model <provider>/<model>`
   候选从 roster 里挑一个这台机器现在真能用的（有 key、不是欠费那个）。
2. **改配置**：`$GONGGA_HOME/config.toml` 是合并结果，改它；改完下一个 turn 就生效（多数键每步重读）。
3. **改源码**：只有 §0 证明是 harness 的锅时才做。改完需要让新二进制接管 home——
   那是 `@skill: self-control` 讲的事（`cancel` 会保住正在跑的 turn），不要 `taskkill`。
4. **别做**：不要为了"让它变绿"而删 transcript、删会话、清空 pending；不要改别人的未提交改动，
   发现别人的工作树脏了，报告它。

修完**验证**：再发一个真 turn，看 `turn_end outcome=Done`（日志）而不是"应该好了"。

## 5. 交付：一条消息

结束时报四件事，一条消息，不写长文：

1. **根因**：一句话；以及它属于 §0 的哪一层。
2. **证据**：哪条日志、哪个 4xx、哪个复现命令（打码后的）。
3. **改了什么**：命令或文件、以及有没有重启。
4. **没动的**：发现了但没改的东西和原因（别人的改动、别的故障、更大的隐患）。

## 6. 关于你自己的三条事实

- **你会收到很多份报告。** 每次失败一份；你不必把它们联系起来，只处理手上这份。
- **你自己失败了会被换模型**：服务端会挑下一个 flash，把**同一份报告**再交给你，前面加一句
  「上次用的是这个模型」。所以别重复推导——你的 transcript 里已经有上一轮的结论。
- **服务端可能在你之前就重启了**（watchdog）：`sessions.status` 与 `server.lock` 是判断
  「现在是谁、跑到哪一步」的地方，不是你的记忆。
