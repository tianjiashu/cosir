from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _repository_root() -> Path:
    """返回测试运行所在仓库根目录。

    参数:
        无。
    返回:
        仓库根目录路径。
    异常:
        无。
    副作用:
        解析当前测试文件路径。
    """

    return Path(__file__).resolve().parents[3]


def _replay_path() -> Path:
    """返回真实 Langfuse replay 测试数据路径。

    参数:
        无。
    返回:
        已由 fetch_langfuse_replay.py 拉取的真实 replay JSON 路径。
    异常:
        无。
    副作用:
        无。
    """

    return (
        _repository_root()
        / "storage"
        / "langfuse-replays"
        / "a27611ce346d45209ff048881bdfbef2.json"
    )


def _load_script_module() -> Any:
    """加载 analyze_langfuse_replay 脚本模块。

    参数:
        无。
    返回:
        已加载的脚本模块对象。
    异常:
        AssertionError: 如果脚本 spec 无法创建。
    副作用:
        从 scripts/analyze_langfuse_replay.py 执行模块加载。
    """

    script_path = _repository_root() / "scripts" / "analyze_langfuse_replay.py"
    spec = importlib.util.spec_from_file_location("analyze_langfuse_replay", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_uses_real_replay_counts() -> None:
    module = _load_script_module()
    replay = module.load_replay(_replay_path())

    summary = module.build_summary(replay)

    assert summary["trace_id"] == "a27611ce346d45209ff048881bdfbef2"
    assert summary["observation_count"] == 18
    assert summary["type_counts"]["GENERATION"] == 3
    assert summary["type_counts"]["CHAIN"] == 15
    assert summary["logical_roots"] == ["e64ae89ba3ec37ce"]
    assert summary["total_usage"] > 0
    assert summary["sum_observation_latency"] == 73.69
    assert summary["trace_wall_time"] == 25.803


def test_timeline_and_hotspots_use_real_replay_order() -> None:
    module = _load_script_module()
    replay = module.load_replay(_replay_path())

    timeline = module.build_timeline(replay)
    hotspots = module.build_hotspots(replay, limit=3)

    assert timeline[0]["id"] == "e64ae89ba3ec37ce"
    assert timeline[0]["name"] == "LangGraph"
    assert len(timeline) == 18
    assert len(hotspots) == 3
    assert hotspots[0]["latency"] >= hotspots[1]["latency"]


def test_hotspots_rejects_negative_limit() -> None:
    module = _load_script_module()
    replay = module.load_replay(_replay_path())

    try:
        module.build_hotspots(replay, limit=-1)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("negative limit should be rejected")


def test_generations_and_inspect_read_real_model_observations() -> None:
    module = _load_script_module()
    replay = module.load_replay(_replay_path())

    generations = module.build_generations(replay)
    inspected = module.inspect_observation(replay, "efc3617ac2739467")

    assert len(generations) == 3
    assert generations[0]["type"] == "GENERATION"
    assert generations[0]["model"] == "deepseek-v4-flash"
    assert inspected["id"] == "efc3617ac2739467"
    assert inspected["type"] == "GENERATION"


def test_export_llm_writes_markdown_from_real_replay(tmp_path: Path) -> None:
    module = _load_script_module()
    replay = module.load_replay(_replay_path())
    output_path = tmp_path / "trace-report.md"

    report = module.build_llm_report(replay, max_chars=8000)
    written = module.write_text_file(output_path, report, force=False)

    assert written == output_path
    content = output_path.read_text(encoding="utf-8")
    assert "a27611ce346d45209ff048881bdfbef2" in content
    assert "DeepSeekChatOpenAI" in content
    assert len(content) <= 8000
    assert not content.rstrip().endswith("[TRUNCATED: report exceeded max_chars]")


def test_export_llm_truncates_on_section_boundary() -> None:
    module = _load_script_module()
    replay = module.load_replay(_replay_path())

    report = module.build_llm_report(replay, max_chars=5000)

    assert len(report) <= 5000
    assert report.rstrip().endswith("[TRUNCATED: omitted sections exceeded max_chars]")
    assert "## Summary" in report
    assert "## Latency Hotspots" in report


def test_generations_text_includes_agent_debug_fields(capsys: Any) -> None:
    module = _load_script_module()

    exit_code = module.main(["generations", str(_replay_path())])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "model=deepseek-v4-flash" in output
    assert "usage=9593" in output
    assert "ttft=0.437" in output


def test_output_cannot_overwrite_input_replay_even_with_force(tmp_path: Path, capsys: Any) -> None:
    module = _load_script_module()
    replay_copy = tmp_path / "replay.json"
    replay_copy.write_text(_replay_path().read_text(encoding="utf-8"), encoding="utf-8")
    original_content = replay_copy.read_text(encoding="utf-8")

    exit_code = module.main(
        [
            "summary",
            str(replay_copy),
            "--format",
            "json",
            "--output",
            str(replay_copy),
            "--force",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "refusing to overwrite replay input file" in captured.err
    assert replay_copy.read_text(encoding="utf-8") == original_content


def test_summary_cli_outputs_json_for_real_replay(capsys: Any) -> None:
    module = _load_script_module()

    exit_code = module.main(["summary", str(_replay_path()), "--format", "json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["observation_count"] == 18
