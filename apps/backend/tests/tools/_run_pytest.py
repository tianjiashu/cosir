import io
import sys
import traceback

out_path = "H:/pytest_run2.txt"
buf = io.StringIO()
try:
    import pytest
    buf.write("pytest imported OK\n")
    sys.stdout = buf
    rc = pytest.main(["tests/tools/test_codegraph_tool.py", "-q", "-p", "no:cacheprovider", "--no-header", "-rA"])
    sys.stdout = sys.__stdout__
    buf.write(f"\n=== FINAL EXIT CODE: {rc} ===\n")
except Exception:
    buf.write("EXCEPTION:\n" + traceback.format_exc() + "\n")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
