import io

p = r"h:\coding-agent\apps\backend\app\storage\crud\task_common.py"
s = io.open(p, encoding="utf-8").read()
repl = "\ufffd"
print("U+FFFD count:", s.count(repl))
print("preview clean present:", "返回适合标题和侧栏展示的单行摘要" in s)
print("garbled present:", "杩斿洖" in s)
