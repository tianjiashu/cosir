"""针对后端启动状态文件写入器（bootstate）的边界与失败路径测试。

覆盖：缺失配置、非法值、依赖未就绪、重复初始化、并发初始化，
以及原子写与容错行为。
"""

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from app.bootstate import (
    BOOT_PHASE_BOOTING,
    BOOT_PHASE_FAILED,
    BOOT_PHASE_READY,
    boot_state_file_from_env,
    write_bootstate,
)


class BootStateFileFromEnvTests(unittest.TestCase):
    """校验从环境变量解析启动状态文件路径。"""

    def test_env_unset_returns_none(self) -> None:
        env = dict(os.environ)
        env.pop("CODING_AGENT_BOOT_STATE_FILE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(boot_state_file_from_env())

    def test_env_empty_returns_none(self) -> None:
        with mock.patch.dict(
            os.environ, {"CODING_AGENT_BOOT_STATE_FILE": ""}, clear=True
        ):
            self.assertIsNone(boot_state_file_from_env())

    def test_env_set_returns_path(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CODING_AGENT_BOOT_STATE_FILE": "/tmp/x/boot.json"},
            clear=True,
        ):
            result = boot_state_file_from_env()
            self.assertIsNotNone(result)
            self.assertEqual(result, Path("/tmp/x/boot.json"))


class WriteBootStateTests(unittest.TestCase):
    """校验 write_bootstate 的正常、边界与失败路径。"""

    def test_write_booting_then_ready_overwrites(self) -> None:
        """重复初始化：先 booting 后 ready，文件应被覆盖为最新内容。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(path, BOOT_PHASE_BOOTING, step="start")
            write_bootstate(path, BOOT_PHASE_READY, step="app_ready")

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], BOOT_PHASE_READY)
            self.assertEqual(data["step"], "app_ready")
            # 不应残留 booting 阶段
            self.assertNotEqual(data["phase"], BOOT_PHASE_BOOTING)

    def test_write_failed_includes_error_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                error_type="RuntimeError",
                error_message="boom",
                traceback_text="Traceback (most recent call last):\n  File 'app.py'",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], BOOT_PHASE_FAILED)
            self.assertEqual(data["error_type"], "RuntimeError")
            self.assertEqual(data["error_message"], "boom")
            self.assertIn("Traceback", data["traceback"])

    def test_partial_fields_omit_none(self) -> None:
        """部分字段缺失：仅传 phase，其余 None 不应出现在 JSON 中。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(path, BOOT_PHASE_BOOTING)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], BOOT_PHASE_BOOTING)
            self.assertNotIn("step", data)
            self.assertNotIn("error_type", data)
            self.assertNotIn("error_message", data)
            self.assertNotIn("traceback", data)

    def test_atomic_write_no_tmp_leftover(self) -> None:
        """原子写：写入后不应残留 .tmp 临时文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(path, BOOT_PHASE_READY)
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_invalid_phase_is_written_as_is(self) -> None:
        """非法 phase 值：写入器不校验 phase，应原样落盘（由读取侧决定 Unknown）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(path, "not_a_real_phase")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], "not_a_real_phase")

    def test_empty_phase_is_written_as_is(self) -> None:
        """空字符串 phase：边界值应被写入而不抛错。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(path, "")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], "")

    def test_traceback_with_newlines_serializes(self) -> None:
        """traceback 含多行/特殊字符：应正确 JSON 序列化且可回读。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            tb = "line1\nline2\t\"quoted\"\n中文"
            write_bootstate(path, BOOT_PHASE_FAILED, traceback_text=tb)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["traceback"], tb)

    def test_parent_dir_created_if_missing(self) -> None:
        """依赖未就绪：父目录不存在时应自动创建并写入。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "deep" / "boot.json"
            write_bootstate(path, BOOT_PHASE_BOOTING)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], BOOT_PHASE_BOOTING)

    def test_unwritable_parent_dir_does_not_raise(self) -> None:
        """依赖未就绪（父目录不可写）：应捕获 OSError 并输出到 stderr，不抛出。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 构造一个不可写的父目录
            locked_dir = Path(tmp) / "locked"
            locked_dir.mkdir()
            os.chmod(locked_dir, 0o500)
            try:
                path = locked_dir / "boot.json"
                err_stream = io.StringIO()
                with redirect_stderr(err_stream):
                    # 不应抛出
                    write_bootstate(path, BOOT_PHASE_BOOTING)
                self.assertIn("写入启动状态文件失败", err_stream.getvalue())
            finally:
                os.chmod(locked_dir, 0o700)


class ConcurrentBootStateTests(unittest.TestCase):
    """校验并发初始化不会抛出或产生非法 JSON。"""

    def test_concurrent_writes_produce_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            phases = [BOOT_PHASE_BOOTING, BOOT_PHASE_READY, BOOT_PHASE_FAILED]

            def worker(index: int) -> None:
                write_bootstate(
                    path,
                    phases[index % len(phases)],
                    error_type="E" + str(index),
                    error_message="msg" + str(index),
                )

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # 最终文件必须存在且为合法 JSON
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(
                data["phase"],
                [BOOT_PHASE_BOOTING, BOOT_PHASE_READY, BOOT_PHASE_FAILED],
            )


class RedactSensitiveTests(unittest.TestCase):
    """校验落盘前对 error_message / traceback 的敏感信息脱敏。"""

    def test_sk_api_key_is_redacted_in_traceback(self) -> None:
        """traceback 中的 ``sk-`` 密钥不得明文落盘。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            secret = "sk-DEEPSEEKKEY1234567890abcdef"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                traceback_text=f"auth failed with {secret} in call",
            )
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            self.assertIn("sk-[REDACTED]", raw)

    def test_uppercase_sk_key_is_redacted(self) -> None:
        """大写 ``SK-`` 前缀密钥同样需要脱敏。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                error_message="key=SK-ABCDEF123456 leaked",
            )
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("SK-ABCDEF123456", raw)
            self.assertIn("[REDACTED]", raw)

    def test_sensitive_assignment_is_redacted(self) -> None:
        """``api_key=...`` / ``token: ...`` 等敏感赋值需脱敏，普通文本保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                traceback_text='api_key="secret-value-123" password: hunter2 normal ok',
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            tb = data["traceback"]
            self.assertNotIn("secret-value-123", tb)
            self.assertNotIn("hunter2", tb)
            self.assertIn("[REDACTED]", tb)
            self.assertIn("normal ok", tb)

    def test_quoted_value_with_spaces_fully_redacted(self) -> None:
        """引号内含空格的敏感值必须整体脱敏，不得残留任何片段。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                traceback_text='config password="my secret pass phrase" then ok',
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            tb = data["traceback"]
            self.assertNotIn("my secret pass phrase", tb)
            self.assertNotIn("secret pass", tb)
            self.assertNotIn("phrase", tb)
            self.assertIn('password="[REDACTED]"', tb)
            self.assertIn("then ok", tb)

    def test_substring_key_not_over_matched(self) -> None:
        """``token`` 作为 ``tokenizer`` 子串且非赋值时不应被脱敏。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                error_message="loading tokenizer=bert done",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("tokenizer=", data["error_message"])

    def test_unclosed_quote_value_not_leaked(self) -> None:
        """未闭合引号的敏感值不得残留明文（消费到末尾整体脱敏）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                traceback_text='fatal password="oops then rest of line',
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            tb = data["traceback"]
            self.assertNotIn("oops then rest", tb)
            self.assertNotIn("oops", tb)
            # 未闭合引号也补回闭合引号，与 Rust 端形态一致（key="[REDACTED]"）。
            self.assertIn('password="[REDACTED]"', tb)

    def test_sk_key_in_assignment_no_malformed_output(self) -> None:
        """``api_key=sk-...`` 复合场景应完整脱敏且不产生 ``]]`` 畸形输出。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            write_bootstate(
                path,
                BOOT_PHASE_FAILED,
                error_message="auth api_key=REDACTED_DEEPSEEK_KEY extra",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            msg = data["error_message"]
            self.assertNotIn("REDACTED_DEEPSEEK_KEY", msg)
            self.assertNotIn("]]", msg)
            self.assertIn("api_key=[REDACTED]", msg)
            self.assertIn("extra", msg)

    def test_non_sensitive_traceback_preserved(self) -> None:
        """不含敏感信息的 traceback 应原样保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot.json"
            tb = "Traceback (most recent call last):\n  File 'app.py', line 10"
            write_bootstate(path, BOOT_PHASE_FAILED, traceback_text=tb)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["traceback"], tb)


if __name__ == "__main__":
    unittest.main()
