---
name: tencent-cloud-38-seckill
description: 帮用户抢腾讯云 38 元/年的轻量应用服务器秒杀（4核4G3M，每天 10:00-12:00、15:00-18:00 两场）。全程由你代劳——自动找到当前活动页、自动解析 activity_id/bussid/act_id/商品类型/场次/地域（这些每天变，用户永远不用填），弹出浏览器让用户扫码登录一次即可自动拿到含 HttpOnly 的 skey Cookie，然后校时、倒计时、开抢、报结果、催付款。触发词：腾讯云秒杀、抢服务器、38 元服务器、38块服务器、轻量应用服务器、4核4G、抢到了吗、cookie 怎么填、BUSSID/activity_id 怎么找、抢不到。
license: MIT
agent_created: true
metadata:
  version: 1.0.0
  author: workbuddy
  tags: [tencent-cloud, seckill, lighthouse, 38-yuan, playwright, aiohttp]
---

# 腾讯云 38 元服务器秒杀（4核4G3M 轻量应用服务器）

> 面向人类读者的介绍、痛点对照、原理图和 FAQ 见 `README.md`；
> 本文是给 AI 助手看的操作手册，不要把它当 README 展示给用户。

用户是**完全不懂技术**的人。他要做的只有两件事：**说一句"帮我抢"**，和**扫一次码**。
其余全部由你代劳。不要说"请修改第 22 行的 BUSSID"这种话——用户看不懂也不需要懂。

## 铁律

1. **绝对不让用户手填活动 ID。** `activity_id` / `bussid` / `act_id` / 商品类型 / 场次 / 地域
   每天都会变，全部由 `discover.py` 从活动页自动解析。旧版 README 教你改代码里那几个数字的做法已经废弃。
2. **绝对不让用户按 F12 找 Cookie。** 用 `login.py` 弹浏览器扫码登录，从浏览器底层读 Cookie
   （包括 HttpOnly 的 `skey`，所以 F12 里根本看不到它——这不是用户的问题）。
   只有当 `login.py` 连续失败时才降级到 `login.py --paste`。
3. **先体检再开抢。** 任何一次正式抢购前，先跑 `seckill.py --dry-run`。
   体检不过就别等场次，先把问题解决掉。

## 工作目录与运行环境

| 项 | 路径 |
|---|---|
| 脚本目录 | `~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/` |
| 运行环境 | `~/.tencent-cloud-38-seckill/venv/bin/python`（`bootstrap.py` 自动创建） |
| Cookie（600 权限） | `~/.tencent-cloud-38-seckill/cookies.json` |
| 浏览器登录态 | `~/.tencent-cloud-38-seckill/browser_profile/` |
| 上次结果 | `~/.tencent-cloud-38-seckill/last_run.log` |

### 第 0 步：环境准备（每条会话先跑，幂等，1 秒内返回）

```bash
cd ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts && python3 bootstrap.py
```

返回 `[+] 依赖已就绪` 说明可以直接用。返回非 0 就把报错原样告诉用户。

之后所有脚本一律用 **venv 的 python** 执行，不要用系统 `python3`：

```bash
PY=~/.tencent-cloud-38-seckill/venv/bin/python
```

## 标准流程

### 步骤 1 — 问清楚要抢哪一场、哪些地域（一次问完，别来回问）

同时用 `discover.py` 把当前的场次抓出来给用户看：

```bash
~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/discover.py
```

输出里 `[+] 场次` 就是可选列表（通常是每天 10:00-12:00、15:00-18:00 两场）。
把**还没结束的最近一场**作为推荐项告诉用户，让他在「场次 + 地域（广州/上海/北京，可多选）」
两个问题上一次性确认。用户说"随便/你定"就默认：最近一场 + 三个地域全选。

### 步骤 2 — 登录（只在需要时做）

先看有没有现成可用的 Cookie：

```bash
~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/login.py --check
```

- 退出码 0 → 登录态还有效，直接跳到步骤 3。
- 退出码非 0 → 需要登录：

```bash
~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/login.py
```

这条命令会**弹出一个浏览器窗口**。执行前必须告诉用户（用人话）：

> 马上会弹出一个浏览器，请用微信扫码登录你的腾讯云账号。如果扫码后要填短信验证码，
> 慢慢填，脚本不会动浏览器。登录完成后窗口会自己关掉（还会再等 2~5 秒做确认）。

登录成功会打印 `[+] 登录真实有效（后台已确认），Cookie 已保存`。

⚠️ 这里有两条容易踩的坑，脚本已经处理，别再改回去：
1. **「看到 skey」≠「登录成功」**。`cloud.tencent.com/login` 页面本身会下发一张临时
   skey（防 CSRF），未登录也有。所以必须 `skey + uin` 同时存在，再用 do-goods
   探针确认，才允许保存。旧版看到 skey 就保存，抓到的是废 Cookie，害用户白等一场。
2. **确认登录态不能开标签页访问控制台**。未登录时控制台会重定向回扫码页，
   每 3 秒一次就变成「浏览器反复重开扫码页」，把用户正在输入的验证码顶掉。
   现在等待期间只被动观察已有标签页 URL + Cookie，确认走后台 HTTP 探针（preview=1）。超时（默认 300 秒）就再跑一次；
连续两次失败才降级：

```bash
~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/login.py --paste
```

`--paste` 是给用户自己粘贴 Cookie 的兜底方案，**仅在自动登录失败时用**，并且要把
F12 的步骤一步步讲清楚（用户看不到 skey 时提醒他换一个 `api.cloud.tencent.com` 的请求）。

### 步骤 3 — 体检（必须做，不能跳）

```bash
~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/seckill.py --dry-run
```

它会依次验证：活动页能解析 → 场次存在 → Cookie 登录态有效 → 校时成功。
看到 `[✓] 参数、Cookie、网络都通` 才算过。任何 `[!]` 都按下面的异常表处理。

### 步骤 4 — 正式开抢

```bash
~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/seckill.py
```

可选参数：

| 参数 | 用途 |
|---|---|
| `--regions 广州,上海` | 只抢指定地域，默认全选 |
| `--session 2` 或 `--session "10-27 10:00"` | 指定场次，默认最近一场 |
| `--now` | 不等待立即开抢（场次已开始或测试用） |
| `--interval-ms 150` | 被限流时调大 |
| `--advance-ms 1500` | 提前多少毫秒触发 |
| `--max-seconds 120` | 最多抢多少秒（默认本场结束自动停） |

**注意超时**：脚本会一直等到场次开始，如果离场次还有几小时，这条命令会长时间挂着。
用时较长的命令请放到后台跑（`run_in_background`），并告诉用户：

> 已经在等了，XX:XX 开抢，到点我会把结果告诉你。这个窗口你可以干别的，但别关机、别断网。

### 隔夜等第二天场次（macOS）

距离开抢还有几小时时，**必须提醒两件事**，否则大概率白等：

1. **别合盖休眠**。macOS 合盖后进程会挂起，醒来时已经错过了那一秒。
   如果有电源，建议用 `caffeinate` 包一层，防止空闲休眠：

   ```bash
   caffeinate -i ~/.tencent-cloud-38-seckill/venv/bin/python ~/.workbuddy/skills/tencent-cloud-38-seckill/scripts/seckill.py
   ```

2. 脚本会在触发前 90 秒**自动重新校时**（等了几小时后开机时钟可能已经漂了），
   日志里会多一行 `[*] 临阵校时: 本地时钟偏移 ±XXms`，看到这行说明它还活着。
   如果没看到这行就说明进程已经死了，要重新拉起。

推荐节奏：**今晚只做登录 + 体检**（步骤 2、3），**明早提前 5-10 分钟再启动正式抢购**，
比从前一晚挂到早上更稳。

### 步骤 5 — 报结果

- 退出码 0 + `🎉 抢购成功`：立刻把 **地域** 和 **付款链接** 给用户，
  并强调「**1 小时内付款，超时自动取消**」：https://console.cloud.tencent.com/expense/deal
- 退出码 4：没抢到。把统计里占比最高的那条原因翻译成人话（见异常表），给出下一步建议。

## 退出码对照

| 码 | 含义 | 你该做什么 |
|---|---|---|
| 0 | 成功 / 体检通过 | 报喜 + 催付款 |
| 1 | 环境或登录准备失败 | 把报错给用户，按异常表处理 |
| 2 | 找不到活动页或没有该商品 | 活动可能换页/下架，用 `--url` 指定用户给的活动页 |
| 3 | Cookie 失效 | 回步骤 2 重新登录 |
| 4 | 没抢到 | 看统计，给建议 |
| 130 | 用户中断 | 已停止，无需处理 |

## 异常 → 人话翻译表

| 现象 | 真实原因 | 告诉用户 / 怎么处理 |
|---|---|---|
| `NOT-LOGINED` / `登陆态校验失败` | Cookie 过期 | "登录状态过期了，重新扫一次码" → 跑 `login.py` |
| `1100110 正在执行其他操作` | 该账号有请求排队，通常已抢过 | "这个账号本轮可能已经抢到了，去控制台看看待付款订单" |
| `408 系统访问频率过快` | 被限流 | 加大间隔：`--interval-ms 180`，并用 `--regions` 减少同时抢的地域 |
| 找不到活动页 | 活动页 URL 换名了 | 让用户从浏览器复制当前活动页链接，用 `--url` 传进去 |
| 活动页里没有 4核4G3M | 该商品下架/换 SKU | 如实说，别硬编参数；可让用户发活动页截图确认商品名 |
| 倒计时后一直 `NOT-LOGINED` 刷屏 | 体检后被挤下线 | 停下重登，别让它空转一整场 |
| 浏览器没弹出来 | 被其他窗口挡住 / 无图形界面 | 让用户看 Dock 或 Cmd+Tab；macOS 首次运行可能被安全提示拦 |

## 安全与体面

- **只让用户扫码，永远不要索取账号密码**；不要把 Cookie 内容打印到对话里
  （脚本已经做了脱敏：`skey=***后四位`）。
- Cookie 只写到本机 `~/.tencent-cloud-38-seckill/cookies.json`（600 权限），**不要上传、不要提交到 git**。
- 如果用户要分发这套脚本给别人：让他把整个 skill 目录（或 `~/.tencent-cloud-38-seckill/` 之外的部分）
  发出去，**绝不要连 `cookies.json` / `browser_profile/` 一起发**。
- 秒杀是高频请求，只用于用户本人自用；如果用户想拿去批量代抢或对外售卖，提醒他这可能违反平台规则。

## 脚本清单（给你的速查，不用读源码）

| 文件 | 职责 |
|---|---|
| `bootstrap.py` | 建 venv、装依赖。幂等 |
| `discover.py` | 抓活动页 → 解析全部参数。**不需要登录** |
| `login.py` | 弹浏览器扫码 → 导出 Cookie；`--check` 只检查；`--paste` 手动兜底 |
| `seckill.py` | 体检（`--dry-run`）+ 校时 + 倒计时 + 抢 |
| `lib.py` | 共用：CSRF 计算、Cookie 读写、校时、鉴权探针、请求体组装 |
