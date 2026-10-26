#!/usr/bin/env python3
"""
腾讯云秒杀 —— 活动参数自动发现

不需要登录、不需要 Cookie。只做一件事：把活动页里那些「每天都要手动去 F12 里翻」
的参数全部自动挖出来。

用法:
    python3 discover.py                 # 自动找当月/次月活动
    python3 discover.py --url <活动页>  # 指定活动页
    python3 discover.py --json          # 只输出 JSON（给程序用）

输出 JSON 结构:
{
  "ok": true,
  "page_url": "...",
  "activity_id": 166111704417621,
  "goods": {"name": ..., "type": ..., "image_id": ..., "goods_param": {...}},
  "regions": [{"id": 1, "label": "广州"}, ...],
  "sessions": [{"date": "2026-10-27", "start_time": "10:00:00", "end_time": "12:00:00",
                "start_ts": 1793066400.0, "end_ts": ..., "bussid": 25053,
                "act_id": 1996855673581677, "status": "未开始"}],
  "server_now": 1793018442.696,
  "local_now": 1793018440.1,
  "clock_offset_ms": 2.6
}
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 目标商品的识别特征：SKU 里必须同时出现这两个词
TARGET_SKU_TOKENS = ("4核4G", "3M")

CANDIDATE_PATTERNS = [
    "https://cloud.tencent.com/act/pro/featured-{ym}",
    "https://cloud.tencent.com/act/pro/warmup-{ym}",
    "https://cloud.tencent.com/act/pro/seckill-{ym}",
]


# ---------------------------------------------------------------- 基础工具

def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", errors="ignore")


def extract_json_value(text: str, key: str, start: int = 0):
    """从混杂的 HTML 里抠出 key 对应的 JSON 值，返回 (value, found_index, end_index)。"""
    i = text.find('"' + key + '"', start)
    if i < 0:
        return None, -1, -1
    j = text.find(":", i) + 1
    if j <= 0:
        return None, -1, -1
    while j < len(text) and text[j] in " \n\r\t":
        j += 1
    if j >= len(text):
        return None, -1, -1

    if text[j] not in '{["-0123456789tfn':
        return None, -1, -1

    # 数字 / 布尔 / null
    if text[j] != '"' and text[j] not in "{[":
        m = re.match(r"-?\d+(?:\.\d+)?|true|false|null", text[j:j + 32])
        if not m:
            return None, -1, -1
        raw = m.group(0)
        try:
            return json.loads(raw), i, j + len(raw)
        except Exception:
            return None, -1, -1

    if text[j] == '"':
        k = text.find('"', j + 1)
        try:
            return json.loads(text[j:k + 1]), i, k
        except Exception:
            return None, -1, -1

    opener = text[j]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for k in range(j, len(text)):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1]), i, k
                except Exception:
                    return None, -1, -1
    return None, -1, -1


def extract_all_json_values(text: str, key: str):
    """抠出所有同名 key 的 JSON 值，返回 [(value, found_index), ...]。"""
    out, pos = [], 0
    while True:
        val, found, end = extract_json_value(text, key, pos)
        if val is None or end < 0:
            break
        out.append((val, found))
        pos = end + 1
    return out


# ---------------------------------------------------------------- 解析活动页

def parse_page(html: str, page_url: str) -> dict | None:
    state, _, _ = extract_json_value(html, "twoDaySeckillState")
    if not state or "flashResult" not in state:
        return None

    # --- 1. 找目标商品对应的场次 ---
    sessions, target_act_id, target_info = [], None, None
    for date_str, items in (state.get("flashResult") or {}).items():
        for item in items:
            for sale in item.get("sales") or []:
                sku = " ".join((sale.get("seckill_act_extra") or {}).get("skuList") or [])
                name = sale.get("goods_name") or ""
                haystack = sku + " " + name
                if not all(t in haystack for t in TARGET_SKU_TOKENS):
                    continue
                target_act_id = sale.get("act_id") or target_act_id
                target_info = {
                    "name": sale.get("goods_name") or name,
                    "sku": (sale.get("seckill_act_extra") or {}).get("skuList") or [],
                    "title": (sale.get("seckill_act_extra") or {}).get("title") or "",
                }
                try:
                    start_ts = item["startTime"] / 1000.0
                    end_ts = item["endTime"] / 1000.0
                except Exception:
                    start_ts = end_ts = 0.0
                sessions.append({
                    "date": item.get("start_date") or date_str,
                    "start_time": item.get("start_time") or "",
                    "end_time": item.get("end_time") or "",
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "bussid": item.get("id"),
                    "act_id": sale.get("act_id"),
                    "status": item.get("dynamic_status_cn") or "",
                })
                break

    if not sessions:
        return None

    # --- 2. 找该商品的 goods_param（type / imageId 等） ---
    goods_param, goods_type = None, None
    for cand, pos in extract_all_json_values(html, "defaultGoodsParam"):
        window = html[max(0, pos - 600):pos + 600]
        if target_act_id and str(target_act_id) in window:
            goods_param = cand
            goods_type = cand.get("type")
            break
    if goods_param is None:
        # 兜底：取第一个带 type 的
        for cand, _ in extract_all_json_values(html, "defaultGoodsParam"):
            if cand.get("type"):
                goods_param, goods_type = cand, cand.get("type")
                break

    # --- 3. 地域映射 ---
    regions = []
    for cand, _ in extract_all_json_values(html, "regionMap"):
        for area_key, lst in (cand.get("data") or {}).items():
            for r in lst:
                if isinstance(r, dict) and "value" in r and "label" in r:
                    regions.append({"id": r["value"], "label": r["label"],
                                    "region_en": r.get("regionEn", "")})
        if regions:
            break
    if not regions:
        regions = [{"id": 1, "label": "广州"}, {"id": 4, "label": "上海"},
                   {"id": 8, "label": "北京"}]

    # --- 4. 服务器时间（页面自带，无需额外请求） ---
    server_now = None
    for v in (state.get("nowTime"), state.get("flashResult") and None):
        if isinstance(v, (int, float)) and v > 1e12:
            server_now = v / 1000.0
            break
    if server_now is None:
        v, _, _ = extract_json_value(html, "nowTime")
        if isinstance(v, (int, float)) and v > 1e12:
            server_now = v / 1000.0

    activity_id, _, _ = extract_json_value(html, "activityId")

    return {
        "ok": True,
        "page_url": page_url,
        "activity_id": activity_id,
        "goods": {
            "name": (target_info or {}).get("name", ""),
            "title": (target_info or {}).get("title", ""),
            "sku": (target_info or {}).get("sku", []),
            "type": goods_type,
            "image_id": (goods_param or {}).get("imageId"),
            "goods_param": goods_param,
        },
        "regions": regions,
        "sessions": sorted(sessions, key=lambda s: s["start_ts"] or 0),
        "server_now": server_now,
        "local_now": time.time(),
    }


# ---------------------------------------------------------------- 主流程

def candidate_urls() -> list[str]:
    """候选活动页。

    腾讯云活动页 URL 里的月份经常滞后（比如 10 月的秒杀仍挂在 featured-202607 下），
    所以不能只靠「当前月份」猜。策略：
      1) 从活动总览页 https://cloud.tencent.com/act 抓所有 /act/pro/xxx 链接
      2) 按「像秒杀活动」的程度排序（含年月 / 双11 / 618 / featured / warmup）
      3) 再补几个常见固定命名兜底
    """
    urls: list[str] = []

    def add(u: str) -> None:
        if u not in urls:
            urls.append(u)

    scored: list[tuple[int, str]] = []
    try:
        index_html = http_get("https://cloud.tencent.com/act")
        for u in set(re.findall(r"https://cloud\.tencent\.com/act/pro/[A-Za-z0-9\-]+",
                                index_html)):
            score = 0
            low = u.lower()
            if re.search(r"20\d{4}", low):
                score += 3
            for kw, bonus in (("double11", 5), ("618", 5), ("seckill", 5),
                              ("featured", 4), ("warmup", 4), ("free", 2),
                              ("center", 2), ("promo", 2)):
                if kw in low:
                    score += bonus
            if score > 0:
                scored.append((-score, u))
        for _, u in sorted(scored):
            add(u)
    except Exception:
        pass

    now = datetime.now()
    for month_delta in (0, 1):
        y, m = now.year, now.month + month_delta
        if m > 12:
            y, m = y + 1, m - 12
        ym = f"{y}{m:02d}"
        for pat in CANDIDATE_PATTERNS:
            add(pat.format(ym=ym))
    for fixed in ("https://cloud.tencent.com/act/pro/featured-202607",
                  "https://cloud.tencent.com/act/pro/warmup-202606"):
        add(fixed)
    return urls


def main() -> int:
    ap = argparse.ArgumentParser(description="自动发现腾讯云秒杀活动参数")
    ap.add_argument("--url", help="手动指定活动页 URL")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    urls = [args.url] if args.url else candidate_urls()
    errors = []

    for url in urls:
        if not args.json:
            print(f"[*] 尝试活动页: {url}", file=sys.stderr)
        try:
            html = http_get(url)
        except Exception as e:
            errors.append(f"{url} -> 抓取失败: {e}")
            continue

        data = parse_page(html, url)
        if not data:
            errors.append(f"{url} -> 页面里没找到 4核4G3M 的秒杀场次")
            continue

        if data.get("server_now"):
            data["clock_offset_ms"] = round(
                (data["server_now"] - data["local_now"]) * 1000, 1)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0

        g = data["goods"]
        print(f"\n[+] 活动页: {data['page_url']}")
        print(f"[+] 商品  : {g['name']}")
        print(f"            SKU={g.get('sku')}  type={g.get('type')}  imageId={g.get('image_id')}")
        print(f"[+] activity_id = {data['activity_id']}")
        print(f"[+] 地域  : " + "  ".join(f"{r['id']}={r['label']}" for r in data["regions"]))
        if data.get("clock_offset_ms") is not None:
            print(f"[+] 时钟偏移: {data['clock_offset_ms']:+.0f}ms")
        print("[+] 场次  :")
        for s in data["sessions"]:
            print(f"      {s['date']} {s['start_time']}-{s['end_time']}  "
                  f"bussid={s['bussid']}  act_id={s['act_id']}  [{s['status']}]")
        print()
        return 0

    if not args.json:
        print("[!] 所有候选活动页都没找到目标商品。", file=sys.stderr)
        for e in errors:
            print(f"    - {e}", file=sys.stderr)
        print("[!] 该活动可能已结束或改为其它活动页，请用 --url 指定当前活动页。",
              file=sys.stderr)
    print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
