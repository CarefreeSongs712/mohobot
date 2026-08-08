"""前端 JS 词法括号检查(跳过字符串/注释/模板插值)。"""
import re
from pathlib import Path

html = Path("mohobot/web_panel/static/index.html").read_text(encoding="utf-8")
scripts = "".join(re.findall(r"<script>([\s\S]*?)</script>", html))

BACKSLASH = chr(92)

i, n = 0, len(scripts)
stack = []
line = 1
pairs = {")": "(", "}": "{", "]": "["}
ok = True
while i < n:
    c = scripts[i]
    if c == "\n":
        line += 1
        i += 1
        continue
    if scripts.startswith("//", i):
        j = scripts.find("\n", i)
        i = n if j == -1 else j
        continue
    if scripts.startswith("/*", i):
        j = scripts.find("*/", i + 2)
        i = n if j == -1 else j + 2
        continue
    if c in ('"', "'", "`"):
        quote = c
        j = i + 1
        while j < n:
            if scripts[j] == BACKSLASH:
                j += 2
                continue
            if scripts[j] == quote:
                break
            if quote == "`" and scripts[j] == "$" and j + 1 < n and scripts[j + 1] == "{":
                depth = 1
                k = j + 2
                while k < n and depth:
                    if scripts[k] == "{":
                        depth += 1
                    elif scripts[k] == "}":
                        depth -= 1
                    k += 1
                j = k
                continue
            j += 1
        i = j + 1
        continue
    # 正则字面量: /pattern/flags (粗略判断, 前一个字符是运算符/括号/冒号/等号/逗号)
    if c == "/" and i > 0 and scripts[i - 1] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$)]}":
        j = i + 1
        in_class = False
        while j < n:
            ch = scripts[j]
            if ch == BACKSLASH:
                j += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                break
            elif ch == "\n":
                break
            j += 1
        i = j + 1  # 跳过 flags
        continue
    if c in "({[":
        stack.append((c, line))
    elif c in ")}]":
        if not stack or stack[-1][0] != pairs[c]:
            print("MISMATCH:", c, "at line", line, "stack top:", stack[-1] if stack else None)
            ok = False
            break
        stack.pop()
    i += 1

if stack:
    print("UNCLOSED:", stack[:5])
    ok = False
if ok and i >= n:
    print("JS lexical balance OK (all brackets closed)")
