"""腾讯云秒杀 —— 共用工具（Cookie / CSRF / 服务器校时 / 配置）"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

HOME = os.path.expanduser("~/.tencent-cloud-38-seckill")
COOKIE_FILE = os.path.join(HOME, "cookies.json")
CONFIG_FILE = os.path.join(HOME, "config.json")
PROFILE_DIR = os.path.join(HOME, "browser_profile")
LOG_FILE = os.path.join(HOME, "last_run.log")

API_QUERY = "https://act-api.cloud.tencent.com/dianshi/query-seckill-by-date"
API_BUY = "https://act-api.cloud.tencent.com/dianshi/do-goods"

REQUIRED_COOKIES = ("skey", "p_skey", "uin", "p_uin")


# ------------------------------------------------------------------ 基础

def ensure_home() -> None:
    os.makedirs(HOME, exist_ok=True)


def compute_csrf(skey: str) -> str:
    """腾讯云 CSRF Token = djb2(skey) & 0x7FFFFFFF"""
    if not skey:
        return ""
    n = 5381
    for ch in skey:
        n += (n << 5) + ord(ch)
    return str(2147483647 & n)


def cookie_header_to_dict(cookie_str: str) -> dict:
    out = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def pick_skey(cookies: dict) -> str:
    return cookies.get("skey") or cookies.get("p_skey") or ""


def has_login_cookies(cookies: dict) -> bool:
    """是否具备完整登录态。

    只看 skey 是错的 —— cloud.tencent.com/login 页面本身就会下发一张
    临时 skey（防 CSRF 用），未登录时也存在。真正的判据是 skey 和 uin 同时有值。
    """
    if not cookies:
        return False
    if not pick_skey(cookies):
        return False
    return bool(cookies.get("uin") or cookies.get("p_uin"))


def summarize_cookies(cookies: dict) -> str:
    """只暴露安全的部分，别把 skey 打到日志里。"""
    bits = []
    if cookies.get("uin") or cookies.get("p_uin"):
        bits.append(f"uin={cookies.get('uin') or cookies.get('p_uin')}")
    user = cookies.get("lusername") or cookies.get("username")
    if user:
        bits.append(f"账号={user}")
    skey = pick_skey(cookies)
    if skey:
        bits.append(f"skey=***{skey[-4:]}({len(skey)}位)")
    return "  ".join(bits) if bits else "（空）"


# ------------------------------------------------------------------ 配置文件

def load_cookies() -> dict:
    if not os.path.exists(COOKIE_FILE):
        return {}
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "cookies" in data:
            return data["cookies"] or {}
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_cookies(cookies: dict, source: str = "manual") -> None:
    ensure_home()
    payload = {
        "cookies": cookies,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
    }
    fd = os.open(COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    ensure_home()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ 服务器时间

def server_now_from_header(date_header: str | None) -> float | None:
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).timestamp()
    except Exception:
        return None


def find_now_time(payload) -> float | None:
    """在响应 JSON 里递归找 nowTime（毫秒）。接口和活动页都带这个字段。"""
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            v = cur.get("nowTime")
            if isinstance(v, (int, float)) and v > 1e12:
                return v / 1000.0
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur[:4])
    return None


class Clock:
    """用若干个样本中「网络往返最短」的那个估计服务器时钟偏移。"""

    def __init__(self, offset: float = 0.0):
        self.offset = offset

    def now(self) -> datetime:
        return datetime.now() + timedelta(seconds=self.offset)

    def now_ts(self) -> float:
        return time.time() + self.offset

    def describe(self) -> str:
        return f"本地时钟偏移 {self.offset * 1000:+.0f}ms"


async def calibrate_clock(session, csrf: str, activity_id: int, rounds: int = 5) -> Clock:
    """校准服务器时钟。

    优先级：
      1. 接口返回的 nowTime（毫秒级）—— 精度最好，误差 ≈ RTT/2
      2. HTTP Date 响应头（秒级）—— 兜底，量化误差最大 ±500ms
    只采信「往返最快」的那一次，减少网络抖动影响。
    """
    best_ms, best_rtt = None, float("inf")
    best_hdr, hdr_rtt = None, float("inf")

    for _ in range(rounds):
        t0 = time.time()
        try:
            async with session.post(
                API_QUERY,
                json={"activity_id": activity_id, "preview": 0,
                      "seckill_type": "block", "goods_type": "goods", "days": 2},
                headers={"X-Csrf-Token": csrf},
                timeout=5,
            ) as resp:
                body = await resp.text()
                date_hdr = resp.headers.get("Date")
            t1 = time.time()
        except Exception:
            continue

        rtt = t1 - t0
        mid = (t0 + t1) / 2

        try:
            srv = find_now_time(json.loads(body))
        except Exception:
            srv = None
        if srv and rtt < best_rtt:
            best_rtt, best_ms = rtt, srv - mid

        if date_hdr:
            dsrv = server_now_from_header(date_hdr)
            if dsrv and rtt < hdr_rtt:
                hdr_rtt, best_hdr = rtt, dsrv - mid

    if best_ms is not None:
        return Clock(best_ms)
    return Clock(best_hdr if best_hdr is not None else 0.0)


def build_payload(activity_id: int, bussid: int, act_id, goods_param: dict,
                  region_id: int, from_url: str, preview: int = 0) -> dict:
    """组装 do-goods 请求体。参数全部来自活动页自动发现。"""
    param = dict(goods_param or {})
    param["regionId"] = region_id
    goods_type = param.get("type") or "bundle_budget_mc_lg4_01"
    param["type"] = goods_type
    return {
        "activity_id": activity_id,
        "agent_channel": {"fromChannel": "", "fromSales": "",
                          "isAgentClient": False, "fromUrl": from_url},
        "business": {"id": bussid, "from": "lightningDeals"},
        "goods": [{"act_id": act_id, "type": goods_type, "goods_param": param}],
        "preview": preview,
    }


AUTH_FAIL_TOKENS = ("NOT-LOGINED", "NOT_LOGIN", "登陆态校验失败", "请先登录", "未登录")


async def probe_auth(session, csrf: str, payload: dict) -> tuple[bool, str]:
    """用 do-goods（preview=1，不生成订单）判断 Cookie 是否真的有效。

    query-seckill-by-date 是公开接口，未登录也会正常返回，靠它判断不出登录态，
    所以必须打 do-goods —— 它会先做登陆态校验。
    """
    probe = dict(payload)
    probe["preview"] = 1
    try:
        async with session.post(API_BUY, json=probe,
                                headers={"X-Csrf-Token": csrf},
                                timeout=10) as resp:
            text = await resp.text()
    except Exception as e:
        return False, f"网络请求失败: {e}"

    upper = text.upper()
    if any(t.upper() in upper for t in AUTH_FAIL_TOKENS):
        return False, "Cookie 已失效，需要重新登录"
    if "CSRF" in upper:
        return False, "CSRF 校验失败（Cookie 里的 skey 不对）"
    return True, text[:160]


async def check_login(session, csrf: str, activity_id: int) -> tuple[bool, str]:
    """兼容旧调用：只做公开接口连通性检查（判断不了登录态）。"""
    try:
        async with session.post(
            API_QUERY,
            json={"activity_id": activity_id, "preview": 0,
                  "seckill_type": "block", "goods_type": "goods", "days": 2},
            headers={"X-Csrf-Token": csrf},
            timeout=8,
        ) as resp:
            await resp.text()
        return True, "接口可达"
    except Exception as e:
        return False, f"网络请求失败: {e}"
