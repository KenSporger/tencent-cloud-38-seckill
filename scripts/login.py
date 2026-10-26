#!/usr/bin/env python3
"""
腾讯云秒杀 —— 登录并抓取 Cookie（含 HttpOnly 的 skey）

用户只需要在弹出的浏览器里扫码 / 输密码登录一次，剩下的交给脚本。
登录态保存在浏览器配置目录里，下次直接复用，不用反复登录。

用法:
    python3 login.py            # 打开浏览器登录，成功后自动保存 Cookie
    python3 login.py --check    # 只检查已保存的 Cookie 还能不能用
    python3 login.py --paste    # 没装浏览器驱动时的兜底：手动粘贴 Cookie

退出码: 0 成功 / 1 失败
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib

LOGIN_URL = "https://cloud.tencent.com/login"
CONSOLE_URL = "https://console.cloud.tencent.com/"
ACTIVITY_FALLBACK = "https://cloud.tencent.com/act/pro/featured-202607"


def _header(msg: str) -> None:
    print(f"\n{msg}", flush=True)


# ------------------------------------------------------------------ 模式一：浏览器登录

async def browser_login(timeout_sec: int = 300) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[!] 没装 playwright，无法自动登录。")
        print("    请在项目目录执行: pip install playwright")
        print("    或改用: python3 login.py --paste")
        return 1

    lib.ensure_home()
    os.makedirs(lib.PROFILE_DIR, exist_ok=True)

    _header("=" * 56)
    print("  即将弹出一个浏览器窗口 ——")
    print("  请在里面用【微信扫码】或【账号密码】登录腾讯云。")
    print("  登录成功后窗口会自动关闭，你什么都不用做。")
    print("  （首次启动可能要等几秒，别急着关窗口）")
    print("=" * 56, flush=True)

    async with async_playwright() as p:
        ctx = None
        last_err = None
        # 优先用系统已装的 Chrome，省掉下载 130MB 浏览器内核
        for kwargs in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
            try:
                ctx = await p.chromium.launch_persistent_context(
                    lib.PROFILE_DIR,
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1200, "height": 820},
                    **kwargs,
                )
                break
            except Exception as e:
                last_err = e
                ctx = None
        if ctx is None:
            print(f"[!] 浏览器启动失败: {last_err}")
            print("    请先执行: playwright install chromium")
            return 1

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                await page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=30000)

            deadline = time.time() + timeout_sec
            found = None
            dots = 0
            while time.time() < deadline:
                cookies = await ctx.cookies()
                jar = {c["name"]: c["value"] for c in cookies
                       if "tencent.com" in (c.get("domain") or "")}
                if lib.pick_skey(jar):
                    found = jar
                    break
                if ctx.pages:
                    for pg in ctx.pages:
                        if "login" not in pg.url and "tencent.com" in pg.url:
                            try:
                                await pg.goto(CONSOLE_URL, wait_until="domcontentloaded",
                                              timeout=15000)
                            except Exception:
                                pass
                dots += 1
                print(f"\r[*] 等待登录... {int(deadline - time.time())}s 剩余 "
                      f"{'.' * (dots % 4)}   ", end="", flush=True)
                await asyncio.sleep(1.5)

            print()
            if not found:
                print("[!] 超时未检测到登录状态。")
                print("    可以重新运行一次；如果一直失败，改用 python3 login.py --paste")
                return 1

            lib.save_cookies(found, source="browser")
            print(f"[+] 登录成功并已保存 Cookie")
            print(f"    {lib.summarize_cookies(found)}")
            print(f"[+] 保存在: {lib.COOKIE_FILE}（仅本人可读）")
            return 0
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


# ------------------------------------------------------------------ 模式二：手动粘贴

def paste_login() -> int:
    _header("=" * 56)
    print("  手动粘贴模式")
    print("  1) 浏览器打开 https://cloud.tencent.com 并登录")
    print("  2) 按 F12 → Network(网络) → 刷新页面")
    print("  3) 点任意一个请求 → Headers → 找到 Cookie: 那一行")
    print("  4) 右键「Copy value」后粘贴到下面，回车")
    print("=" * 56, flush=True)
    try:
        raw = input("\nCookie > ").strip()
    except (EOFError, KeyboardInterrupt):
        return 1
    if not raw:
        print("[!] 没输入内容")
        return 1
    jar = lib.cookie_header_to_dict(raw)
    if not lib.pick_skey(jar):
        print("[!] 这段 Cookie 里没有 skey —— 说明不是登录状态，或者复制的是错的请求。")
        print("    换一个 api.cloud.tencent.com 或 console.cloud.tencent.com 的请求再试。")
        return 1
    lib.save_cookies(jar, source="paste")
    print(f"[+] 已保存: {lib.summarize_cookies(jar)}")
    return 0


# ------------------------------------------------------------------ 模式三：检查

async def check_only() -> int:
    jar = lib.load_cookies()
    if not jar:
        print("[!] 还没有保存过 Cookie，先运行 python3 login.py")
        return 1
    if not lib.pick_skey(jar):
        print("[!] 保存的 Cookie 里没有 skey，需要重新登录")
        return 1

    import aiohttp
    import discover

    csrf = lib.compute_csrf(lib.pick_skey(jar))
    print(f"[*] Cookie: {lib.summarize_cookies(jar)}")

    info, page_url = None, None
    for url in discover.candidate_urls():
        try:
            html = discover.http_get(url)
        except Exception:
            continue
        info = discover.parse_page(html, url)
        if info:
            page_url = url
            break
    if not info:
        print("[!] 找不到当前活动页，无法验证登录态（活动可能已结束）。")
        return 1

    s = info["sessions"][0]
    payload = lib.build_payload(info["activity_id"], s["bussid"], s["act_id"],
                               info["goods"]["goods_param"],
                               info["regions"][0]["id"], page_url)

    async with aiohttp.ClientSession(cookies=jar) as session:
        ok, msg = await lib.probe_auth(session, csrf, payload)

    if ok:
        print(f"[+] 状态正常 —— 登录态有效")
        return 0
    print(f"[!] 状态异常 —— {msg}")
    print("    请重新运行: python3 login.py")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="登录腾讯云并保存 Cookie")
    ap.add_argument("--check", action="store_true", help="只检查现有 Cookie")
    ap.add_argument("--paste", action="store_true", help="手动粘贴 Cookie")
    ap.add_argument("--timeout", type=int, default=300, help="登录等待秒数")
    args = ap.parse_args()

    if args.check:
        return asyncio.run(check_only())
    if args.paste:
        return paste_login()
    return asyncio.run(browser_login(args.timeout))


if __name__ == "__main__":
    sys.exit(main())
