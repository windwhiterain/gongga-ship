---
name: providers
description: 一个会话因为**供应商**而失败时，服务端把 `@monitor/providers` 交给你，并用这份文件当你的起点——除了 `@monitor/disk`（满盘，读 `clean-disk`），这是唯一的 error 型 monitor：只收 LLM 请求相关的失败（请求被拒、流没结束、模型不在 roster、没有 key），别的错误不会叫醒它。你要回答的是「是鉴权、额度、模型还是 harness」并把该改的那一处改对：本文件给的是这台机器上 roster / 密钥 / 额度脚本的位置与规则、复现一次失败的最小方法、以及「改了什么才算修好」。控制台会话 cookie 缺席（`qwen-console` / `minimax-console`）时走 §4b：从用户已登录的浏览器里拿，不用等人去签。普通 agent 拿到它也照此更新 provider。
---

# Providers

你不是被用户叫来的，是被**服务端**叫来的：某个会话的 turn 因为模型请求失败而收了 `error`，
服务端把那一份报告交给这个会话。你默认没有上下文——除了系统提示词里的这份文件，和那份报告。

**你的产物是一次真实的修复**：要么改 roster / 密钥 / 额度脚本，要么证明这不是 provider 的
问题并说明该谁修。**不是一份分析。**

## 0. 先分清是谁的问题（唯一容易做错的判断）

| 层 | 长什么样 | 谁修 |
| --- | --- | --- |
| 供应商 | `401`/`403`（key 无效、模型未购买）、`402`/`429`（欠费/限流）、4xx 参数、上游 5xx | 换 key / 换模型 / 等；roster 只记事实 |
| 配置 | 模型不在 roster、`key_env` 指向的变量没有值、模型名写错、`context` 没声明、额度脚本读不出来 | 本文件 §3–§5（cookie 缺席看 §4b），改 roster 或脚本 |
| harness | panic、请求体构造错、状态机、迁移 | 不是这份报告的范围；说清楚并停 |

**别猜，复现。** 拿 roster 里的端点和 key，用 `curl` 在 harness 之外发一次同样的请求
（`docs/notes` 里没有魔法，`providers.toml` 的 `endpoint.base_url` 就是答案）。

```bash
# key：先 ~/.agents/keys.toml 的 [keys]，再 key_env 指的环境变量
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/chat/completions" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"'"$MODEL"'","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
```

外面也失败 → 不是 harness 的锅，去 §3–§5 修配置或记账；外面成功而 harness 失败 → 记下两边
差异（端点、头部、请求体），那才是真 bug，交给用户/源码。

`crates/server/src/llm.rs` 的重试分类是设计：**`408`/`429`/`5xx` 重试，其它 4xx 不重试**。
别去"修"一个让 turn 直接失败的 400。

## 1. 这台机器的事实在哪

| 路径 | 是什么 | 注意 |
| --- | --- | --- |
| `~/.agents/gongga-agents/providers.toml` | **roster**：provider / 模型 / tier / 价格 / 缓存 / `quota_query` | 检出在 `local` 分支，上游是 `main`；改它 = 改这台机器的事实 |
| `~/.agents/keys.toml` | 密钥表，`[keys]` 是 provider id → key | **不在 git**，`~/.agents` 不是仓库；别抄进 roster |
| `~/.agents/gongga-agents/providers/*.py` | 额度/价格脚本（reader） | 契约见 §4 |
| `$GONGGA_HOME/server.log` | 服务端日志，一行一条带字段 | `model request: rejected` / `no answer` 就在这里 |
| `$GONGGA_HOME/gongga.db` | 会话、消息、事件（**只读打开**） | 失败原因在最新的 `notice` 行 |
| `~/.model_pool/state.json` | 抽签用的读数缓存（可删） | 缓存的语义见 §4 |

服务端不替你猜额度：它跑 roster 里**带 `script` 的 `quota_query`，按文件顺序**，第一条答上来的
算数（`crates/server/src/quota.rs`）。文件顺序就是偏好顺序。

## 2. 报告里最先看的三样

1. **它的模型** —— `provider/model`，去 roster 找这一条：`retired_ids`、`tier`、`context`、端点。
2. **最新的 `notice` 行** —— 服务端写下的原话（`chat/completions: HTTP 401 …` 之类），
   它比日志里的时间线更接近原因。
3. **`server.log` 的上下文** —— 同一时刻的重试、限流、`usage` 行；`429` 与 `402` 的区别常在
   这里（前者等，后者要钱）。

## 3. 改 roster 的规则

- **只写事实**：模型 id、上下文、模态、价格、端点、`tier`、`retired_ids`。价格与字段要带
  `source` 与 `retrieved`；拿不准就写 `unverified` 并说明，别留空。
- **key 永不进 roster**：`key_env` 只指环境变量名。密钥放 `~/.agents/keys.toml`。
- **`tier` 由人定**：`flash` / `pro` / `sota`。你是来修"跑不起来"的，不是来改这台机器用哪个
  档位的；报告里写着"同档换模型可以"，跨档不行。
- **一个 provider 可以有多条 `quota_query`**：订阅额度、钱包、积分各自一条，按文件顺序试。
  只写 `url`/`jsonpath` 而没有 `script` 的条目是给人看的文档，机器读不到。
- 改完整理：`python -c "import tomllib;tomllib.load(open('providers.toml','rb'))"` 必须过。

## 4. 写一个额度 reader（`providers/*.py`）

契约（`providers/contract.py` 已经实现大半，照抄同类脚本）：

- **stdout 最后一行**是一个 JSON 读数：`{"remaining": n, "windows": [...], "detail": "…"}`；
  前面可以有别的行。
- **exit 2 + stderr 一句** = 「我问到了，答案是**没有**」。这是**答复**，不是沉默：
  池子会把旧读数丢掉。超时/起不来才算沉默，旧读数保留。
- `unit` 由 roster 那条写着：`percent-left`（窗口百分比）、`cny-left`（钱包余额）、
  `credit-left`（预付积分：钱，不是百分比）。
- **凭据**：多数 reader 要的是 provider 的 API key；有的要**控制台会话 cookie**，那就自己从
  `~/.agents/keys.toml` 的独立条目读（例：`minimax-console`），拿不到就明确拒绝，别把 API key
  当 cookie 发出去。取 cookie 的四步和四个坑在 §4b。
- **绝不转发响应里的秘密**：有的端点会把账户的 `api_key` 原样带回来（MiniMax 的
  `/backend/account/token_plan_credit` 就是），只取数字。
- **没有 key 可读的额度就明说**：写进 roster 的 `note`，别让下一个人以为它坏了。

## 4b. 控制台会话 cookie：这台机器上已有的会话，不是凭空签一个新的

有的额度只在控制台里，凭据是**浏览器会话**而不是 key。reader 自己从 `~/.agents/keys.toml`
的独立条目读（`qwen-token-plan` 的额度 → `qwen-console`，`minimax-cn` 的积分 →
`minimax-console`）。条目缺席是「没有可问的凭据」，不是「额度为 0」，也不是「用户得重新登录」——
两者要分开报，否则下次读的人会去查一个没坏的账号。

harness 传给脚本的是**模型 key**（`providers.rs` 的 `QuotaAccess.key`，注释说「和请求一样的凭据」），
所以 cookie 型 reader 必须自己按名字拒掉它，再自己去找会话：`qwen.py` / `minimax_credit.py` 里的
`keystore_cookie` + `cookie_of` 就是这份形状，新写一个照抄，别发明第二套条目名。

**取 cookie 的四步**（2026-11-11 对 `qwen-token-plan` 实测；日常那个浏览器里登录着就够了）：

```bash
$ gongga-cli '{"name":"run_skill","args":{"id":"general/browser","cmd":{"args":["attach"]},"session":"$GONGGA_SESSION"}}'          # 接用户在用的 Edge，不重启、不拷 profile
$ gongga-cli '{"name":"run_skill","args":{"id":"general/browser","cmd":{"args":["cookies","qianwenai"]},"session":"$GONGGA_SESSION"}}' > /项目内的临时文件
#   ↑ 每行 `域 名 = 值`，含 HttpOnly 的 login_qianwenai_ticket
# 拼成 name=value; name=value，写成 keys.toml 的 qwen-console
$ gongga-cli '{"name":"run_skill","args":{"id":"general/browser","cmd":{"args":["stop"]},"session":"$GONGGA_SESSION"}}'            # 只释放连接，那个浏览器照常跑
```

写进去之后两步都要过：`python ~/.agents/gongga-agents/providers/qwen.py --timeout 15` 出读数，
`gongga-cli '{"name":"run_skill","args":{"id":"general/model_pool","cmd":{"args":["status","--provider","<id>"]},"session":"$GONGGA_SESSION"}}'` 复核。

四个坑，都是踩过的：

| 坑 | 现象 | 原因 |
| --- | --- | --- |
| 用页面里的 `document.cookie` | 只拿到 `cna/tfstk/isg`，用拼出来的头去打网关回 `NotLogined` | 会话票是 HttpOnly，JS 看不见；那句 `NotLogined` 说的是「没有会话」，不是「会话过期」 |
| 直接读浏览器的 cookie 数据库 | `Device or resource busy` | Edge 运行中独占那个文件；而且 Chromium 136 起，真实 profile 上 `--remote-debugging-port` 被静默忽略，重启浏览器也没用 |
| 把值打印到 stdout | 值进了 transcript（`<home>/gongga.db`） | cookie 是活凭据：用文件或剪贴板搬，只打印**名字** |
| 以为抓一次就永久 | 几天后又 `NotLogined` | 会话有寿命：`GET /tool/user/info.json` 的 `data.sessionExpireTimeStamp`（毫秒）。到期照上面四步再来一遍；在意泄露就去控制台退出重登，换一把新的 |

`browse attach` 需要**人**在 `edge://inspect/#remote-debugging` 勾一次「Allow remote debugging
for this browser instance」——这一步 agent 不代办；勾过之后这台机器上一直是开的，所以通常是
直接 `attach` 就成。

## 5. 改完怎么证明

```bash
# 1) 读数本身
python ~/.agents/gongga-agents/providers/<script>.py --timeout 10     # 直接跑，看最后一行

# 2) 池子怎么看它（真的会跑脚本，不是读缓存）
gongga-cli '{"name":"run_skill","args":{"id":"general/model_pool","cmd":{"args":["status","--provider","<id>","--refresh","0"]},"session":"$GONGGA_SESSION"}}'

# 3) 一次真请求（不是探针）：用报告里那个模型，一句话
```

三条都过才算修好。只跑第 2 条会被缓存骗：`status` 的第二行是脚本自己的 `detail`，
第一行才是判定。

## 6. 报告

一条消息写完：**谁的锅**（以及你排除另外两层的证据）、**改了什么**（文件 + 一句话）、
**证明**（上面三条的输出摘要）、**还没解决的部分**。改动落在 `~/.agents/gongga-agents` 的
`local` 分支时，提交要单独一次，别和别的机器事实混在一起。

不要做的：改这台机器的档位选择、动别的会话、删用户数据、把 key 写进任何会被提交的文件。
