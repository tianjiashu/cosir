import io

p = r"h:\coding-agent\apps\backend\app\storage\crud\task_common.py"
s = io.open(p, encoding="utf-8").read()

marker = "def _preview(value: str, limit: int = 80) -> str:"
start = s.index(marker)
# 找到下一个顶层 def（即下一个 "\ndef "）作为结束边界
end = s.find("\ndef ", start + len(marker))
if end == -1:
    end = len(s)
else:
    # 回退到该 def 前的换行，保留尾部空行
    end = s.rfind("\n", 0, end) + 1

new_block = (
    'def _preview(value: str, limit: int = 80) -> str:\n'
    '    """返回适合标题和侧栏展示的单行摘要。\n'
    "\n"
    "    参数:\n"
    "        value: 原始用户输入或消息文本。\n"
    "        limit: 摘要允许的最大字符数，默认 80。\n"
    "\n"
    "    返回:\n"
    "        去除多余空白后的单行摘要文本；超出长度时截断并追加省略号。\n"
    "\n"
    "    异常:\n"
    "        无。\n"
    "\n"
    "    副作用:\n"
    "        无。\n"
    '    """\n'
    "\n"
    '    normalized = " ".join(value.strip().split())\n'
    "    if len(normalized) <= limit:\n"
    "        return normalized\n"
    '    return f"{normalized[: limit - 1]}..."\n'
)

s2 = s[:start] + new_block + s[end:]
io.open(p, "w", encoding="utf-8").write(s2)
print("done; replaced bytes:", end - start)
