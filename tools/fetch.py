#!/usr/bin/env python3
"""独立下载器 —— 绕开 Windows schannel。

为什么需要它：
    这台机器的 Windows 证书/凭据子系统有问题，任何走 schannel 的 HTTPS 客户端
    都会在 TLS 握手时报 SEC_E_NO_CREDENTIALS：
        curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS
        Invoke-WebRequest: 基础连接已经关闭: 接收时发生错误
    而 Python 自带 OpenSSL，不依赖 schannel，所以 HTTPS 是通的。
    DNS 和 TCP 都正常，不是网络问题。

特性：
    * 支持断点续传（HTTP Range）—— 大文件下载中断后重跑不会从头开始
    * 只依赖标准库，不需要 pip install
    * 带进度显示，方便在后台日志里看进度

用法：
    python tools/fetch.py <url> <输出文件> [--expect-bytes N] [--sha256 HEX]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 1024 * 256
UA = "mockroute-fetch/1.0"

# 进度信息里有中文。Windows 控制台默认用 GBK(936) 解码子进程输出，
# 而 Python 按 UTF-8 写，于是全变成乱码 —— 不影响下载，但很难看。
# 这里显式把 stdout/stderr 切到 UTF-8 并关掉错误即崩的行为。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fetch(url: str, dest: Path, expect_bytes: int = 0,
          sha256: str = "", retries: int = 3) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    # 断点续传：已有 .part 就从它的大小接着下
    resume_from = part.stat().st_size if part.exists() else 0
    if resume_from and expect_bytes and resume_from >= expect_bytes:
        part.replace(dest)
        resume_from = 0

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            headers = {"User-Agent": UA}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            req = urllib.request.Request(url, headers=headers)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                # 服务器不支持断点续传时会返回 200，那就从头写
                if resume_from and resp.status != 206:
                    print("  [i] 服务器不支持续传，从头下载")
                    resume_from = 0
                    part.unlink(missing_ok=True)
                    mode = "wb"
                else:
                    mode = "ab" if resume_from else "wb"

                total = int(resp.headers.get("Content-Length", 0)) + resume_from
                if not total:
                    total = expect_bytes

                done = resume_from
                t0 = time.time()
                last_print = 0.0
                with open(part, mode) as f:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_print >= 2.0:
                            last_print = now
                            speed = done / max(0.001, now - t0)
                            if total:
                                pct = done * 100.0 / total
                                eta = (total - done) / max(1.0, speed)
                                print(f"  {pct:5.1f}%  {fmt_size(done)}/{fmt_size(total)}"
                                      f"  {fmt_size(speed)}/s  剩余约 {eta:.0f}s", flush=True)
                            else:
                                print(f"  {fmt_size(done)}  {fmt_size(speed)}/s", flush=True)

            size = part.stat().st_size
            if expect_bytes and size != expect_bytes:
                raise IOError(f"大小不符: 期望 {expect_bytes}，实际 {size}")
            if sha256:
                h = hashlib.sha256()
                with open(part, "rb") as f:
                    for blk in iter(lambda: f.read(CHUNK), b""):
                        h.update(blk)
                actual = h.hexdigest()
                if actual.lower() != sha256.lower():
                    raise IOError(f"sha256 不符: 期望 {sha256}，实际 {actual}")

            part.replace(dest)
            print(f"  完成: {dest}  ({fmt_size(size)})", flush=True)
            return 0

        except (urllib.error.URLError, urllib.error.HTTPError, IOError, OSError,
                ssl.SSLError) as e:
            last_err = e
            # 记录已下多少，便于下一次续传
            resume_from = part.stat().st_size if part.exists() else 0
            print(f"  [!] 第 {attempt}/{retries} 次失败: {type(e).__name__}: {e}", flush=True)
            if attempt < retries:
                wait = 3 * attempt
                print(f"      {wait}s 后重试（已下载 {fmt_size(resume_from)}，将续传）", flush=True)
                time.sleep(wait)

    print(f"  [XX] 下载失败: {url}", file=sys.stderr)
    print(f"       {last_err}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fetch", description="绕开 schannel 的下载器")
    ap.add_argument("url")
    ap.add_argument("dest")
    ap.add_argument("--expect-bytes", type=int, default=0,
                    help="期望字节数，用于校验完整性（0=不校验）")
    ap.add_argument("--sha256", default="", help="期望的 sha256（可选）")
    ap.add_argument("--retries", type=int, default=3)
    a = ap.parse_args(argv)

    print(f"[fetch] {a.url}")
    return fetch(a.url, Path(a.dest), a.expect_bytes, a.sha256, a.retries)


if __name__ == "__main__":
    raise SystemExit(main())
