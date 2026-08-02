"""终端子进程输出编码回归测试：UTF-8 与 GBK 输出均不应乱码。

这些测试覆盖之前「强制 UTF-8 解码」导致的乱码隐患——在 Windows 上子进程常以
GBK/CP936 输出，强制 UTF-8 会把中文变成替换字符。修复后 ``LocalExecutionBackend``
读取原始字节并按 UTF-8 → GBK → 系统首选编码回退解码。
"""

import sys
from pathlib import Path

from app.tools.tool_handler.terminal.local_backend import LocalExecutionBackend


def _write_runner(tmp_path: Path, code: str) -> str:
    """写一个直接往 stdout 二进制管道写字节的 Python 脚本，返回其路径。"""
    script = tmp_path / "runner.py"
    script.write_text(code, encoding="utf-8")
    return str(script)


def test_utf8_output_decoded_cleanly(tmp_path: Path) -> None:
    """子进程输出 UTF-8 字节时，结果包含原文且无替换字符。"""
    script = _write_runner(
        tmp_path,
        "import sys\n" "sys.stdout.buffer.write('中文输出测试'.encode('utf-8'))\n",
    )
    result = LocalExecutionBackend().execute(f"{sys.executable} {script}", str(tmp_path), 10.0)
    assert result.timed_out is False
    assert "中文输出测试" in result.output
    assert "\ufffd" not in result.output


def test_gbk_output_decoded_cleanly(tmp_path: Path) -> None:
    """子进程输出 GBK 字节时（Windows 常见），结果仍应正确解码为中文。"""
    script = _write_runner(
        tmp_path,
        "import sys\n" "sys.stdout.buffer.write('中文输出测试'.encode('gbk'))\n",
    )
    result = LocalExecutionBackend().execute(f"{sys.executable} {script}", str(tmp_path), 10.0)
    assert result.timed_out is False
    assert "中文输出测试" in result.output
    assert "\ufffd" not in result.output
