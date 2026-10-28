---
name: latency
description: 定位延迟到底丢在哪一层，再在瓶颈之前把它整形掉——判断是不是代理/TUN 接管了流量、逐跳找出从哪一跳开始尖峰、同机同钟采集出流量与 RTT 并报分布、读 mihomo /connections 找出谁在吃上行、Windows QoS 限速与 DSCP 优先级。任何"游戏/API 延迟高、是不是代理的锅、谁占了带宽、怎么给某台机器限速"的问题用它。Windows 已实测；Linux 的 tc/HTB 未实现。
---

# 先定位到哪一层，再整形

用户给的假设通常不是原因。有人问"延迟高是不是代理"，实测常常是**完全无关**的问题
（别人的机器把上行打满）。所以这里的顺序是固定的：**先证明是哪一层，再动任何开关。**

```bash
~/.agents/skills/latency/latency doctor --app Overwatch.exe   # 是代理吗？
~/.agents/skills/latency/latency hops --dest 223.5.5.5       # 从哪一跳开始炸？
```

## 判断链

按顺序走，每一步都能把范围砍掉一半：

| 步骤 | 命令 | 它回答什么 | 典型结论 |
|---|---|---|---|
| 1 | `doctor --app X` | 有 TUN 接管吗？目标进程连到哪？ | "TUN 没接管，游戏直连" |
| 2 | `hops --dest X` | 第几跳开始出现 >100ms | "前两跳干净，第 3 跳炸" |
| 3 | `probe --peer Y` | 那台机器忙的时候 RTT 怎样 | "空闲最大 11ms，饱和时 62% 超 100ms" |
| 4 | `top --peer Y` | 谁在吃上行 | "api.minimaxi.com 占 70%" |
| 5 | `cap --peer Y on 8` | 整形后是否真的不排队 | "最大 25ms，零尖峰" |

**第 2 步是分水岭**：如果前两跳（自己的路由）干净、第 3 跳（ISP 网关）开始炸，
那就是**上行队列（bufferbloat）**，不是自己的路由器，也不是 Wi-Fi。这个判断一次就能
排除掉"换路由器/换网线/改 DNS"这些无效方向。

## doctor：先排除代理

```bash
latency doctor --app Overwatch.exe
```

它看四件事，并给出结论：

1. **默认路由落在哪个网卡**——只有持有 `0.0.0.0/0` 的 TUN 才真的接管流量。
2. **哪些网卡像隧道**（`et_*`/`tun*`/`vEthernet`/TAP/WireGuard）以及它是否持有默认路由。
3. **本地代理端口**在监听什么进程。
4. **目标进程每条连接的对端**，并标出哪些指向本地代理端口。

输出末尾直接给结论。注意一个容易查错方向的点：**网卡存在 ≠ 流量被接管**。
残留的虚拟网卡（EasyTier、关掉的 TUN）会显示 `Up` 并有 IP，但没有默认路由，
一条流量都不走它。

## hops：从哪一跳开始

```bash
latency hops --dest 223.5.5.5 --count 20
latency hops --dest 121.41.35.233 --peer awin   # 从另一台机器看
```

用**递增 TTL 的结构化 Ping** 探测路径，不解析 `tracert` 的文本——那份输出会随系统
语言变化，是脆的。每一跳探 N 次，报 `min/avg/p95/max` 和超阈值的包数。

- `(answered TTL, drops pings)`：这一跳回 TTL 但忽略后续 ping，很常见，不代表故障。
- 最后一行直接指出第一处尖峰，并区分是"自己的设备"还是"上游队列"。

## probe：为什么忙的时候卡

```bash
latency probe --peer awin --target 223.5.5.5 --seconds 60
```

在**同一台机器上**、**同一个时钟下**采两样东西：网卡出字节数、到目标的 RTT。
然后按"出流量高低"分组，报每组 RTT 的分布。

这是整套工具里最重要的一个，因为三件事会让人得出错误结论：

1. **跨机器时钟不可对齐。** 在两台机器上分别采流量和 RTT，再算相关性，得到的基本是噪声。
   所以必须同机。
2. **均值会骗人。** "平均 2.4 Mbps" 的链路照样能打出 16 Mbps 的突发，把队列顶满。
   要看 P90/max，不看平均。
3. **短窗口高估速率。** 150ms 的单点差值会被 TCP 突发填满网卡暂存区而虚高
   （实测出现过 25 Mbps，而链路上行只有 13 Mbps）。所以速率按 **~1 秒滑窗**重算，
   单点峰值只作为"缓冲突发"标注出来。

决定性的证据长这样——它比任何相关系数都有说服力：

```
  egress bucket        n   RTT median   max   >100ms
  low                 160          9     10   0/160
  medium               22         90    528   6/22
  high (busiest)        4        138    522   1/4
```

空闲时最大 10ms、一个都不超；一忙就 25% 超 100ms、最大 522ms。这是排队，不是丢包。

**它会给三种不同的结论，因为三种情况要分开处理**——别把它们混成一个"延迟高"：

| 看到什么 | 结论 | 下一步 |
|---|---|---|
| 空闲时也在尖峰 | 不是排队，整形没用 | 查 Wi-Fi 信号/干扰/2.4G↔5G 漫游、网线、路由器 |
| 只有忙时才尖峰 | 瓶颈处队列积压 | `cap` 在瓶颈之前整形 |
| 链路根本没忙起来 | 这次采样无效 | 有负载时重采，**不要硬下结论** |

采样间隔会被阻塞的 ping 拉长（实测 150ms 变成 170–570ms），所以**速率必须用时间戳算**，
不能按"每秒一次"直接数。

## top：谁在吃上行

```bash
latency top --peer awin --seconds 20
```

读 mihomo 的 `/connections`，多次采样算每个连接的**增量**，按 host 汇总上报速率。

一个必须避开的坑：`upload` 是**连接建立以来的累计值**。对早就存在的连接直接用它会
凭空造出几十 MB 的流量（实测算出 80 Mbps，而链路只有 13 Mbps），要用 `last - first`。

`process` 列在 Windows 上通常是空的——mihomo 不装 TUN 时拿不到进程名。要归因到进程，
就靠 `doctor` 里看目标进程的连接，或者用 host 名推断。

### 一个值得记住的负载特征

**LLM API 是极端上行型**：接口无状态，每轮都要把整个对话上下文重传一遍。实测单个
连接 **上传:下载 ≈ 96:1**，几个 agent 并发就能把家宽上行（13~15 Mbps）打满。
所以看到一台机器跑多个 coding agent，上行是第一个该怀疑的地方——但仍要用 `top` 实测，
不要猜。

## cap：在瓶颈之前整形

```bash
latency cap --peer awin status
latency cap --peer awin on 8      # Mbps，整机
latency cap --peer awin off
```

`on` 会建策略、注册开机任务，然后**采样本机出流量验证峰值是否真的被压住**，最后给结论。

限速值取**实测上行的 50%~70%**。太高仍会 bufferbloat，太低任务变慢。
先 `probe` 看峰值，再定值。

整形发生在发包之前，所以它**不增加延迟**，它消除的正是那几百毫秒排队延迟。

### Windows QoS 的四个坑（都实测过，别改）

1. **只有 `-PolicyStore ActiveStore` 真正执行。** 默认存储（Group Policy Machine）和
   `PersistentStore` 会**收下策略然后什么也不做**：实测 4 Mbps 的策略仍有 11.8 Mbps 突发。
   `PersistentStore` 在某些机器上还会直接报 `The network path was not found`。
2. **`-AppPathNameMatchCondition` 对限速无效。** 实测限 `curl.exe` 到 2 Mbps，实际仍跑
   11.4 Mbps。限速只能整机。要按进程限，得换别的机制。
3. **ActiveStore 不跨重启**，所以必须配一个开机计划任务重新应用（脚本自动做了）。
4. **读 ActiveStore 和 SYSTEM 任务需要管理员。** 非提权会话里 `Get-NetQosPolicy`
   返回 **0 条而不是报错**，`schtasks` 则说 `Access is denied`。所以**非提权的检查会把
   正常配置报成"未启用"**。验证特权设置时，验证本身也得有特权。

## prio：Wi-Fi 空口优先

```bash
latency prio --app Overwatch.exe on     # DSCP 46 (EF)，需要提权
latency prio status
latency prio off
```

效果要在两层分开看，别混为一谈：

- **Wi-Fi 空口：有效。** Windows 把 DSCP 映射到 802.11 用户优先级，EF → AC_VO。
- **WAN 队列：通常无效。** 家用路由器普遍不解析 DSCP。所以它**不能替代 `cap`**。

优先级是锦上添花，限速才对症。

## 提权

本机如果不在提权上下文（`whoami /groups` 里 `BUILTIN\Administrators` 标注
`Group used for deny only` 就是这种情况），`cap`/`prio` 会打印一条**可直接粘贴**的提权
命令并返回 2。UAC 弹窗在独立安全桌面上，**任何程序都无法替你点**——不要试图绕过，
把命令交给用户。

## 命令

| 命令 | 作用 |
|---|---|
| `doctor [--app X]` | 默认路由、隧道网卡、本地代理、目标进程连接去向 + 结论 |
| `hops --dest X [--count N] [--peer Y]` | 逐跳 RTT 分布，指出第一处尖峰 |
| `probe [--peer Y] [--target X] [--seconds N] [--interval MS]` | 同机同钟采出流量与 RTT，按负载分组报分布 |
| `top [--peer Y] [--seconds N] [--limit N]` | 按 host 报经 mihomo 的上行速率 |
| `cap on RATE\|off\|status [--peer Y] [--verify N]` | 上行限速（整机，ActiveStore，带自检） |
| `prio on\|off\|status --app X[,...] [--peer Y]` | DSCP 46 标记 |

`--peer` 用的是 `remote` skill 记住的目标名（`~/.remote/targets.json`）。没有记住的
目标时退化为直接 `ssh NAME`。

## 实现上几个不显然的决定

- **不解析 ping / tracert 的文本输出。** 它们随系统语言变化。用 .NET
  `NetworkInformation.Ping` 拿结构化结果，`ConvertTo-Json` 回来。
- **上行网卡靠"谁持有默认网关"来选。** 隧道和虚拟交换机会报 `Up` 且链路速率更高
  （`100 Gbps`），按速率排序会选错。
- **发给远端的是脚本文本，不是文件。** 本地 `powershell -File` 需要 UTF-8 BOM
  （PS 5.1 会把无 BOM 的 .ps1 当 ANSI 读坏中文），但经 `remote run --file` 发送时
  **不能带 BOM**——远端收到的是一个字面 U+FEFF，PowerShell 直接报
  `The term '﻿' is not recognized`。所以写临时文件要能选加不加 BOM。
- **PS 5.1 把单元素数组序列化成对象**，收到后要统一包成 list。
- **每个片段开头都要 `$ProgressPreference='SilentlyContinue'`。** 否则
  `Invoke-RestMethod`/`Get-NetAdapterStatistics` 的进度记录会把 CLIXML 写到 stderr，
  把 JSON 前面糊满乱码——看起来像"远端失败"，其实数据是好的。手敲一次性命令时同样要设。

## 限制

- **只有 Windows 被实测过。** Linux 的 `tc`/`HTB`、macOS 的 `pfctl` 都还没实现，
  `cap`/`prio` 在非 Windows 上会失败。
- **`cap` 是整机的**，会同时影响那台机器上所有出站流量。
- **DSCP 对 WAN 基本无效**，只在 Wi-Fi 空口和解析 DSCP 的路由器上有意义。
- 没有拥塞时 `hops`/`probe` 会诚实地报"没有尖峰"。那就换个窗口再测，不要编结论。
