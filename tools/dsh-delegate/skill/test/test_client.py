"""Client-contract tests for the dsh-delegate SKILL script.

Runs the real CLI against a fake delegate bridge on loopback, so exit codes,
blocking/polling behaviour, and output files are covered without a Harness host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "dsh_delegate.py")
TOKEN = "test-token"

RUNNING = {"jobId": "j1", "sessionId": "s1", "state": "running", "text": "", "pendingInput": []}
DONE = {
    "jobId": "j1",
    "sessionId": "s1",
    "state": "done",
    "text": "final answer",
    "reason": "completed",
    "turn": 1,
    "pendingInput": [],
    "error": None,
}
INPUT_REQUIRED = {
    "jobId": "j1",
    "sessionId": "s1",
    "state": "input-required",
    "text": "",
    "pendingInput": ["approval"],
    "pending": [
        {
            "requestId": "evt-1",
            "kind": "approval",
            "sessionId": "s1",
            "toolName": "bash",
            "requestedScope": "danger-full-access",
            "reason": "escalate sandbox to danger-full-access: writing outside the workspace",
        }
    ],
    "error": None,
}
EXPIRED = {
    "jobId": "j1",
    "sessionId": "s1",
    "state": "expired",
    "text": "",
    "pendingInput": [],
    "extensions": 4,
    "lastObserved": "running",
    "error": {"code": "expired", "message": "The job exceeded its observation window; last observed session state: running."},
}

FAILED = {
    "jobId": "j1",
    "sessionId": "s1",
    "state": "failed",
    "text": "",
    "pendingInput": [],
    "error": {"code": "agent_error", "message": "boom"},
}


class FakeBridge:
    def __init__(self) -> None:
        self.prompt_response = DONE
        self.job_queue: list[dict] = []
        self.last_prompt_body: dict | None = None
        self.last_job_polls = 0
        self.turns = [{"turn": 2, "text": "latest answer", "seq": 10}]
        self.pending_entries = [
            {
                "requestId": "evt-1",
                "kind": "approval",
                "sessionId": "s1",
                "toolName": "bash",
                "callId": "call-1",
                "requestedScope": "danger-full-access",
                "reason": "escalate sandbox to danger-full-access: writing outside the workspace",
            },
            {
                "requestId": "evt-2",
                "kind": "question",
                "sessionId": "s1",
                "questions": [{"id": "q1", "question": "Deploy now?", "options": [{"label": "Yes"}, {"label": "No"}]}],
            },
        ]
        self.answer_body: dict | None = None
        self.answer_caller: str | None = None
        self.answer_status = 200
        self.answer_response: dict | None = None
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep test output quiet
                pass

            def _send(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _authorised(self) -> bool:
                return self.headers.get("authorization") == f"Bearer {TOKEN}"

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                route = parsed.path
                if not self._authorised():
                    self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "no token"}})
                    return
                if route == "/delegate/v1/health":
                    self._send(200, {"ok": True, "pid": 1, "workspaces": 0})
                    return
                if route.startswith("/delegate/v1/jobs/"):
                    bridge.last_job_polls += 1
                    view = bridge.job_queue.pop(0) if bridge.job_queue else DONE
                    self._send(200, view)
                    return
                if route.endswith("/last"):
                    self._send(200, {"sessionId": "s1", "turns": bridge.turns})
                    return
                if route.endswith("/pending"):
                    self._send(200, {"sessionId": "s1", "pending": bridge.pending_entries, "jobId": "j1"})
                    return
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": route}})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self._authorised():
                    self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "no token"}})
                    return
                if parsed.path.endswith("/prompt"):
                    bridge.last_prompt_body = body
                    self._send(202, bridge.prompt_response)
                    return
                if "/pending/" in parsed.path and parsed.path.endswith("/answer"):
                    request_id = parsed.path.split("/pending/")[1].split("/")[0]
                    bridge.answer_body = body
                    bridge.answer_caller = self.headers.get("x-delegate-caller")
                    if bridge.answer_status == 200:
                        response = bridge.answer_response or {
                            "ok": True,
                            "requestId": request_id,
                            "kind": "approval",
                            "decision": (body.get("decision") if "decision" in body else "answered"),
                            "jobId": "j1",
                        }
                        self._send(200, response)
                    else:
                        self._send(
                            bridge.answer_status,
                            {"ok": False, "error": {"code": "request-not-pending", "message": "already handled"}},
                        )
                    return
                self._send(404, {"ok": False, "error": {"code": "not_found", "message": parsed.path}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeBridge":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


class ClientContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def endpoint_file(self, bridge: FakeBridge) -> str:
        path = os.path.join(self.tempdir.name, "delegate.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"origin": bridge.origin, "token": TOKEN, "routePrefix": "/delegate/v1"}, handle)
        return path

    def run_cli(self, endpoint: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, SCRIPT, "--endpoint", endpoint, *argv],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_prompt_done_prints_answer_and_exits_zero(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = DONE
            result = self.run_cli(self.endpoint_file(bridge), "prompt", "--session", "s1", "--wait", "5", "do it")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("final answer", result.stdout)

    def test_prompt_running_exits_pending_then_wait_until_done(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = RUNNING
            endpoint = self.endpoint_file(bridge)
            first = self.run_cli(endpoint, "prompt", "--session", "s1", "--wait", "1", "do it")
            self.assertEqual(first.returncode, 6, first.stderr)
            self.assertIn("job: j1", first.stdout)

            bridge.job_queue = [RUNNING, DONE]
            second = self.run_cli(endpoint, "wait", "--job", "j1", "--until-done", "--wait", "1")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("final answer", second.stdout)
            self.assertGreaterEqual(bridge.last_job_polls, 2)

    def test_input_required_exits_seven_with_first_line(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = INPUT_REQUIRED
            result = self.run_cli(self.endpoint_file(bridge), "prompt", "--session", "s1", "--wait", "1", "do it")
        self.assertEqual(result.returncode, 7, result.stderr)
        first_line = result.stdout.strip().splitlines()[0]
        self.assertEqual(first_line, "input-required: approval")

    def test_failed_exits_five_and_prints_error(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = FAILED
            result = self.run_cli(self.endpoint_file(bridge), "prompt", "--session", "s1", "--wait", "1", "do it")
        self.assertEqual(result.returncode, 5)
        self.assertIn("agent_error", result.stdout)
        self.assertIn("boom", result.stdout)

    def test_expired_exits_five_and_prints_diagnostics(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = EXPIRED
            result = self.run_cli(self.endpoint_file(bridge), "prompt", "--session", "s1", "--wait", "1", "do it")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("[expired]", result.stdout)
        self.assertIn("observation window", result.stdout)
        self.assertIn("extensions: 4", result.stdout)
        self.assertIn("last observed: running", result.stdout)

    def test_expire_flag_is_forwarded_in_milliseconds(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = DONE
            result = self.run_cli(
                self.endpoint_file(bridge), "prompt", "--session", "s1", "--wait", "1", "--expire", "8h", "do it"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bridge.last_prompt_body["expireMs"], 8 * 60 * 60 * 1000)

    def test_out_writes_full_answer_and_truncates_stdout(self) -> None:
        long_text = "x" * 200
        done = dict(DONE, text=long_text)
        with FakeBridge() as bridge:
            bridge.prompt_response = done
            out_path = os.path.join(self.tempdir.name, "answer.txt")
            result = self.run_cli(
                self.endpoint_file(bridge),
                "prompt",
                "--session",
                "s1",
                "--wait",
                "1",
                "--max-chars",
                "20",
                "--out",
                out_path,
                "do it",
            )
            with open(out_path, "r", encoding="utf-8") as handle:
                written = handle.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(written, long_text)
        self.assertIn("[truncated:", result.stdout)

    def test_last_prints_newest_answer(self) -> None:
        with FakeBridge() as bridge:
            bridge.turns = [
                {"turn": 1, "text": "older", "seq": 3},
                {"turn": 2, "text": "latest answer", "seq": 10},
            ]
            result = self.run_cli(self.endpoint_file(bridge), "last", "--session", "s1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "latest answer")

    def test_json_flag_works_before_and_after_subcommand(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = DONE
            endpoint = self.endpoint_file(bridge)
            before = self.run_cli(endpoint, "--json", "prompt", "--session", "s1", "--wait", "1", "do it")
            after = self.run_cli(endpoint, "prompt", "--session", "s1", "--wait", "1", "--json", "do it")
        for result in (before, after):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["state"], "done")

    def test_missing_endpoint_exits_four(self) -> None:
        missing = os.path.join(self.tempdir.name, "nope.json")
        result = self.run_cli(missing, "health")
        self.assertEqual(result.returncode, 4)
        self.assertIn("delegate bridge not found", result.stderr)

    def test_pending_lists_requests(self) -> None:
        with FakeBridge() as bridge:
            result = self.run_cli(self.endpoint_file(bridge), "pending", "--session", "s1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("approval evt-1 tool=bash", result.stdout)
        self.assertIn("danger-full-access", result.stdout)
        self.assertIn("question evt-2 Deploy now?", result.stdout)

    def test_approve_sends_allowed_once_with_caller(self) -> None:
        with FakeBridge() as bridge:
            result = self.run_cli(
                self.endpoint_file(bridge), "approve", "--session", "s1", "--request", "evt-1", "--reason", "scoped write"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bridge.answer_body["decision"], "allowed-once")
        self.assertEqual(bridge.answer_body["reason"], "scoped write")
        self.assertIsNotNone(bridge.answer_caller)

    def test_deny_sends_rejected(self) -> None:
        with FakeBridge() as bridge:
            result = self.run_cli(self.endpoint_file(bridge), "deny", "--session", "s1", "--request", "evt-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bridge.answer_body["decision"], "rejected")

    def test_answer_builds_option_batch(self) -> None:
        with FakeBridge() as bridge:
            result = self.run_cli(
                self.endpoint_file(bridge), "answer", "--session", "s1", "--request", "evt-2", "--option", "Yes"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bridge.answer_body["answer"], {"answers": [{"id": "q1", "selected": ["Yes"]}]})

    def test_conflict_exits_eight(self) -> None:
        with FakeBridge() as bridge:
            bridge.answer_status = 409
            result = self.run_cli(self.endpoint_file(bridge), "approve", "--session", "s1", "--request", "evt-1")
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertIn("request-not-pending", result.stderr)

    def test_input_required_prints_pending_summary(self) -> None:
        with FakeBridge() as bridge:
            bridge.prompt_response = INPUT_REQUIRED
            result = self.run_cli(self.endpoint_file(bridge), "prompt", "--session", "s1", "--wait", "1", "do it")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("input-required: approval", result.stdout)
        self.assertIn("approval evt-1 tool=bash", result.stdout)

    def test_invalid_expire_duration_is_a_usage_error(self) -> None:
        with FakeBridge() as bridge:
            result = self.run_cli(self.endpoint_file(bridge), "prompt", "--session", "s1", "--expire", "soon", "x")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
