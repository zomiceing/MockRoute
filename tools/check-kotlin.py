"""编译前静态检查：Kotlin 括号平衡（剥掉注释与字符串，避免误报）。"""
import re
import sys
from pathlib import Path

BASE = Path("mockroute-android/app/src/main/java/com/mockroute")


def strip(src: str) -> str:
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            i = n if j < 0 else j + 3
        elif c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            i += 1
        elif c == "'":
            i += 1
            while i < n and src[i] != "'":
                i += 2 if src[i] == "\\" else 1
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


bad = False
for f in sorted(BASE.glob("*.kt")):
    s = strip(f.read_text(encoding="utf-8"))
    b = s.count("{") - s.count("}")
    p = s.count("(") - s.count(")")
    flag = "OK" if (b == 0 and p == 0) else "XX"
    if flag == "XX":
        bad = True
    print(f"  [{flag}] {f.name:26} {{}}={b:+d}  ()={p:+d}")

print()
print("  括号平衡，可以编译" if not bad else "  不平衡，别编译")
sys.exit(1 if bad else 0)
