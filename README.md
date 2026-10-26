# tencent-cloud-38-seckill

> 抢腾讯云 **38 元/年轻量应用服务器**（4核4G3M）的自动化工具。
> 活动 ID 每天变、Cookie 藏在 F12 里看不到 —— 这些活儿全交给它，你只需要扫一次码。

---

## 这玩意儿解决什么问题

官方秒杀页每天 10:00–12:00、15:00–18:00 两场，38 元一年的 4核4G3M 轻量服务器，
手点基本抢不到。网上流传的抢购脚本大多要你自己改一堆参数：

| 你要做的事 | 传统脚本 | 本项目 |
|---|---|---|
| `BUSSID` / `activity_id` / `act_id` / 商品 `type` | 每天手动改，得去 F12 里搜 `end_date` 才找得到 | **从活动页自动解析** |
| Cookie | 按 F12 → Network → 找请求 → 复制整条 → 手动粘贴 | **弹浏览器扫码，自动读取** |
| 场次时间、地域 ID | 写死在代码里 | **自动解析，可命令行指定** |
| 校准自己电脑和服务器的时间差 | 自己 `ntpdate`，或者干脆不校 | **用接口毫秒级时间校准** |

最坑的一点：登录凭证 `skey` 是 `HttpOnly` 的，**F12 里根本看不到**。
不知道这一点的人，会在 Application → Cookies 里找一个不存在的值，然后怀疑人生。
本项目通过浏览器协议层读取 Cookie，绕开这个限制。

## 它能自动搞定什么

- **自动找到当前活动页**。活动页 URL 里的月份 slug 是滞后的，代码内置候选列表逐个探测，
  命中含目标商品的那个页面。
- **自动解析全部活动参数**：`activity_id`、`bussid`、`act_id`、商品类型 `bundle_budget_mc_lg4_01`、
  镜像 `imageId`、场次起止时间、地域列表（广州/上海/北京）。
- **扫码登录**。Playwright 拉起系统 Chrome（复用你已装的浏览器，**不额外下载 130MB 内核**），
  登录成功后从上下文读取全部 Cookie 并落盘（`600` 权限）。
- **毫秒级校时**。取接口响应里的 `nowTime` 毫秒时间戳，配合 RTT/2 估算偏移，
  精度从 HTTP `Date` 头那套 ±500ms 提升到 ±(RTT/2)。
- **倒计时 + 提前量触发**。默认提前 1500ms 开始派发，到点后按间隔并发打多个地域。
- **到点自动停**。本场结束即停止，不会空转一整天。
- **结果落盘**。每次运行写入 `last_run.log`，含各轮响应统计。

## 快速开始

### 方式 A：作为 AI 助手技能（推荐，零门槛）

```bash
git clone https://github.com/<you>/tencent-cloud-38-seckill.git \
  ~/.workbuddy/skills/tencent-cloud-38-seckill
```

然后在 AI 助手里说一句「**帮我抢腾讯云那个 38 块的服务器**」。
它会问你抢哪一场、哪些地域，然后弹浏览器让你扫码，剩下的你不用管。

`SKILL.md` 是给 AI 看的操作手册，里面写了完整流程、退出码含义和异常翻译表。

### 方式 B：纯命令行

```bash
git clone https://github.com/<you>/tencent-cloud-38-seckill.git
cd tencent-cloud-38-seckill

python3 scripts/bootstrap.py        # 建 venv + 装 aiohttp / playwright，幂等
PY=~/.tencent-cloud-38-seckill/venv/bin/python

$PY scripts/discover.py             # 先看看今天有哪些场次（不需要登录）
$PY scripts/login.py                # 弹浏览器扫码，保存 Cookie
$PY scripts/seckill.py --dry-run    # 体检：参数 / Cookie / 校时全通
$PY scripts/seckill.py              # 正式开抢
```

## 用法

```bash
$PY scripts/seckill.py [选项]
```

| 选项 | 说明 |
|---|---|
| `--regions 广州,上海` | 只抢指定地域，默认三个全开 |
| `--session 2` | 选场次列表里的第 2 场 |
| `--session "10-27 10:00"` | 用日期时间指定场次 |
| `--now` | 不等待，立即开抢（场次已开始或调试用） |
| `--dry-run` | 只体检，不下单 |
| `--advance-ms 1500` | 提前多少毫秒触发 |
| `--interval-ms 100` | 重试间隔，被限流时调大 |
| `--max-seconds 90` | 最多抢多少秒，默认到本场结束 |
| `--url <活动页>` | 自动找不到活动页时手动指定 |

**隔夜等第二天场次？** 别让电脑合盖休眠，进程会被挂起、正好错过那一秒：

```bash
caffeinate -i $PY scripts/seckill.py
```

脚本会在触发前 90 秒自动重新校时，日志里出现 `[*] 临阵校时` 说明它还活着。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 抢到了 / 体检通过 |
| `1` | 环境或登录准备失败 |
| `2` | 找不到活动页，或页面上没这个商品 |
| `3` | Cookie 失效，需要重新登录 |
| `4` | 没抢到 |
| `130` | 用户中断 |

## 工作原理

```
活动页 HTML
   └─ 内嵌 JSON: twoDaySeckillState.flashResult[date][*]
        ├─ id            → bussid（每场一个）
        ├─ sales[*].act_id → act_id
        ├─ end_time / startTime / nowTime → 场次与校时基准
        └─ $goods.{type,image_id,goods_param} → 下单商品参数
                                     ↓
                          POST /dianshi/do-goods
                          X-Csrf-Token: md5("skey=" + skey)
                                     ↓
                              下单结果
```

CSRF token 由 `skey` 计算得出，这是 Cookie 无法绕过的原因，也是必须登录的原因。
`query-seckill-by-date` 是公开接口（未登录也正常返回），所以**不能**用它判断登录态 ——
本项目用 `do-goods` + `preview=1` 做鉴权探针。

## 常见问题

**Q: 报 `NOT-LOGINED` / `登陆态校验失败`？**
Cookie 过期了，重新跑 `login.py` 扫码。

**Q: 报 `1100110 正在执行其他操作`？**
这个账号有请求排队，通常意味着本轮已经抢到了，去[待付款订单](https://console.cloud.tencent.com/expense/deal)看看。

**Q: 报 `408 系统访问频率过快`？**
被限流了。加大 `--interval-ms`，并用 `--regions` 减少同时抢的地域数量。

**Q: 找不到活动页？**
大促活动会换 slug。从浏览器复制当前活动页链接，用 `--url` 传进来。

**Q: 浏览器没弹出来？**
可能被其他窗口挡住（看 Dock）、或者首次运行被 macOS 安全提示拦了。

**Q: 一定要装 Playwright 吗？**
只想手动粘 Cookie 的话，`login.py --paste` 可以兜底，但仍需要 aiohttp。
Playwright 只在扫码登录时用到，且复用系统 Chrome，不下载浏览器内核。

## 免责声明

- 仅供**个人自用**，用于抢自己账号名下的商品。
- 高频请求可能触犯平台规则，**请勿用于批量代抢、对外售卖或任何商业用途**。
- 项目不保证抢购成功，抢购结果取决于商品库存、账号状态和网络条件。
- Cookie 与登录态只保存在本机（`~/.tencent-cloud-38-seckill/`），请勿提交到仓库或分享给他人。
  `.gitignore` 已排除相关文件。
- 使用本工具产生的一切后果由使用者自行承担。

## License

[MIT](LICENSE)
