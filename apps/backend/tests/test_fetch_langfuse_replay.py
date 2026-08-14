from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_script_module() -> Any:
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fetch_langfuse_replay.py"
    spec = importlib.util.spec_from_file_location("fetch_langfuse_replay", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_observations_fetches_all_cursor_pages() -> None:
    module = _load_script_module()
    seen_cursors: list[str | None] = []
    pages = [
        {"data": [{"id": "child", "traceId": "trace-1"}], "meta": {"cursor": "next"}},
        {"data": [{"id": "root", "traceId": "trace-1"}], "meta": {}},
    ]

    def fetch_page(cursor: str | None) -> dict[str, Any]:
        seen_cursors.append(cursor)
        return pages.pop(0)

    observations, page_count = module.collect_observations(fetch_page)

    assert page_count == 2
    assert seen_cursors == [None, "next"]
    assert [item["id"] for item in observations] == ["child", "root"]


def test_build_replay_payload_orders_observations_and_builds_tree() -> None:
    module = _load_script_module()
    observations = [
        {
            "id": "child",
            "traceId": "trace-1",
            "parentObservationId": "root",
            "startTime": "2026-08-13T01:00:01Z",
        },
        {
            "id": "root",
            "traceId": "trace-1",
            "parentObservationId": None,
            "startTime": "2026-08-13T01:00:00Z",
        },
    ]

    payload = module.build_replay_payload(
        trace_id="trace-1",
        base_url="https://cloud.langfuse.com",
        fields="core,basic,io",
        observations=observations,
        page_count=1,
    )

    assert payload["trace_id"] == "trace-1"
    assert payload["observation_count"] == 2
    assert [item["id"] for item in payload["observations"]] == ["root", "child"]
    assert payload["tree"]["roots"] == ["root"]
    assert payload["tree"]["logical_roots"] == ["root"]
    assert payload["tree"]["children_by_parent"] == {"root": ["child"]}


def test_build_replay_payload_uses_langfuse_root_flag_for_logical_roots() -> None:
    module = _load_script_module()
    observations = [
        {
            "id": "root",
            "traceId": "trace-1",
            "parentObservationId": "missing-parent",
            "startTime": "2026-08-13T01:00:00Z",
            "isRootObservation": True,
        },
    ]

    payload = module.build_replay_payload(
        trace_id="trace-1",
        base_url="https://cloud.langfuse.com",
        fields="core,basic,io",
        observations=observations,
        page_count=1,
    )

    assert payload["tree"]["roots"] == []
    assert payload["tree"]["logical_roots"] == ["root"]
    assert payload["tree"]["orphan_ids"] == ["root"]


def test_write_replay_json_creates_parent_directory(tmp_path: Path) -> None:
    module = _load_script_module()
    output_path = tmp_path / "nested" / "trace-1.json"

    written = module.write_replay_json(output_path, {"trace_id": "trace-1"})

    assert written == output_path
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"trace_id": "trace-1"}


def test_build_observations_url_rejects_non_http_base_url() -> None:
    module = _load_script_module()

    try:
        module.build_observations_url(
            base_url="file:///tmp/langfuse",
            trace_id="trace-1",
            fields="core",
            limit=100,
            cursor=None,
        )
    except module.LangfuseReplayError as exc:
        assert "must use https" in str(exc)
    else:
        raise AssertionError("expected non-http base URL to be rejected")


def test_build_observations_url_rejects_http_without_explicit_opt_in() -> None:
    module = _load_script_module()

    try:
        module.build_observations_url(
            base_url="http://langfuse.internal",
            trace_id="trace-1",
            fields="core",
            limit=100,
            cursor=None,
            allow_insecure_http=False,
        )
    except module.LangfuseReplayError as exc:
        assert "requires --allow-insecure-http" in str(exc)
    else:
        raise AssertionError("expected insecure HTTP base URL to be rejected")


def test_build_observations_url_allows_http_with_explicit_opt_in() -> None:
    module = _load_script_module()

    url = module.build_observations_url(
        base_url="http://langfuse.internal",
        trace_id="trace-1",
        fields="core",
        limit=100,
        cursor=None,
        allow_insecure_http=True,
    )

    assert url.startswith("http://langfuse.internal/api/public/v2/observations?")


def test_normalize_args_rejects_trace_id_path_traversal() -> None:
    module = _load_script_module()
    parser = module.build_parser()
    args = parser.parse_args(["..\\..\\secret"])

    try:
        module.normalize_args(args)
    except module.LangfuseReplayError as exc:
        assert "trace_id may only contain" in str(exc)
    else:
        raise AssertionError("expected unsafe trace id to be rejected")
