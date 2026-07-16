"""针对模型适配器工厂函数的测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config.settings import BackendSettings, default_settings
from app.models.echo import EchoStreamingModelAdapter
from app.models.factory import build_model_adapter, build_openai_compatible_adapter
from app.models.openai_compatible import OpenAICompatibleStreamingAdapter


class ModelFactoryTests(unittest.TestCase):
    """校验模型适配器工厂会使用后端配置。"""

    def test_default_settings_use_echo_provider(self) -> None:
        """校验本地默认值使后端无需 API Key 即可启动。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果默认服务商不是 echo。

        副作用:
            临时清除模型服务商的环境变量覆盖。
        """

        with patch.dict("os.environ", {}, clear=True), patch(
            "app.config.settings.dotenv_values", return_value={}
        ):
            settings = default_settings()

        self.assertEqual(settings.model_provider, "echo")
        self.assertEqual(settings.model_base_url, "https://api.deepseek.com")

    def test_default_settings_switch_to_openai_provider_when_key_exists(self) -> None:
        """校验只要存在 API Key，默认服务商会切到 OpenAI 兼容模型。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果有 Key 时默认仍停留在 echo。

        副作用:
            临时打补丁修改进程环境变量。
        """

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            settings = default_settings()

        self.assertEqual(settings.model_provider, "openai-compatible")
        self.assertEqual(settings.model_thinking_mode, "disabled")

    def test_environment_can_select_openai_compatible_provider(self) -> None:
        """校验环境变量覆盖可以选择 OpenAI 兼容模型。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果配置未反映环境变量的值。

        副作用:
            临时打补丁修改进程环境变量。
        """

        with patch.dict(
            "os.environ",
            {
                "CODING_AGENT_MODEL_PROVIDER": "openai-compatible",
                "CODING_AGENT_MODEL_NAME": "custom-model",
                "CODING_AGENT_MODEL_BASE_URL": "https://example.test/v1",
                "CODING_AGENT_MODEL_API_KEY_ENV": "CUSTOM_KEY",
                "CODING_AGENT_MAX_STEPS": "5",
                "CODING_AGENT_TOOL_ERROR_LIMIT": "2",
                "CODING_AGENT_MAX_CONTEXT_CHARS": "1234",
            },
            clear=True,
        ):
            settings = default_settings()

        self.assertEqual(settings.model_provider, "openai-compatible")
        self.assertEqual(settings.model_name, "custom-model")
        self.assertEqual(settings.model_base_url, "https://example.test/v1")
        self.assertEqual(settings.model_api_key_env, "CUSTOM_KEY")
        self.assertEqual(settings.model_thinking_mode, "disabled")
        self.assertEqual(settings.max_steps, 5)
        self.assertEqual(settings.tool_error_limit, 2)
        self.assertEqual(settings.max_context_chars, 1234)

    def test_invalid_model_provider_is_rejected(self) -> None:
        """校验不受支持的模型服务商会在配置校验时失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非法服务商未抛出 ValueError。

        副作用:
            创建一个临时的配置路径。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                BackendSettings(
                    project_root=root,
                    log_dir=root / "logs",
                    database_file=root / "app.sqlite3",
                    model_provider="unknown",
                )

    def test_invalid_context_budget_is_rejected(self) -> None:
        """校验非正的上下文预算会在配置校验时失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果非法的上下文预算未抛出 ValueError。

        副作用:
            创建一个临时的配置路径。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                BackendSettings(
                    project_root=root,
                    log_dir=root / "logs",
                    database_file=root / "app.sqlite3",
                    max_context_chars=0,
                )

    def test_build_model_adapter_uses_echo_by_default(self) -> None:
        """校验工厂会为默认服务商构建 echo 适配器。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果返回了错误的适配器类型。

        副作用:
            创建一个临时的配置对象。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = BackendSettings(
                project_root=root,
                log_dir=root / "logs",
                database_file=root / "app.sqlite3",
            )

            adapter = build_model_adapter(settings)

        self.assertIsInstance(adapter, EchoStreamingModelAdapter)

    def test_build_model_adapter_uses_openai_compatible_provider(self) -> None:
        """校验在配置好时工厂会构建 OpenAI 兼容适配器。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果返回了错误的适配器类型。

        副作用:
            创建一个临时的配置对象。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = BackendSettings(
                project_root=root,
                log_dir=root / "logs",
                database_file=root / "app.sqlite3",
                model_provider="openai-compatible",
            )

            adapter = build_model_adapter(settings)

        self.assertIsInstance(adapter, OpenAICompatibleStreamingAdapter)

    def test_build_openai_compatible_adapter(self) -> None:
        """校验工厂返回 OpenAI 兼容的流式适配器。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工厂返回了错误的适配器类型。

        副作用:
            创建一个临时的配置对象。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = BackendSettings(
                project_root=root,
                log_dir=root / "logs",
                database_file=root / "app.sqlite3",
            )

            adapter = build_openai_compatible_adapter(settings)

            self.assertIsInstance(adapter, OpenAICompatibleStreamingAdapter)


if __name__ == "__main__":
    unittest.main()
