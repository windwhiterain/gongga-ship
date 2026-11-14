---
name: image
description: 让模型看一张图（截图、图表、设计稿、照片）。调用本 skill 的 `show(paths, check=false)` 打印 `@image(<路径>)`，harness 会自动上传并作为图片交给模型——不要用 exec + base64 自己拼。当你要断言"这个界面/图表/图里是什么"、或需要读一张磁盘上的图片时用它。
compatibility: 需要 Python 3
---

# 看图

harness 会扫描每个工具的输出，认出 `@image(<路径>)` 就把它上传到 provider 的
Files API，然后作为 `input_image` 交给模型。**图是真的进到模型眼里，不是一串 base64。**

**必须写成 `@image(<路径>)`。** 裸的 `@C:\...\shot.png` 服务端不认识、**静默丢弃**——
那条「`@` 开头加图片后缀」的旧规则在 2026-10-31 随 Meta 一起删掉了。打印裸 `@路径` 的
脚本都会白跑一趟：不报错，只是图永远不到。

本 skill 是一个包：一次调用就是**函数名 + 一份 JSON 参数**。函数只有一个：

```
show(paths, check=False)     # paths 是路径列表；一张图一行 `@image(...)`，check 只报它是什么、多大
```

```bash
# 一次调用怎么拼、退出码什么意思，是 `@skill(skill)` 的事；这里只写 `show` 自己的参数。
# 会话里更短：`skill` 工具的 `cmd` 就是同一个对象，`session` 由工具填。
R() { gongga-cli "{\"name\":\"run_skill\",\"args\":{\"id\":\"harness/image\",\"cmd\":{\"name\":\"$1\",\"args\":$2},\"session\":\"$GONGGA_SESSION\"}}"; }
R show '{"paths":["/path/to/shot.png"]}'              # 打印 `@image(...)` 引用，harness 接手
R show '{"paths":["shot.png"],"check":true}'         # 只看它是什么、多大
```

`paths` 里的相对路径按调用会话的目录解析。

`show` 会打印形如 `@image(C:\...\shot.png)` 的一行，一行一张图。括号里就是路径本身，
有空格也不用转义——分隔符是 `)`，不是空白。

## 支持的格式

`png` `jpg` `jpeg` `gif` `webp` `bmp`。扩展名优先，文件头兜底（`.png` 但内容不是
PNG 的，会按文件头判断）。

不在这个列表里的（比如 `tiff`、`svg`、`heic`）`show` 会拒绝并说明——先转格式。
`svg` 尤其要注意：它是文本不是位图，先 `convert` 成 png。

## 为什么不用 base64

模型看不懂 base64。`read` 一坨 `iVBORw0KGgo...` 只会烧上下文，模型还得去解码——
而它解不了。走 Files API 之后：

- 请求里带的是 `file_id`，不是几 MB 的字节；
- 同一个文件只上传一次，重放 transcript 时复用那个 id；
- 图片按路径记在 transcript 里，文件删掉会降级成一句话，不会让回合一整轮失败。

## 常见情况

| 情况 | 结果 |
|---|---|
| 文件不存在 / 读不了 | `show` 报错，不打印引用（不会给你一个假引用） |
| 不是图片 | `show` 报错并列出支持的格式 |
| 模型不支持图片（roster 的 `modalities` 没有 `image`） | 引用被跳过，服务端日志里有 warn；`show` 本身不知道这件事 |
| 上传失败（网络、鉴权） | 引用保持为文本，回合继续；日志里有 warn |

最后两行意味着：**打印了引用 ≠ 模型一定看到了**。要确认，就在下一轮问它图里是什么——
答得出来才是真看到了。

## 一次看多张

```bash
R show '{"paths":["a.png","b.png","c.png"]}'
```bash
"$SKILL_DIR/show" a.png b.png c.png
```

三行引用，三张图都附上。别一次给几十张：每张都是一次上传，而且请求体会变大。

## 自己生成图再看的完整流程

```bash
# 1. 用任何方式生成一张图（这里是一个纯色方块）
python -c "..." > /tmp/x.png     # 或者截图、导出图表

# 2. 让它进到模型眼里
R show '{"paths":["/tmp/x.png"]}'
```

第 2 步的输出会作为这条 exec 结果的一部分回到你面前，同时图被附上。
