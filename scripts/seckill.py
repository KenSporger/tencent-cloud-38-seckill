#!/usr/bin/env python3
"""
腾讯云 4核4G3M 轻量服务器秒杀 —— 抢购引擎

所有活动参数（activity_id / bussid / act_id / 商品类型 / 场次 / 地域）
都从活动页自动获取，无需手改任何 ID。

用法:
    python3 seckill.py                      # 自动选最近一场，默认三地域全开
    python3 seckill.py --next               # 同上
    python3 seckill.py --now                # 不等待，立即开抢（场次已开始/测试用）
    python3 seckill.py --dry-run            # 只体检：验证 Cookie、参数、校时，不下单
    python3 seckill.py --regions 广州,上海   # 只抢指定地域
    python3 seckill.py --session 2          # 选列表里的第 2 场
    python3 seckill.py --session "10-27 10:00"
    python3 seckill.py --advance-ms 1500 --interval-ms 100
    python3 seckill.py --max-seconds 90     # 兜底：最多抢 90 秒后自动停

结果会写入 ~/.tencent-cloud-38-seckill/last_run.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discover
import lib

REGION_LABELS = {"广州": 1, "上海": 4, "北京": 8, "1": 1, "4": 4, "8": 8}

CODE_HINT = {
    0: "抢到了",
    408: "频率过快",
    1100110: "账号处理中",
    -1: "未登录",
}


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


_last_tick = 0.0


def tick(msg: str, quiet: bool = False, min_gap: float = 0.05) -> None:
    """原地刷新的单行状态（不会滚屏）。"""
    global _last_tick
    if quiet:
        return
    now = time.time()
    if now - _last_tick < min_gap:
        return
    _last_tick = now
    print("\r" + msg + "            ", end="", flush=True)


def resolve_session(sessions: list[dict], want: str | None, offset: float) -> dict | None:
    """按用户选择或「最近的未结束场次」挑一场。"""
    now = time.time() + offset

    if want:
        want = want.strip()
        if want.isdigit():
            idx = int(want) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]
            return None
        for s in sessions:
            stamp = f"{s['date'][5:]} {s['start_time'][:5]}"  # 10-27 10:00
            if want in stamp or want in f"{s['date']} {s['start_time']}":
                return s
        return None

    # 默认：还没结束的最近一场；全结束了就取最后一场
    future = [s for s in sessions if (s.get("end_ts") or 0) > now]
    if future:
        return min(future, key=lambda s: s["start_ts"] or 0)
    return max(sessions, key=lambda s: s["end_ts"] or 0) if sessions else None


async def run(args) -> int:
    # ---------- 1. 准备参数 ----------
    log("[1/5] 读取活动参数（不需要登录）...")
    html = None
    page_url = args.url
    if not page_url:
        for url in discover.candidate_urls():
            try:
                h = discover.http_get(url)
            except Exception:
                continue
            if discover.parse_page(h, url):
                html, page_url = h, url
                break
    else:
        html = discover.http_get(page_url)

    if not html:
        print("[!] 找不到活动页，可能活动已结束。用 --url 指定当前活动页。")
        return 2

    info = discover.parse_page(html, page_url)
    if not info:
        print("[!] 活动页里没有 4核4G3M 的秒杀场次。")
        return 2

    goods = info["goods"]
    activity_id = info["activity_id"]
    sessions = info["sessions"]

    # 用页面自带的 nowTime 先粗校一次时钟
    page_offset = 0.0
    if info.get("server_now"):
        page_offset = info["server_now"] - info["local_now"]

    session = resolve_session(sessions, args.session, page_offset)
    if not session:
        print(f"[!] 没有匹配的场次。可选：")
        for i, s in enumerate(sessions, 1):
            print(f"      {i}) {s['date']} {s['start_time']}-{s['end_time']} [{s['status']}]")
        return 2

    log(f"      商品      : {goods['name']}")
    log(f"      SKU       : {' / '.join(goods.get('sku') or [])}")
    log(f"      场次      : {session['date']} {session['start_time']}-{session['end_time']} [{session['status']}]")
    log(f"      activity  : {activity_id}  bussid={session['bussid']}  act_id={session['act_id']}")

    # ---------- 2. Cookie ----------
    jar = lib.load_cookies()
    if not jar or not lib.pick_skey(jar):
        print("[!] 还没有可用的 Cookie。")
        print("    请先运行: python3 login.py   （会弹浏览器让你扫码登录一次）")
        return 3
    csrf = lib.compute_csrf(lib.pick_skey(jar))
    log(f"[2/5] Cookie 已载入  {lib.summarize_cookies(jar)}")

    # ---------- 3. 地域 ----------
    regions = []
    if args.regions:
        for name in args.regions.replace("，", ",").split(","):
            name = name.strip()
            if name in REGION_LABELS:
                regions.append(REGION_LABELS[name])
            elif name:
                print(f"[!] 不认识的地域: {name}（可选 广州/上海/北京）")
                return 2
    else:
        regions = [r["id"] for r in info["regions"]]
    regions = sorted(set(regions))
    name_map = {r["id"]: r["label"] for r in info["regions"]}
    log(f"[3/5] 目标地域: " + "、".join(name_map.get(r, str(r)) for r in regions))

    # ---------- 4. 连接与体检 ----------
    import aiohttp
    connector = aiohttp.TCPConnector(limit=600, limit_per_host=600, ssl=False)
    async with aiohttp.ClientSession(cookies=jar, connector=connector) as session_http:
        base_payload = lib.build_payload(activity_id, session["bussid"], session["act_id"],
                                         goods["goods_param"], regions[0], page_url)
        ok, msg = await lib.probe_auth(session_http, csrf, base_payload)
        if not ok:
            print(f"[!] Cookie 不可用：{msg}")
            print("    请重新运行: python3 login.py")
            return 3
        clock = await lib.calibrate_clock(session_http, csrf, activity_id)
        log(f"[4/5] 登录有效；{clock.describe()}")

        if args.dry_run:
            log("[5/5] 体检")
            log("      鉴权探针返回: " + msg)
            log("\n[✓] 参数、Cookie、网络都通，可以正式开抢。")
            log("    （--dry-run 不下单；正式开抢直接跑 python3 seckill.py）")
            return 0

        # ---------- 5. 等待并开抢 ----------
        start_ts = session.get("start_ts") or 0
        trigger_ts = start_ts - args.advance_ms / 1000.0
        now_ts = clock.now_ts()

        if not args.now and trigger_ts > now_ts:
            log(f"[5/5] 目标时间: {datetime.fromtimestamp(start_ts).strftime('%m-%d %H:%M:%S')}"
                f"  (提前 {args.advance_ms}ms 触发)")
            waited = trigger_ts - now_ts
            if waited > 120:
                log(f"      距离目标还有 {waited / 3600:.1f} 小时，可以先去忙别的（别合盖、别断网）")

            # 第一段：粗等到触发前 90 秒
            while True:
                left = trigger_ts - clock.now_ts()
                if left <= 90:
                    break
                tick(f"[*] 倒计时 {int(left // 3600)}h{int(left % 3600 // 60)}m"
                     f"{int(left % 60)}s ...", quiet=args.quiet, min_gap=0.5)
                await asyncio.sleep(min(5.0, max(0.2, left - 90)))

            # 临阵重新校时：等了几小时的话，开机时钟可能已经漂了
            if waited > 120:
                clock = await lib.calibrate_clock(session_http, csrf, activity_id)
                tick("", quiet=args.quiet)
                log(f"[*] 临阵校时: {clock.describe()}")

            # 第二段：精等到触发点
            while True:
                left = trigger_ts - clock.now_ts()
                if left <= 0:
                    break
                tick(f"[*] 倒计时 {left:6.3f}s ...", quiet=args.quiet)
                await asyncio.sleep(0.005 if left < 10 else 0.2)
            tick("", quiet=args.quiet)
            log("")
        else:
            log(f"[5/5] 立即开抢（不再等待）")

        trigger_at = clock.now().strftime("%H:%M:%S.%f")[:-3]
        log(f"[!] 开始派发 @ {trigger_at}  间隔 {args.interval_ms}ms  地域 {len(regions)} 个")
        log("=" * 58)

        stop = asyncio.Event()
        stats: dict = {}
        success: dict = {}
        started = time.time()

        async def fire(region_id: int, seq: int):
            if stop.is_set():
                return
            payload = lib.build_payload(activity_id, session["bussid"], session["act_id"],
                                    goods["goods_param"], region_id, page_url)
            label = name_map.get(region_id, str(region_id))
            try:
                async with session_http.post(
                    lib.API_BUY, json=payload,
                    headers={"X-Csrf-Token": csrf},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    data = await resp.json(content_type=None)
                code = data.get("code")
                stats[code] = stats.get(code, 0) + 1
                if code == 0:
                    success.update({"region": label, "seq": seq, "data": data,
                                    "at": clock.now().strftime("%H:%M:%S.%f")[:-3]})
                    stop.set()
            except Exception:
                stats["_err"] = stats.get("_err", 0) + 1

        max_stop_ts = started + args.max_seconds if args.max_seconds else 0
        stop_candidates = []
        if max_stop_ts:
            stop_candidates.append(max_stop_ts)
        if session.get("end_ts"):
            stop_candidates.append(session["end_ts"] + 60)
        hard_stop_ts = min(stop_candidates) if stop_candidates else 0

        seq = 0
        last_report = time.time()
        while not stop.is_set():
            if hard_stop_ts and time.time() > hard_stop_ts:
                if max_stop_ts and time.time() > max_stop_ts:
                    log(f"\n[*] 已达运行上限 {args.max_seconds}s，停止派发。")
                else:
                    log("\n[*] 本场已结束，自动停止派发。")
                break
            seq += 1
            for rid in regions:
                asyncio.create_task(fire(rid, seq))

            now = time.time()
            if not args.quiet and now - last_report >= 2:
                last_report = now
                brief = "  ".join(
                    f"{CODE_HINT.get(k, k)}×{v}" for k, v in
                    sorted(stats.items(), key=lambda x: -x[1])[:5]
                )
                log(f"[{datetime.now().strftime('%H:%M:%S')}] 已发 {seq} 轮 | {brief}")
            await asyncio.sleep(args.interval_ms / 1000.0)

        log("=" * 58)
        if success:
            log(f"\n🎉 抢购成功！地域：{success['region']}   时间：{success['at']}")
            log("   " + json.dumps(success["data"], ensure_ascii=False))
            log("\n⚠️ 请立刻去付款（通常限时 1 小时，超时订单自动取消）：")
            log("    https://console.cloud.tencent.com/expense/deal")
            lib.ensure_home()
            with open(lib.LOG_FILE, "w", encoding="utf-8") as f:
                json.dump({"ok": True, **success, "stats": stats}, f,
                          ensure_ascii=False, indent=2)
            return 0

        log("\n[!] 本轮没抢到。")
        log(f"    统计: {stats}")
        if stats.get(1100110, 0) > seq * len(regions) * 0.5:
            log("    提示: 大量「正在执行其他操作」= 账号已有请求在排队，通常说明这个号本轮已抢过。")
        if stats.get(408, 0) > seq * len(regions) * 0.5:
            log("    提示: 大量「频率过快」= 被限流了，把 --interval-ms 调大到 150~200 再试。")
        lib.ensure_home()
        with open(lib.LOG_FILE, "w", encoding="utf-8") as f:
            json.dump({"ok": False, "stats": stats}, f, ensure_ascii=False, indent=2)
        return 4


def main() -> int:
    ap = argparse.ArgumentParser(description="腾讯云 4核4G3M 秒杀")
    ap.add_argument("--url", help="活动页 URL（默认自动找）")
    ap.add_argument("--session", help="场次序号或 '10-27 10:00'")
    ap.add_argument("--next", action="store_true", help="选最近一场（默认行为）")
    ap.add_argument("--now", action="store_true", help="不等待，立即开抢")
    ap.add_argument("--dry-run", action="store_true", help="只体检不下单")
    ap.add_argument("--regions", help="广州,上海,北京（默认全选）")
    ap.add_argument("--advance-ms", type=int, default=1500, help="提前多少毫秒触发")
    ap.add_argument("--interval-ms", type=int, default=100, help="派发间隔毫秒")
    ap.add_argument("--max-seconds", type=int, default=0, help="最多抢多少秒后自动停")
    ap.add_argument("--quiet", action="store_true", help="少打日志")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[*] 已手动停止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
