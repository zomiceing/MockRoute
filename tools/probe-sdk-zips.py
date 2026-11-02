#!/usr/bin/env python3
"""探查 Android SDK 各官方压缩包内部结构，用于确定落位目录。

背景：这台机器上 sdkmanager 无法访问远程仓库（Java 的 TLS 栈被挡），
所以改为直接下载官方 zip。但 Gradle 是按固定目录布局找 SDK 的：

    <sdk>/platform-tools/adb.exe
    <sdk>/platforms/android-34/android.jar
    <sdk>/build-tools/34.0.0/aapt2.exe

而 zip 内部的顶层目录名不一定等于目标目录名（历史上从 android-4.1.2 变成
android-34 这种形式）。所以先抓每个 zip 的中央目录读出真实顶层目录名。

用法：python tools/probe-sdk-zips.py
"""

from __future__ import annotations

import io
import re
import struct
import sys
import urllib.request
import zipfile

BASE = "https://dl.google.com/android/repository/"
UA = "mockroute-probe/1.0"


def remote_zip_names(url: str) -> list:
    """只读 zip 的中央目录，不下载整个文件（用 Range 取尾部 64KB）。"""
    # 先拿总大小
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        size = int(r.headers.get("Content-Length", 0))
    if size <= 0:
        raise IOError("拿不到 Content-Length")
    want = min(65536, size)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Range": f"bytes={size - want}-{size - 1}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return _parse_central_directory(data)


def _parse_central_directory(data: bytes) -> list:
    sig = b"PK\x01\x02"
    names = []
    pos = 0
    while True:
        idx = data.find(sig, pos)
        if idx < 0 or idx + 46 > len(data):
            break
        try:
            nlen = struct.unpack_from("<H", data, idx + 28)[0]
            name = data[idx + 46: idx + 46 + nlen].decode("utf-8", "replace")
            names.append(name)
        except Exception:
            pass
        pos = idx + 4
    return names


def main() -> int:
    repo = urllib.request.urlopen(
        urllib.request.Request(BASE + "repository2-3.xml", headers={"User-Agent": UA}),
        timeout=40).read().decode("utf-8", "replace")
    urls = re.findall(r"<url>([^<]+)</url>", repo)

    def find(pred):
        for u in urls:
            if pred(u):
                return u
        return None

    targets = {
        "platform-tools": find(lambda u: u.startswith("platform-tools_r") and "-win" in u),
        # platform 34 在仓库里的文件名形如 platform-34-ext12_r01.zip，
        # 取补丁级别最高的那个（字符串排序对 extNN 有效，因为位数固定）
        "platforms;android-34": max(
            (u for u in urls if re.match(r"platform-34-ext\d+_r\d+\.zip$", u)),
            default=None,
        ),
        "build-tools;34.0.0": find(lambda u: u == "build-tools_r34-windows.zip"),
    }

    print("选定的官方压缩包：")
    for k, v in targets.items():
        print(f"  {k:24} -> {v}")
    print()

    for label, url in targets.items():
        if not url:
            print(f"[{label}] 未在仓库清单里找到")
            continue
        full = BASE + url
        try:
            names = remote_zip_names(full)
        except Exception as e:
            print(f"[{label}] 读取失败: {type(e).__name__}: {e}")
            continue
        tops = sorted({n.split("/")[0] for n in names if n})
        print(f"[{label}]  {full}")
        print(f"  中央目录条目数: {len(names)}  顶层: {tops}")
        # 挑几个关键文件确认路径
        keys = [n for n in names if n.endswith(("adb.exe", "android.jar", "aapt2.exe",
                                                "source.properties", "package.xml"))]
        for k in keys[:6]:
            print(f"    {k}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
