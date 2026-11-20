#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""檢查手冊結構：章節、程式碼圍欄、內嵌 app.py 是否可解析。"""
import ast
import re

PA = "/home/rakazo/shared/gps-上報-設置手冊.md"
t = open(PA, encoding="utf-8").read()
print("chars", len(t), "lines", t.count("\n"))
print("sections:")
for l in t.split("\n"):
    if l.startswith("## "):
        print("  ", l)
blocks = re.findall(r"```(\w+)\n(.*?)```", t, re.S)
print("fences:", [(b[0], len(b[1])) for b in blocks])
pys = [b[1] for b in blocks if b[0] == "python"]
print("python blocks:", len(pys))
ast.parse(pys[0])
print("embedded app.py parses OK")
open("/tmp/extract_app.py", "w").write(pys[0])
print("match original:", pys[0].strip() == open("/home/rakazo/shared/gps-docker/app.py", encoding="utf-8").read().strip())
