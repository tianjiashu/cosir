#!/usr/bin/env python3
"""Fetch a full Langfuse trace replay as local JSON.

Single responsibility: call Langfuse's public observations API for one trace id, follow
cursor pagination until all observation rows are fetched, rebuild a lightweight local tree, and
persist the replay payload as JSON for later LLM-assisted debugging.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://cloud.langfuse.com"
DEFAULT_FIELDS = "core,basic,io,metadata,model,usage,metrics,trace_context,scores"
DEFAULT_LIMIT = 1000
MAX_LIMIT = 1000
TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class LangfuseReplayError(RuntimeError):
    """Raised when the replay fetch cannot proceed or cannot be completed."""


def repository_root() -> Path:
    """Return the repository root inferred from this script location.

    Args:
        None.

    Returns:
        Absolute path to the repository root.

    Raises:
        None.

    Side Effects:
        Resolves the current script path.
    """

    return Path(__file__).resolve().parent.parent


def default_output_path(trace_id: str) -> Path:
    """Build the default local replay JSON path for a trace id.

    Args:
        trace_id: Langfuse trace id used in the output filename.

    Returns:
        Path under ``storage/langfuse-replays`` with a ``.json`` suffix.

    Raises:
        None.

    Side Effects:
        None.
    """

    return repository_root() / "storage" / "langfuse-replays" / f"{trace_id}.json"


def load_local_env() -> None:
    """Load known local dotenv files without overriding existing environment variables.

    Args:
        None.

    Returns:
        None.

    Raises:
        OSError: If an env file exists but cannot be read.

    Side Effects:
        Adds missing keys from ``.env`` and ``.env.local`` files to ``os.environ``.
    """

    root = repository_root()
    for env_path in (
        root / ".env",
        root / "apps" / "backend" / ".env",
        root / ".env.local",
        root / "apps" / "backend" / ".env.local",
    ):
        if env_path.exists():
            _load_env_file(env_path)


def _load_env_file(env_path: Path) -> None:
    """Load simple KEY=VALUE entries from one dotenv file.

    Args:
        env_path: Dotenv file to parse.

    Returns:
        None.

    Raises:
        OSError: If the file cannot be read.

    Side Effects:
        Sets missing environment variables in the current process.
    """

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _clean_env_value(raw_value.strip())


def _clean_env_value(value: str) -> str:
    """Remove matching single or double quotes around an env value.

    Args:
        value: Raw dotenv value text.

    Returns:
        Unquoted value when the first and last characters are matching quotes.

    Raises:
        None.

    Side Effects:
        None.
    """

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def resolve_config(args: argparse.Namespace) -> tuple[str, str, str]:
    """Resolve Langfuse base URL and credentials from CLI args or environment.

    Args:
        args: Parsed CLI arguments.

    Returns:
        ``(base_url, public_key, secret_key)``.

    Raises:
        LangfuseReplayError: If either credential is missing.

    Side Effects:
        Reads process environment variables.
    """

    base_url = (
        args.base_url
        or os.environ.get("CODING_AGENT_LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_BASE_URL")
        or DEFAULT_BASE_URL
    )
    public_key = (
        args.public_key
        or os.environ.get("CODING_AGENT_LANGFUSE_PUBLIC_KEY")
        or os.environ.get("LANGFUSE_PUBLIC_KEY")
        or ""
    )
    secret_key = (
        args.secret_key
        or os.environ.get("CODING_AGENT_LANGFUSE_SECRET_KEY")
        or os.environ.get("LANGFUSE_SECRET_KEY")
        or ""
    )
    if not public_key or not secret_key:
        raise LangfuseReplayError(
            "missing Langfuse credentials: set CODING_AGENT_LANGFUSE_PUBLIC_KEY and "
            "CODING_AGENT_LANGFUSE_SECRET_KEY, or pass --public-key/--secret-key"
        )
    return base_url.rstrip("/"), public_key, secret_key


def build_observations_url(
    *,
    base_url: str,
    trace_id: str,
    fields: str,
    limit: int,
    cursor: str | None,
    allow_insecure_http: bool = False,
) -> str:
    """Build the Langfuse observations API URL for one page.

    Args:
        base_url: Langfuse host without a trailing slash.
        trace_id: Trace id to filter observations by.
        fields: Comma-separated field groups requested from Langfuse.
        limit: Page size, capped by the caller.
        cursor: Optional pagination cursor from the previous response.
        allow_insecure_http: Whether explicit HTTP transport is allowed.

    Returns:
        Fully encoded observations API URL.

    Raises:
        None.

    Side Effects:
        None.
    """

    parsed_base_url = urllib.parse.urlparse(base_url)
    if parsed_base_url.scheme == "http" and not allow_insecure_http:
        raise LangfuseReplayError("HTTP Langfuse base URL requires --allow-insecure-http")
    if parsed_base_url.scheme != "https" and not (
        parsed_base_url.scheme == "http" and allow_insecure_http
    ):
        raise LangfuseReplayError("Langfuse base URL must use https")

    params = {
        "traceId": trace_id,
        "fields": fields,
        "limit": str(limit),
    }
    if cursor:
        params["cursor"] = cursor
    return f"{base_url}/api/public/v2/observations?{urllib.parse.urlencode(params)}"


def fetch_observations_page(
    *,
    base_url: str,
    trace_id: str,
    fields: str,
    limit: int,
    cursor: str | None,
    public_key: str,
    secret_key: str,
    timeout_seconds: float,
    allow_insecure_http: bool,
) -> dict[str, Any]:
    """Fetch one observations page from Langfuse.

    Args:
        base_url: Langfuse host without trailing slash.
        trace_id: Trace id to filter observations by.
        fields: Comma-separated field groups requested from Langfuse.
        limit: Page size.
        cursor: Optional pagination cursor.
        public_key: Langfuse public key used as Basic Auth username.
        secret_key: Langfuse secret key used as Basic Auth password.
        timeout_seconds: HTTP timeout in seconds.
        allow_insecure_http: Whether explicit HTTP transport is allowed.

    Returns:
        Decoded JSON response dictionary.

    Raises:
        LangfuseReplayError: If the HTTP request fails or the response is not JSON.

    Side Effects:
        Performs one network request to Langfuse.
    """

    url = build_observations_url(
        base_url=base_url,
        trace_id=trace_id,
        fields=fields,
        limit=limit,
        cursor=cursor,
        allow_insecure_http=allow_insecure_http,
    )
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    request = urllib.request.Request(  # noqa: S310 - base_url scheme is validated above.
        url,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "coding-agent-langfuse-replay-fetcher/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - request URL is built from validated base_url.
            request,
            timeout=timeout_seconds,
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        raise LangfuseReplayError(
            f"Langfuse API returned HTTP {exc.code} for trace {trace_id}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LangfuseReplayError(f"Langfuse API request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LangfuseReplayError("Langfuse API request timed out") from exc

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LangfuseReplayError("Langfuse API returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise LangfuseReplayError("Langfuse API returned a non-object JSON payload")
    return decoded


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    """Read and shorten an HTTP error body for diagnostics.

    Args:
        exc: HTTPError raised by urllib.

    Returns:
        Human-readable body excerpt.

    Raises:
        None.

    Side Effects:
        Reads the error response stream.
    """

    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return "<unable to read response body>"
    return body[:1000] if body else "<empty response body>"


def collect_observations(
    fetch_page: Callable[[str | None], dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Fetch every observations page using a caller-provided page function.

    Args:
        fetch_page: Function accepting a cursor and returning one Langfuse page payload.

    Returns:
        ``(observations, page_count)`` containing all rows in fetched order.

    Raises:
        LangfuseReplayError: If the response shape is invalid.

    Side Effects:
        Delegates network or other I/O to ``fetch_page``.
    """

    cursor: str | None = None
    observations: list[dict[str, Any]] = []
    page_count = 0
    while True:
        page = fetch_page(cursor)
        page_count += 1
        data = page.get("data", [])
        if not isinstance(data, list):
            raise LangfuseReplayError("Langfuse observations response has non-list data")
        for item in data:
            if not isinstance(item, dict):
                raise LangfuseReplayError("Langfuse observations response contains non-object row")
            observations.append(item)
        meta = page.get("meta", {})
        if not isinstance(meta, dict):
            raise LangfuseReplayError("Langfuse observations response has non-object meta")
        next_cursor = meta.get("cursor")
        cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
        if not cursor:
            return observations, page_count


def build_replay_payload(
    *,
    trace_id: str,
    base_url: str,
    fields: str,
    observations: list[dict[str, Any]],
    page_count: int,
) -> dict[str, Any]:
    """Build the persisted replay JSON payload.

    Args:
        trace_id: Langfuse trace id used for the query.
        base_url: Langfuse host queried.
        fields: Field groups requested from Langfuse.
        observations: Raw observation rows returned by Langfuse.
        page_count: Number of API pages fetched.

    Returns:
        JSON-serializable replay payload containing metadata, rows, and local tree indexes.

    Raises:
        None.

    Side Effects:
        Reads current UTC time.
    """

    ordered = sorted(observations, key=_observation_sort_key)
    tree = build_observation_tree(ordered)
    return {
        "schema_version": 1,
        "source": "langfuse_observations_api_v2",
        "trace_id": trace_id,
        "base_url": base_url,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "fields": fields,
        "page_count": page_count,
        "observation_count": len(ordered),
        "tree": tree,
        "observations": ordered,
    }


def _observation_sort_key(observation: dict[str, Any]) -> tuple[str, str]:
    """Return a stable chronological sort key for an observation row.

    Args:
        observation: Langfuse observation dictionary.

    Returns:
        Tuple of ``(start_time, id)`` using empty strings for missing values.

    Raises:
        None.

    Side Effects:
        None.
    """

    start_time = observation.get("startTime") or observation.get("start_time") or ""
    observation_id = observation.get("id") or ""
    return str(start_time), str(observation_id)


def build_observation_tree(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build local parent-child indexes from observation rows.

    Args:
        observations: Observation rows sorted in the desired display order.

    Returns:
        Dictionary with root observation ids and children grouped by parent observation id.

    Raises:
        None.

    Side Effects:
        None.
    """

    ids = {str(item.get("id")) for item in observations if item.get("id")}
    roots: list[str] = []
    logical_roots: list[str] = []
    children_by_parent: dict[str, list[str]] = {}
    orphan_ids: list[str] = []
    for item in observations:
        observation_id = item.get("id")
        if not observation_id:
            continue
        item_id = str(observation_id)
        is_logical_root = item.get("isRootObservation") is True
        parent = item.get("parentObservationId") or item.get("parent_observation_id")
        if not parent:
            roots.append(item_id)
            logical_roots.append(item_id)
            continue
        if is_logical_root:
            logical_roots.append(item_id)
        parent_id = str(parent)
        children_by_parent.setdefault(parent_id, []).append(item_id)
        if parent_id not in ids:
            orphan_ids.append(item_id)
    return {
        "roots": roots,
        "logical_roots": logical_roots,
        "children_by_parent": children_by_parent,
        "orphan_ids": orphan_ids,
    }


def write_replay_json(output_path: Path, payload: dict[str, Any]) -> Path:
    """Persist replay payload to a UTF-8 JSON file.

    Args:
        output_path: Destination JSON path.
        payload: JSON-serializable replay payload.

    Returns:
        The destination path.

    Raises:
        OSError: If directory creation or file writing fails.
        TypeError: If payload is not JSON serializable.

    Side Effects:
        Creates parent directories and writes the replay JSON file.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Args:
        None.

    Returns:
        Configured ``ArgumentParser``.

    Raises:
        None.

    Side Effects:
        None.
    """

    parser = argparse.ArgumentParser(
        prog="fetch_langfuse_replay",
        description="Fetch all Langfuse observations for one trace and save a local JSON replay.",
    )
    parser.add_argument("trace_id", help="Langfuse trace id to fetch.")
    parser.add_argument("--output", default="", help="Output JSON path.")
    parser.add_argument("--base-url", default="", help="Langfuse base URL.")
    parser.add_argument("--public-key", default="", help="Langfuse public key.")
    parser.add_argument("--secret-key", default="", help="Langfuse secret key.")
    parser.add_argument("--fields", default=DEFAULT_FIELDS, help="Observation field groups.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Page size, max 1000.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow sending Langfuse Basic Auth credentials over plain HTTP.",
    )
    return parser


def normalize_args(args: argparse.Namespace) -> None:
    """Validate and normalize parsed arguments in place.

    Args:
        args: Parsed CLI arguments.

    Returns:
        None.

    Raises:
        LangfuseReplayError: If trace id, limit, timeout, or fields are invalid.

    Side Effects:
        Mutates ``args.trace_id`` and ``args.fields`` by trimming whitespace.
    """

    args.trace_id = args.trace_id.strip()
    args.fields = args.fields.strip()
    if not args.trace_id:
        raise LangfuseReplayError("trace_id must not be blank")
    if not TRACE_ID_PATTERN.fullmatch(args.trace_id):
        raise LangfuseReplayError(
            "trace_id may only contain letters, numbers, hyphen, and underscore"
        )
    if not args.fields:
        raise LangfuseReplayError("fields must not be blank")
    if args.limit < 1 or args.limit > MAX_LIMIT:
        raise LangfuseReplayError(f"limit must be between 1 and {MAX_LIMIT}")
    if args.timeout <= 0:
        raise LangfuseReplayError("timeout must be greater than zero")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for fetching and persisting one Langfuse trace replay.

    Args:
        argv: Optional argument vector for tests; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code, ``0`` on success and ``1`` on user-facing failure.

    Raises:
        None.

    Side Effects:
        Reads env files, performs Langfuse API requests, writes JSON, and prints status.
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_local_env()
        normalize_args(args)
        base_url, public_key, secret_key = resolve_config(args)
        output_path = (
            Path(args.output).expanduser() if args.output else default_output_path(args.trace_id)
        )

        def fetch_page(cursor: str | None) -> dict[str, Any]:
            """Fetch one page using normalized CLI configuration.

            Args:
                cursor: Optional pagination cursor.

            Returns:
                Decoded Langfuse page.

            Raises:
                LangfuseReplayError: If the request fails.

            Side Effects:
                Performs one HTTP request.
            """

            return fetch_observations_page(
                base_url=base_url,
                trace_id=args.trace_id,
                fields=args.fields,
                limit=args.limit,
                cursor=cursor,
                public_key=public_key,
                secret_key=secret_key,
                timeout_seconds=args.timeout,
                allow_insecure_http=args.allow_insecure_http,
            )

        observations, page_count = collect_observations(fetch_page)
        payload = build_replay_payload(
            trace_id=args.trace_id,
            base_url=base_url,
            fields=args.fields,
            observations=observations,
            page_count=page_count,
        )
        written = write_replay_json(output_path, payload)
    except (LangfuseReplayError, OSError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"saved {payload['observation_count']} observations from {page_count} page(s) to {written}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
