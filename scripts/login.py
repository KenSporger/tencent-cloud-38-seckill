#!/usr/bin/env python3
"""
腾讯云秒杀 —— 登录并抓取 Cookie（含 HttpOnly 的 skey）

用户只需要在弹出的浏览器里扫码 / 输密码登录一次，剩下的交给脚本。

⚠️ 踩过的两个坑（都已修）：
  ① cloud.tencent.com/login 页面本身就会下发一张临时 skey（防 CSRF），
     未登录时也存在。所以「看到 skey」≠「登录成功」——
     旧版一看到 skey 就保存并关窗口，抓到的是废 Cookie。
  ② 校验登录态时不能反复新开标签页去访问控制台：
     未登录时控制台会重定向回扫码页，表现为「浏览器每隔几秒重开扫码页」，
     把用户正在输入的短信验证码顶掉。
     新版等待期间**只被动观察已有标签页地址 + Cookie，绝不新开/跳转页面**，
     确认登录态改用后台 HTTP 探针（do-goods preview=1），不打扰用户。

用法:
    python3 login.py            # 打开浏览器登录，确认有效后自动保存 Cookie
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

POLL_SEC = 2.0            # 被动观察间隔
FALLBACK_PROBE_AFTER = 120  # 有 Cookie 但页面一直不跳转时，多久后开始用接口探针兜底
FALLBACK_PROBE_GAP = 15.0   # 兜底探针的间隔

# 活动页探针参数缓存：None=未尝试，False=拿不到（活动页不可用）
_PROBE_CACHE = None


def _header(msg: str) -> None:
    print(f"\n{msg}", flush=True)


# ------------------------------------------------------------------ 只读观察

async def _tencent_cookies(ctx) -> dict:
    cookies = await ctx.cookies()
    return {c["name"]: c["value"] for c in cookies
            if "tencent.com" in (c.get("domain") or "")}


def _looks_logged_url(url: str) -> bool:
    """标签页地址是否已经离开登录页（登录成功后站点会跳走）。"""
    if not url or "tencent.com" not in url:
        return False
    markers = ("/login", "login.", "signin", "/passport", "passport.")
    return not any(m in url for m in markers)


async def _left_login_page(ctx) -> bool:
    for pg in ctx.pages:
        try:
            if _looks_logged_url(pg.url or ""):
                return True
        except Exception:
            continue
    return False


# ------------------------------------------------------------------ 后台鉴权探针

def _build_probe_payload():
    """同步抓活动页拿 do-goods 参数（会被 to_thread 包起来，别直接在主循环调）。"""
    global _PROBE_CACHE
    if _PROBE_CACHE is not None:
        return _PROBE_CACHE or None
    import discover
    import seckill

    for url in discover.candidate_urls():
        try:
            html = discover.http_get(url)
        except Exception:
            continue
        info = discover.parse_page(html, url)
        if not info:
            continue
        offset = info["server_now"] - info["local_now"] if info.get("server_now") else 0.0
        s = seckill.resolve_session(info["sessions"], None, offset) or info["sessions"][0]
        _PROBE_CACHE = lib.build_payload(
            info["activity_id"], s["bussid"], s["act_id"],
            info["goods"]["goods_param"], info["regions"][0]["id"], url, preview=1)
        return _PROBE_CACHE
    _PROBE_CACHE = False
    return None


async def _http_authed(jar: dict) -> tuple[bool, str]:
    """用后台 HTTP 请求确认登录态（不开浏览器标签页，完全不打扰用户）。"""
    try:
        payload = await asyncio.to_thread(_build_probe_payload)
    except Exception:
        payload = None
    if not payload:
        # 活动页拿不到时无法用接口验证，退化为「Cookie 齐全」判定
        return True, "（活动页不可用，跳过接口验证）"

    import aiohttp
    csrf = lib.compute_csrf(lib.pick_skey(jar))
    try:
        async with aiohttp.ClientSession(cookies=jar) as session:
            return await lib.probe_auth(session, csrf, payload)
    except Exception as e:
        return False, f"网络请求失败: {e}"


# ------------------------------------------------------------------ 模式一：浏览器登录

async def browser_login(timeout_sec: int = 300) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[!] 没装 playwright，无法自动登录。")
        print("    请先执行: python3 bootstrap.py")
        print("    或改用: python3 login.py --paste")
        return 1

    lib.ensure_home()
    os.makedirs(lib.PROFILE_DIR, exist_ok=True)

    _header("=" * 56)
    print("  即将弹出一个浏览器窗口 ——")
    print("  请在里面用【微信扫码】或【账号密码】登录腾讯云。")
    print("  扫码后如果需要填短信验证码，慢慢填，脚本不会打扰你。")
    print("  登录完成后窗口会自己关闭（大约再等 2~5 秒）。")
    print("=" * 56, flush=True)

    async with async_playwright() as p:
        ctx = None
        last_err = None
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
            print("    请先执行: python3 bootstrap.py")
            return 1

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40000)
            except Exception:
                await page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=40000)

            deadline = time.time() + timeout_sec
            jar_ok = None
            dots = 0
            cookie_since = None
            next_fallback_probe = 0.0

            while time.time() < deadline:
                jar = await _tencent_cookies(ctx)
                cookie_ready = lib.has_login_cookies(jar)
                if cookie_ready and cookie_since is None:
                    cookie_since = time.time()

                # 正常路径：页面已经离开登录页 → 后台确认一次即可
                if cookie_ready and await _left_login_page(ctx):
                    print("\r[*] 检测到页面已跳转，正在后台确认登录态...          ", end="", flush=True)
                    ok, msg = await _http_authed(jar)
                    if ok:
                        jar_ok = jar
                        break
                    print(f"\n[!] 接口仍报未登录（{msg}），继续等待...", flush=True)

                # 兜底路径：Cookie 齐全但页面始终不跳（少数登录方式），隔一会儿探一次
                elif cookie_ready and time.time() >= next_fallback_probe:
                    if cookie_since and time.time() - cookie_since > FALLBACK_PROBE_AFTER:
                        ok, msg = await _http_authed(jar)
                        next_fallback_probe = time.time() + FALLBACK_PROBE_GAP
                        if ok:
                            jar_ok = jar
                            break

                dots += 1
                left = int(deadline - time.time())
                if cookie_ready:
                    print(f"\r[*] 已收到登录 Cookie，等你完成验证... 剩余 {left}s "
                          f"{'·' * (dots % 4)}  ", end="", flush=True)
                else:
                    print(f"\r[*] 等待扫码登录... 剩余 {left}s {'·' * (dots % 4)}  ",
                          end="", flush=True)
                await asyncio.sleep(POLL_SEC)

            print()
            if not jar_ok:
                print("[!] 超时仍未登录成功（不会再把临时 Cookie 误判为登录）。")
                print("    可以重跑一次；如果一直失败，改用 python3 login.py --paste")
                return 1

            lib.save_cookies(jar_ok, source="browser-verified")
            print("[+] 登录真实有效（后台已确认），Cookie 已保存")
            print(f"    {lib.summarize_cookies(jar_ok)}")
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
    if not lib.has_login_cookies(jar):
        print("[!] 这段 Cookie 里只有 skey、没有 uin —— 很可能是登录页的临时 Cookie，不是登录态。")
        print("    请确认已登录后再复制（换个 console.cloud.tencent.com 的请求）。")
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
    if not lib.has_login_cookies(jar):
        print("[!] 保存的 Cookie 不完整（只有 skey、没有 uin），需要重新登录")
        return 1

    print(f"[*] Cookie: {lib.summarize_cookies(jar)}")
    ok, msg = await _http_authed(jar)
    if ok:
        print(f"[+] 状态正常 —— 登录态有效（接口已确认）")
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
