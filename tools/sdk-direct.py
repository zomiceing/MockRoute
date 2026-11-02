#!/usr/bin/env python3
"""直接安装 Android SDK 组件（绕开 sdkmanager）。

为什么需要它
------------
这台机器上 sdkmanager 无法访问远程仓库 —— 它执行到 "Computing updates..."
就永远卡住（Java 的 TLS 栈被挡，和 schannel 那个问题是同一类）。
但官方 zip 的直链是可用的，Python 能下，所以改为自己下载解压。

踩过的坑（都已在下面处理）
--------------------------
1. zip 内部顶层目录名不一定等于目标目录名：
       build-tools_r34-windows.zip  ->  内部是 android-14/   （历史遗留命名）
       platform-34-ext8_r01.zip     ->  内部是 android-34/   （正好等于目标名）
   必须重命名，否则 Gradle 报 "Failed to find Build Tools revision 34.0.0"。
2. platform 34 在仓库里叫 platform-34-extNN_rNN.zip，没有 platform-34_rNN.zip。
3. Gradle 还会校验 SDK 许可，sdkmanager 不在就得自己写 licenses/ 下的哈希文件。

用法
----
    python tools/sdk-direct.py --sdk <SDK 根目录> [--only platform-tools]

幂等：已经装好的组件会跳过。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "https://dl.google.com/android/repository/"
UA = "mockroute-sdk/1.0"

# name -> (目标相对路径, 用于校验的关键文件)
COMPONENTS = {
    "platform-tools": ("platform-tools", "adb.exe"),
    "platforms;android-34": ("platforms/android-34", "android.jar"),
    "build-tools;34.0.0": ("build-tools/34.0.0", "aapt2.exe"),
}

# Android SDK 各许可的 SHA1（sdkmanager 写的也是这些公开值）。
# 少了它们，Gradle 会报 "You have not accepted the license agreements"。
LICENSE_HASHES = {
    "android-sdk-license": [
        "24333f8a63b6825ea9c5514f83c2829b004d1fee",
        "d56f5187479451eabf01fb78af6dfcb131a6481e",
    ],
    "android-sdk-preview-license": [
        "84831b9409646a918e30573bab4c9c91346d8abd",
    ],
    "android-sdk-arm-dbt-license": [
        "859f317696f67ef3d7f30a50a5560e7834b43903",
    ],
    "intel-android-extra-license": [
        "d975f751698a77b662f1254ddbeed3901e976f5a",
    ],
}


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_repo_urls(timeout: float = 60.0) -> list:
    with http_get(REPO + "repository2-3.xml", timeout) as r:
        xml = r.read().decode("utf-8", "replace")
    return re.findall(r"<url>([^<]+)</url>", xml)


def resolve_urls(urls: list) -> dict:
    """把组件名映射到官方 zip 文件名。"""
    def first(pred):
        for u in urls:
            if pred(u):
                return u
        return None

    pt = first(lambda u: u.startswith("platform-tools_r") and "-win" in u)

    # platform-34-extNN_rNN.zip，取 extNN 最大的（位数固定，字符串排序可用）
    plat = first(lambda u: u.startswith("platform-34_r"))
    if not plat:
        exts = [u for u in urls if re.match(r"platform-34-ext\d+_r\d+\.zip$", u)]
        if exts:
            def ext_num(n):
                m = re.search(r"platform-34-ext(\d+)_", n)
                return int(m.group(1)) if m else -1
            plat = max(exts, key=ext_num)

    bt = first(lambda u: u == "build-tools_r34-windows.zip")

    # platform-tools 的探测脚本会发现顶层就叫 platform-tools；
    # platform 顶层叫 android-34；build-tools 顶层叫 android-14（要改名）。
    return {
        "platform-tools": (pt, "platform-tools"),
        "platforms;android-34": (plat, "android-34"),
        "build-tools;34.0.0": (bt, "android-14"),
    }


def download(url: str, dest: Path, expect_min: int = 1_000_000) -> None:
    if dest.exists() and dest.stat().st_size >= expect_min:
        log(f"    已缓存 {dest.name} ({dest.stat().st_size/1024/1024:.1f} MB)")
        return
    log(f"    下载 {url}")
    part = dest.with_suffix(dest.suffix + ".part")
    done = 0
    with http_get(url) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done % (4 * 1024 * 1024) < 262144:
                log(f"      {done*100//total}%  {done/1024/1024:.1f}/{total/1024/1024:.1f} MB")
    if expect_min and part.stat().st_size < expect_min:
        raise IOError(f"{dest.name} 太小 ({part.stat().st_size} 字节)，下载可能失败")
    part.replace(dest)
    log(f"    完成 {dest.name} ({dest.stat().st_size/1024/1024:.1f} MB)")


def install_component(sdk: Path, name: str, url: str, inner_top: str,
                      cache: Path, force: bool) -> bool:
    rel, marker = COMPONENTS[name]
    target = sdk / rel
    marker_path = target / marker

    if marker_path.exists() and not force:
        log(f"  [skip] {name} 已就绪 ({marker_path.relative_to(sdk)})")
        return True

    if not url:
        log(f"  [FAIL] {name}: 仓库清单里找不到对应压缩包")
        return False

    log(f"  [..] {name}")
    zpath = cache / url
    try:
        download(REPO + url, zpath)
    except Exception as e:
        log(f"  [FAIL] {name}: 下载失败 {type(e).__name__}: {e}")
        return False

    tmp = cache / (name.replace(";", "_").replace(":", "_") + "__x")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    log("    解压 ...")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)

    src = tmp / inner_top
    if not src.is_dir():
        # 兜底：顶层只有一个目录时就用它
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        if len(tops) == 1:
            src = tops[0]
        else:
            log(f"  [FAIL] {name}: 压缩包内部结构意外，顶层={[p.name for p in tmp.iterdir()]}")
            return False

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    shutil.rmtree(tmp, ignore_errors=True)

    if not marker_path.exists():
        log(f"  [FAIL] {name}: 解压后仍缺少 {marker}")
        return False
    log(f"  [OK] {name} -> {target.relative_to(sdk)}")
    return True


def write_licenses(sdk: Path) -> None:
    lic = sdk / "licenses"
    lic.mkdir(parents=True, exist_ok=True)
    for name, hashes in LICENSE_HASHES.items():
        (lic / name).write_text("\n".join(hashes) + "\n", encoding="utf-8")
    log(f"  [OK] 写入 {len(LICENSE_HASHES)} 个许可文件到 licenses/")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sdk-direct",
                                 description="直接下载安装 Android SDK 组件")
    ap.add_argument("--sdk", required=True, help="SDK 根目录")
    ap.add_argument("--only", action="append", default=None,
                    help="只装指定组件（可重复），默认全部")
    ap.add_argument("--force", action="store_true", help="已存在也重装")
    ap.add_argument("--keep-cache", action="store_true", help="保留下载的 zip")
    a = ap.parse_args(argv)

    sdk = Path(a.sdk)
    cache = sdk.parent / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    sdk.mkdir(parents=True, exist_ok=True)

    log(f"SDK 根目录 : {sdk}")
    log(f"下载缓存   : {cache}")
    log("")

    log("读取仓库清单 ...")
    try:
        urls = fetch_repo_urls()
    except Exception as e:
        log(f"  [FAIL] 无法读取 repository2-3.xml: {type(e).__name__}: {e}")
        return 1
    table = resolve_urls(urls)
    log(f"  共 {len(urls)} 个条目")
    for name, (u, inner) in table.items():
        log(f"  {name:24} <- {u}   (内部顶层 {inner})")
    log("")

    wanted = a.only or list(table.keys())
    ok = True
    for name in wanted:
        if name not in table:
            log(f"  [FAIL] 未知组件: {name}（可用: {', '.join(table)}）")
            ok = False
            continue
        url, inner = table[name]
        if not install_component(sdk, name, url, inner, cache, a.force):
            ok = False

    log("")
    write_licenses(sdk)

    if not a.keep_cache:
        for z in cache.glob("*.zip"):
            z.unlink(missing_ok=True)
        log("  [OK] 已清理下载缓存")

    log("")
    log("校验：")
    for name, (rel, marker) in COMPONENTS.items():
        p = sdk / rel / marker
        log(f"  {'[OK]  ' if p.exists() else '[MISS]'} {rel}/{marker}")

    return 0 if ok else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
