#!/usr/bin/env python3
"""
腾讯云秒杀 —— 一次性环境准备（可重复执行，已装过会自动跳过）

在独立的 venv 里装依赖，不污染系统 Python。
    python3 bootstrap.py

完成后会打印 venv 里 python 的路径，后续脚本都用它来跑。
"""
from __future__ import annotations

import os
import subprocess
import sys

HOME = os.path.expanduser("~/.tencent-cloud-38-seckill")
VENV = os.path.join(HOME, "venv")
VPY = os.path.join(VENV, "bin", "python")
REQUIRED = ["aiohttp", "playwright"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    os.makedirs(HOME, exist_ok=True)
    os.makedirs(os.path.join(HOME, "browser_profile"), exist_ok=True)

    if not os.path.exists(VPY):
        print("[*] 创建独立运行环境（只需一次）...")
        r = run([sys.executable, "-m", "venv", VENV])
        if r.returncode != 0 or not os.path.exists(VPY):
            print("[!] 创建 venv 失败：")
            print(r.stdout[-800:] + r.stderr[-800:])
            return 1

    missing = []
    for pkg in REQUIRED:
        r = run([VPY, "-c", f"import {pkg}"])
        if r.returncode != 0:
            missing.append(pkg)

    if not missing:
        print(f"[+] 依赖已就绪：{', '.join(REQUIRED)}")
        print(f"[+] 运行环境：{VPY}")
        return 0

    print(f"[*] 安装依赖：{', '.join(missing)}（首次约 40MB，需要 30-60 秒）...")
    r = run([VPY, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    r = run([VPY, "-m", "pip", "install", "-q", *missing])
    if r.returncode != 0:
        print("[!] 安装失败：")
        print((r.stdout or "")[-800:] + (r.stderr or "")[-800:])
        print("    如果是网络问题，可以加国内镜像重试：")
        print(f"    {VPY} -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple {' '.join(missing)}")
        return 1

    r = run([VPY, "-c", "import aiohttp, playwright"])
    if r.returncode != 0:
        print("[!] 装完了但导入失败，请把上面的报错发给我。")
        return 1

    print(f"[+] 安装完成：{', '.join(missing)}")
    print(f"[+] 运行环境：{VPY}")
    print("[*] 说明：登录时优先复用系统已装的 Chrome，不需要额外下载浏览器内核。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
