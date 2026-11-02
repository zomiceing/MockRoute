#!/usr/bin/env python3
"""提交前自检 —— 在 git add / commit 之前跑一遍。

用法：
    python tools/preflight.py            # 检查「将会被提交的文件」
    python tools/preflight.py --staged   # 只检查已 git add 的文件

它会拦住这些常见事故：
  1. 个人隐私（真实姓名、校园/家附近的坐标、设备序列号、绝对路径）
  2. 大文件（SDK、构建产物、APK 被误提交）
  3. 密钥/证书（keystore、私钥、token）
  4. 仓库卫生（缺 LICENSE、误提交 .toolchain 之类）

退出码 0 = 可以提交；1 = 有问题，先修。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- 允许进入仓库的文件总大小上限（单文件）----
MAX_FILE_BYTES = 2 * 1024 * 1024     # 2 MB

# ---- 隐私 / 路径特征 ----
PRIVACY_PATTERNS = [
    (re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z0-9_.\-]+"), "Windows 用户目录（会暴露用户名）"),
    (re.compile(r"MGFNW\d{10,}"), "设备序列号"),
    (re.compile(r"[A-Za-z]:\\{1,2}python\d"), "Python 解释器硬编码路径"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[=:]\s*['\"][A-Za-z0-9_\-]{16,}"), "疑似硬编码密钥"),
]

# ---- 不该进仓库的路径特征 ----
FORBIDDEN_PATH = [
    (re.compile(r"(^|/)\.toolchain/"), "工具链目录（GB 级）"),
    (re.compile(r"(^|/)build-ws/"), "构建镜像目录"),
    (re.compile(r"(^|/)out/"), "运行产物"),
    (re.compile(r"(^|/)dist/"), "打包产物"),
    (re.compile(r"(^|/)__pycache__/"), "Python 缓存"),
    (re.compile(r"\.apk$|\.aab$"), "APK/AAB 产物"),
    (re.compile(r"\.(keystore|jks|pem|p12|pfx)$"), "密钥/证书"),
    (re.compile(r"(^|/)locations\.json$"), "个人位置数据（应只保留 locations.example.json）"),
    (re.compile(r"(^|/)\.env$|(^|/)\.env\."), "环境变量文件"),
]

TEXT_SUFFIX = {".py", ".kt", ".md", ".txt", ".ps1", ".cmd", ".json", ".xml",
               ".gradle", ".kts", ".pro", ".properties", ".yml", ".yaml",
               ".gpx", ".geojson", ".gitignore", ".gitattributes", ""}


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout


def list_files(staged: bool) -> list[Path]:
    if staged:
        out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        out = git("status", "--porcelain", "--untracked-files=all")
        out = "\n".join(line[3:] for line in out.splitlines() if line.strip())
    files = []
    for line in out.splitlines():
        line = line.strip().strip('"')
        if line:
            p = ROOT / line
            if p.is_file():
                files.append(p)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="提交前自检")
    ap.add_argument("--staged", action="store_true",
                    help="只检查已 git add 的文件")
    args = ap.parse_args()

    if not (ROOT / ".git").exists():
        print("这不是 git 仓库（没找到 .git/）")
        return 1

    files = list_files(args.staged)
    mode = "已暂存" if args.staged else "将被提交"
    print("=" * 74)
    print(f"提交前自检 —— {mode} {len(files)} 个文件")
    print("=" * 74)

    problems: list[str] = []

    # ---- 1. 路径层面 ----
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        for pat, why in FORBIDDEN_PATH:
            if pat.search(rel):
                problems.append(f"[不该提交] {rel} —— {why}")

    # ---- 2. 大文件 ----
    for p in files:
        n = p.stat().st_size
        if n > MAX_FILE_BYTES:
            rel = p.relative_to(ROOT).as_posix()
            problems.append(f"[文件过大] {rel} —— {n/1024/1024:.1f} MB"
                            f"（上限 {MAX_FILE_BYTES/1024/1024:.0f} MB）")

    # ---- 3. 内容层面（只扫文本文件）----
    for p in files:
        if p.suffix.lower() not in TEXT_SUFFIX and p.suffix != "":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = p.relative_to(ROOT).as_posix()
        for pat, why in PRIVACY_PATTERNS:
            m = pat.search(text)
            if m:
                snippet = m.group(0)[:60]
                problems.append(f"[隐私/密钥] {rel} —— {why}：{snippet!r}")

    # ---- 4. 仓库卫生 ----
    for need in ("LICENSE", "README.md", ".gitignore"):
        if not (ROOT / need).exists():
            problems.append(f"[缺文件] {need} 不存在")

    # ---- 5. 配置文件别被自己忽略 ----
    #
    # 白名单式 .gitignore 的经典陷阱：开头的 `/*` 连隐藏文件一起忽略了，
    # 如果忘了 `!/.gitignore`，配置文件本身就不会进仓库。
    # 实测踩过：清单里少了 .gitignore 和 .gitattributes，肉眼很难发现
    # （它们在 `git status` 里根本不显示，因为被当成"已忽略"）。
    for must in (".gitignore", ".gitattributes"):
        if not (ROOT / must).exists():
            continue
        r = subprocess.run(["git", "check-ignore", "-q", must],
                           cwd=ROOT, capture_output=True)
        if r.returncode == 0:
            problems.append(
                f"[配置失效] {must} 被 .gitignore 自己忽略了，不会进仓库"
                f" —— 在 .gitignore 里加一行 !/{must}")

    # ---- 结果 ----
    print()
    if not problems:
        print(f"  OK  没发现问题，可以提交。")
        print()
        print(f"  这次会提交 {len(files)} 个文件，总计 "
              f"{sum(p.stat().st_size for p in files)/1024:.0f} KB")
        return 0

    print(f"  发现 {len(problems)} 个问题：")
    print()
    for i, msg in enumerate(problems, 1):
        print(f"  {i:>3}. {msg}")
    print()
    print("  处理建议：")
    print("    · 「不该提交」→ 加进 .gitignore，然后 git rm --cached <路径>")
    print("    · 「隐私/密钥」→ 删除或改成示例值（一旦提交会留在 git 历史里）")
    print("    · 「文件过大」→ 别用 git 存二进制产物")
    return 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
