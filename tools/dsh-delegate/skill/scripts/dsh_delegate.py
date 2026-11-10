#!/usr/bin/env python3
"""Client for the DeepSeek Harness delegate bridge.

Talks to the `dsh-delegate` host plugin over the loopback HTTP surface it
publishes at `$DSH_HOME/delegate.json`. Standard library only.

Contract for callers:
  - `prompt` submits one turn and waits up to `--wait` seconds.
  - `wait` continues observing the same job without resubmitting;
    `--until-done` keeps polling until a terminal state or human input is needed.
  - `last` reads the latest persisted answer directly from the session when a
    job view is unavailable.
  - stdout carries only the answer (or a compact JSON view with `--json`);
    diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NO_DSH = 4
EXIT_FAILED = 5
EXIT_PENDING = 6
EXIT_INPUT_REQUIRED = 7
EXIT_CONFLICT = 8

CALLER = os.environ.get("DSH_DELEGATE_CALLER", "dsh_delegate.py")
CONFLICT_CODES = {"request-not-pending", "request-not-found", "answer-failed"}

DEFAULT_WAIT_SECONDS = 60.0
MAX_WAIT_SECONDS = 240.0
DEFAULT_MAX_CHARS = 8000
ROUTE_PREFIX = "/delegate/v1"

TERMINAL_FAILED = {"failed", "cancelled", "lost", "expired", "timeout"}
TERMINAL_OK = {"done"}
ACTIVE_STATES = {"running", "suspended", "cancelling"}


class BridgeError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_FAILED):
        super().__init__(message)
        self.exit_code = exit_code


def parse_duration_ms(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(value))
    if match is None:
        raise BridgeError(f"invalid duration {value!r}; use e.g. 90s, 30m, 8h", EXIT_USAGE)
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    milliseconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * 1000
    return max(1000, int(milliseconds))


def discover_endpoint(override: str | None) -> tuple[str, str, str]:
    url = os.environ.get("DSH_DELEGATE_URL")
    token = os.environ.get("DSH_DELEGATE_TOKEN")
    if url and token:
        return url.rstrip("/"), token, ROUTE_PREFIX

    path = override or os.environ.get("DSH_DELEGATE_ENDPOINT")
    if path is None:
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        path = os.path.join(home, "delegate.json")

    if not os.path.exists(path):
        raise BridgeError(
            f"delegate bridge not found at {path}. Is the DeepSeek Harness desktop app running "
            "and the dsh-delegate plugin installed? Set DSH_DELEGATE_URL/DSH_DELEGATE_TOKEN to override.",
            EXIT_NO_DSH,
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as error:
        raise BridgeError(f"cannot read delegate endpoint file {path}: {error}", EXIT_NO_DSH) from error

    origin = document.get("origin")
    token = document.get("token")
    route_prefix = document.get("routePrefix") or ROUTE_PREFIX
    if not isinstance(origin, str) or not isinstance(token, str) or not isinstance(route_prefix, str):
        raise BridgeError(f"delegate endpoint file {path} is malformed", EXIT_NO_DSH)
    return origin.rstrip("/"), token, route_prefix.rstrip("/")


def request_json(
    endpoint: tuple[str, str, str],
    method: str,
    route: str,
    body: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    origin, token, route_prefix = endpoint
    url = f"{origin}{route_prefix}{route}"
    data = None
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "x-delegate-caller": CALLER,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
            detail = parsed.get("error") or {}
            code = detail.get("code") or f"http_{error.code}"
            message = detail.get("message") or raw.decode("utf-8", "replace")
        except ValueError:
            code = f"http_{error.code}"
            message = raw.decode("utf-8", "replace")
        if error.code in (401, 403):
            exit_code = EXIT_AUTH
        elif code in CONFLICT_CODES:
            exit_code = EXIT_CONFLICT
        else:
            exit_code = EXIT_FAILED
        raise BridgeError(f"{code}: {message}", exit_code) from error
    except urllib.error.URLError as error:
        raise BridgeError(
            f"cannot reach the delegate bridge at {origin}: {error.reason}. Is the DeepSeek Harness app running?",
            EXIT_NO_DSH,
        ) from error
    except TimeoutError as error:
        raise BridgeError(f"request to {url} timed out", EXIT_PENDING) from error
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError as error:
        raise BridgeError(f"delegate bridge returned non-JSON: {payload[:200]!r}") from error


def clamp_wait(value: float | None) -> float:
    if value is None:
        return DEFAULT_WAIT_SECONDS
    return max(1.0, min(float(value), MAX_WAIT_SECONDS))


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated: {len(text) - limit} more chars; rerun with --out PATH or a larger --max-chars]"


def write_answer_file(path: str, text: str) -> int:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(text)


def format_pending_line(entry: dict) -> str:
    request_id = entry.get("requestId", "?")
    kind = entry.get("kind", "?")
    if kind == "approval":
        scope = entry.get("requestedScope")
        tool = entry.get("toolName") or "?"
        reason = entry.get("reason") or ""
        suffix = f" scope={scope}" if scope else ""
        return f"- approval {request_id} tool={tool}{suffix} {reason}".rstrip()
    if kind == "question":
        questions = entry.get("questions") or []
        text = questions[0].get("question") if questions else ""
        return f"- question {request_id} {text or ''}".rstrip()
    return f"- {kind} {request_id}"


def format_input_required(view: dict) -> str:
    pending = view.get("pendingInput") or []
    kinds = ",".join(str(kind) for kind in pending) or "approval-or-question"
    return f"input-required: {kinds}"


def print_job(view: dict, as_json: bool, max_chars: int, out_path: str | None) -> int:
    state = str(view.get("state"))
    text = view.get("text") or ""
    if as_json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
    elif state in TERMINAL_OK:
        if out_path:
            written = write_answer_file(out_path, text)
            print(truncate(text, max_chars))
            print(f"[full answer: {out_path} ({written} chars)]", file=sys.stderr)
        else:
            print(truncate(text, max_chars))
    elif state == "input-required":
        print(format_input_required(view))
        for entry in view.get("pending") or []:
            print(format_pending_line(entry))
        print(f"job: {view.get('jobId')}  session: {view.get('sessionId')}", file=sys.stderr)
        print(
            "The turn is waiting for a human in the Harness GUI. After it is handled, "
            "continue with: dsh_delegate.py wait --job <jobId> --until-done",
            file=sys.stderr,
        )
    elif state in TERMINAL_FAILED:
        error = view.get("error") or {}
        print(f"[{state}] {error.get('code') or ''} {error.get('message') or ''}".strip())
        if state in ("lost", "expired"):
            print(
                f"extensions: {view.get('extensions', 0)}  "
                f"last observed: {view.get('lastObserved') or 'unknown'}"
            )
        print(f"job: {view.get('jobId')}  session: {view.get('sessionId')}", file=sys.stderr)
    else:
        print(f"[{state}] job: {view.get('jobId')}  session: {view.get('sessionId')}")
        print(
            "Not finished yet. Continue with: dsh_delegate.py wait --job <jobId> --until-done "
            "(or --wait 90 for one bounded window).",
            file=sys.stderr,
        )
    if state in TERMINAL_OK:
        return EXIT_OK
    if state in TERMINAL_FAILED:
        return EXIT_FAILED
    if state == "input-required":
        return EXIT_INPUT_REQUIRED
    return EXIT_PENDING


def job_wait(endpoint: tuple[str, str, str], job_id: str, wait_seconds: float) -> dict:
    chunk = clamp_wait(wait_seconds)
    return request_json(
        endpoint,
        "GET",
        f"/jobs/{urllib.parse.quote(job_id)}?waitMs={int(chunk * 1000)}",
        timeout=chunk + 20.0,
    )


def wait_until_settled(endpoint: tuple[str, str, str], job_id: str, chunk_seconds: float) -> dict:
    """Poll one job until a terminal state or a human decision is needed."""
    while True:
        view = job_wait(endpoint, job_id, chunk_seconds)
        state = str(view.get("state"))
        if state in TERMINAL_OK or state in TERMINAL_FAILED or state == "input-required":
            return view
        # running / suspended / cancelling: keep observing the same job.


def read_text_argument(value: str) -> str:
    if value == "-":
        return sys.stdin.read()
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh_delegate.py",
        description="Drive DeepSeek Harness workspaces and sessions from the command line.",
    )
    parser.add_argument("--endpoint", help="Path to the delegate endpoint JSON (defaults to $DSH_HOME/delegate.json).")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of plain text.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Check that the bridge is reachable.")

    workspaces = sub.add_parser("workspaces", help="List workspaces with session counts.")
    workspaces.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    create_workspace = sub.add_parser("workspace-create", help="Create a workspace for an existing or new directory.")
    create_workspace.add_argument("path")
    create_workspace.add_argument("--title")
    create_workspace.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sessions = sub.add_parser("sessions", help="List sessions, optionally for one workspace.")
    sessions.add_argument("--workspace")
    sessions.add_argument("--limit", type=int, default=100)
    sessions.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    create_session = sub.add_parser("session-create", help="Create a session in a workspace or directory.")
    create_session.add_argument("--workspace")
    create_session.add_argument("--cwd")
    create_session.add_argument("--preset")
    create_session.add_argument("--title")
    create_session.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    prompt = sub.add_parser("prompt", help="Submit one prompt and wait for the turn's answer.")
    prompt.add_argument("--session", required=True)
    prompt.add_argument("--permission", help="Permission preset, e.g. workspace-write or danger-full-access.")
    prompt.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS, help="Seconds to wait before returning a job id.")
    prompt.add_argument("--until-done", action="store_true", help="Keep polling until the job is terminal or needs a human.")
    prompt.add_argument("--expire", help="Observation window for this job, e.g. 30m, 8h (default 12h, max 24h).")
    prompt.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    prompt.add_argument("--out", help="Write the full answer to this file; stdout keeps the truncated view.")
    prompt.add_argument("text", help="Prompt text, or - to read stdin.")
    prompt.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    wait = sub.add_parser("wait", help="Continue waiting for a job created by prompt.")
    wait.add_argument("--job", required=True)
    wait.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
    wait.add_argument("--until-done", action="store_true", help="Keep polling until the job is terminal or needs a human.")
    wait.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    wait.add_argument("--out", help="Write the full answer to this file; stdout keeps the truncated view.")
    wait.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    cancel = sub.add_parser("cancel", help="Cancel the active turn of a session.")
    cancel.add_argument("--session", required=True)
    cancel.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    job_cancel = sub.add_parser("job-cancel", help="Cancel the turn behind a delegated job.")
    job_cancel.add_argument("--job", required=True)
    job_cancel.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    state = sub.add_parser("state", help="Read passive state for a session.")
    state.add_argument("--session", required=True)
    state.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    pending = sub.add_parser("pending", help="List pending approvals and questions for a session.")
    pending.add_argument("--session", required=True)
    pending.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    approve = sub.add_parser("approve", help="Allow one pending approval once.")
    approve.add_argument("--session", required=True)
    approve.add_argument("--request", required=True)
    approve.add_argument("--reason", help="Audit-only note; DSH approval outcomes carry no free text.")
    approve.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    deny = sub.add_parser("deny", help="Reject one pending approval.")
    deny.add_argument("--session", required=True)
    deny.add_argument("--request", required=True)
    deny.add_argument("--reason", help="Audit-only note; the Agent receives DSH's standard rejection message.")
    deny.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    answer = sub.add_parser("answer", help="Answer one pending question.")
    answer.add_argument("--session", required=True)
    answer.add_argument("--request", required=True)
    answer.add_argument("--question", help="Question id; defaults to the first question in the request.")
    answer.add_argument("--option", action="append", default=[], help="Option label (repeatable).")
    answer.add_argument("--custom", help="Custom text answer.")
    answer.add_argument("--answers-file", help="JSON file holding the complete answers batch.")
    answer.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    last = sub.add_parser("last", help="Read the latest assistant answer(s) directly from a session.")
    last.add_argument("--session", required=True)
    last.add_argument("--turns", type=int, default=1, help="How many recent turns to return (max 20).")
    last.add_argument("--out", help="Write the newest answer to this file.")
    last.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    presets = sub.add_parser("presets", help="List permission presets and agent presets.")
    presets.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    try:
        endpoint = discover_endpoint(args.endpoint)
        if args.command == "health":
            view = request_json(endpoint, "GET", "/health")
            print(json.dumps(view, ensure_ascii=False, indent=2) if as_json else f"ok pid={view.get('pid')} workspaces={view.get('workspaces')}")
            return EXIT_OK

        if args.command == "workspaces":
            view = request_json(endpoint, "GET", "/workspaces")
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
                return EXIT_OK
            rows = view.get("workspaces") or []
            if not rows:
                print("no workspaces")
                return EXIT_OK
            for row in rows:
                print(f"{row.get('id')}\t{row.get('sessionCount')}\t{row.get('title')}\t{row.get('path')}")
            return EXIT_OK

        if args.command == "workspace-create":
            body = {"path": args.path}
            if args.title:
                body["title"] = args.title
            view = request_json(endpoint, "POST", "/workspaces", body)
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
            else:
                workspace = view.get("workspace") or {}
                print(f"{workspace.get('id')}\t{workspace.get('title')}\t{workspace.get('path')}")
            return EXIT_OK

        if args.command == "sessions":
            query = [f"limit={max(1, args.limit)}"]
            if args.workspace:
                query.append(f"workspaceId={urllib.parse.quote(args.workspace)}")
            view = request_json(endpoint, "GET", "/sessions?" + "&".join(query))
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
                return EXIT_OK
            rows = view.get("sessions") or []
            if not rows:
                print("no sessions")
                return EXIT_OK
            for row in rows:
                flags = []
                if row.get("running"):
                    flags.append("running")
                if row.get("blank"):
                    flags.append("blank")
                if row.get("archived"):
                    flags.append("archived")
                print(f"{row.get('sessionId')}\t{row.get('updatedAt')}\t{','.join(flags) or '-'}")
            return EXIT_OK

        if args.command == "session-create":
            body = {}
            if args.workspace:
                body["workspaceId"] = args.workspace
            if args.cwd:
                body["cwd"] = args.cwd
            if args.preset:
                body["agentPreset"] = args.preset
            if args.title:
                body["title"] = args.title
            view = request_json(endpoint, "POST", "/sessions", body)
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
            else:
                print(view.get("sessionId"))
            return EXIT_OK

        if args.command == "prompt":
            expire_ms = parse_duration_ms(args.expire)
            body = {
                "text": read_text_argument(args.text),
                "waitMs": int(clamp_wait(args.wait) * 1000),
            }
            if args.permission:
                body["permission"] = args.permission
            if expire_ms is not None:
                body["expireMs"] = expire_ms
            view = request_json(
                endpoint,
                "POST",
                f"/sessions/{urllib.parse.quote(args.session)}/prompt",
                body,
                timeout=clamp_wait(args.wait) + 20.0,
            )
            if args.until_done:
                state = str(view.get("state"))
                if state in ACTIVE_STATES:
                    print(f"submitted: job {view.get('jobId')}", file=sys.stderr)
                    view = wait_until_settled(endpoint, str(view.get("jobId")), clamp_wait(args.wait))
            return print_job(view, as_json, args.max_chars, args.out)

        if args.command == "wait":
            if args.until_done:
                view = wait_until_settled(endpoint, args.job, clamp_wait(args.wait))
            else:
                view = job_wait(endpoint, args.job, clamp_wait(args.wait))
            return print_job(view, as_json, args.max_chars, args.out)

        if args.command == "cancel":
            view = request_json(endpoint, "POST", f"/sessions/{urllib.parse.quote(args.session)}/cancel", {})
            print(json.dumps(view, ensure_ascii=False, indent=2) if as_json else "cancel requested")
            return EXIT_OK

        if args.command == "job-cancel":
            view = request_json(endpoint, "POST", f"/jobs/{urllib.parse.quote(args.job)}/cancel", {})
            print(json.dumps(view, ensure_ascii=False, indent=2) if as_json else "cancel requested")
            return EXIT_OK

        if args.command == "state":
            view = request_json(endpoint, "GET", f"/sessions/{urllib.parse.quote(args.session)}")
            print(json.dumps(view, ensure_ascii=False, indent=2))
            return EXIT_OK

        if args.command == "pending":
            view = request_json(endpoint, "GET", f"/sessions/{urllib.parse.quote(args.session)}/pending")
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
                return EXIT_OK
            rows = view.get("pending") or []
            if not rows:
                print("no pending requests")
                return EXIT_OK
            for entry in rows:
                print(format_pending_line(entry))
            print(f"job: {view.get('jobId') or '-'}", file=sys.stderr)
            return EXIT_OK

        if args.command in ("approve", "deny"):
            body = {"decision": "allowed-once" if args.command == "approve" else "rejected"}
            if args.reason:
                body["reason"] = args.reason
            view = request_json(
                endpoint,
                "POST",
                f"/sessions/{urllib.parse.quote(args.session)}/pending/{urllib.parse.quote(args.request)}/answer",
                body,
            )
            print(json.dumps(view, ensure_ascii=False, indent=2) if as_json else f"{view.get('decision')} {args.request}")
            return EXIT_OK

        if args.command == "answer":
            if args.answers_file:
                with open(args.answers_file, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                value = loaded.get("answer", loaded)
            else:
                question_id = args.question
                if question_id is None:
                    listing = request_json(endpoint, "GET", f"/sessions/{urllib.parse.quote(args.session)}/pending")
                    entry = next(
                        (row for row in listing.get("pending") or [] if row.get("requestId") == args.request),
                        None,
                    )
                    if entry is None:
                        raise BridgeError("request-not-pending: the request is not pending", EXIT_CONFLICT)
                    questions = entry.get("questions") or []
                    if questions:
                        question_id = questions[0].get("id")
                if question_id is None:
                    raise BridgeError("cannot determine the question id; pass --question or --answers-file", EXIT_USAGE)
                if not args.option and not args.custom:
                    raise BridgeError("pass --option and/or --custom, or --answers-file", EXIT_USAGE)
                item = {"id": question_id, "selected": list(args.option)}
                if args.custom:
                    item["custom"] = args.custom
                value = {"answers": [item]}
            view = request_json(
                endpoint,
                "POST",
                f"/sessions/{urllib.parse.quote(args.session)}/pending/{urllib.parse.quote(args.request)}/answer",
                {"answer": value},
            )
            print(json.dumps(view, ensure_ascii=False, indent=2) if as_json else f"answered {args.request}")
            return EXIT_OK

        if args.command == "last":
            turns = max(1, min(args.turns, 20))
            view = request_json(
                endpoint,
                "GET",
                f"/sessions/{urllib.parse.quote(args.session)}/last?turns={turns}",
            )
            rows = view.get("turns") or []
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
                return EXIT_OK
            if not rows:
                print("no assistant answer recorded yet")
                return EXIT_FAILED
            newest = rows[-1]
            text = newest.get("text") or ""
            if args.out:
                written = write_answer_file(args.out, text)
                print(truncate(text, DEFAULT_MAX_CHARS))
                print(f"[full answer: {args.out} ({written} chars)]", file=sys.stderr)
            else:
                print(text)
            return EXIT_OK

        if args.command == "presets":
            view = request_json(endpoint, "GET", "/presets")
            if as_json:
                print(json.dumps(view, ensure_ascii=False, indent=2))
                return EXIT_OK
            permissions = view.get("permissions") or {}
            print("permission presets:")
            for option in permissions.get("options") or []:
                print(f"  {option.get('value')}\t{option.get('name')}")
            print("agent presets:")
            for preset in view.get("agentPresets") or []:
                print(f"  {preset.get('id')}")
            return EXIT_OK

        parser.error(f"unknown command {args.command}")
        return EXIT_USAGE
    except BridgeError as error:
        print(f"dsh_delegate: {error}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
