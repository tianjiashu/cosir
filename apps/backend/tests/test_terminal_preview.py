"""预览重放、实时衔接与背压；协议事件不会与 Agent 观察共用游标。"""

import base64

import pytest
from test_terminal_session_service import make_service

from app.config.constant import Constant
from app.service.terminal.preview import TerminalPreviewSubscription

pytestmark = pytest.mark.usefixtures("backend_env")


def test_replay_then_live_keeps_every_frame_once_and_returns_independent_events(
    monkeypatch, tmp_path
):
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    identity = session["session_id"]
    worker = workers[0]
    try:
        worker.emit_output(b"before")
        attachment = service.subscribe(identity, task_id=1, after_seq=None)
        worker.emit_output(b"during")
        assert attachment.subscription.get(0) is None
        assert [item["seq"] for item in attachment.replay] == [1]
        attachment.replay[0]["seq"] = 999
        other = service.subscribe(identity, task_id=1, after_seq=None)
        assert [item["seq"] for item in other.replay] == [1, 2]
        service.unsubscribe(identity, other.subscription)
        attachment.subscription.activate()
        attachment.subscription.activate()
        worker.emit_output(b"after")
        live = [attachment.subscription.get(0), attachment.subscription.get(0)]
        assert [item["seq"] for item in live] == [2, 3]
        assert [base64.b64decode(item["data_base64"]) for item in live] == [b"during", b"after"]
        service.close(identity, task_id=1)
        assert attachment.subscription.get(0)["type"] == "exit"
        assert attachment.subscription.get(0) is None
    finally:
        service.shutdown()


@pytest.mark.parametrize("activate", [False, True])
@pytest.mark.parametrize("limit", ["frames", "bytes"])
def test_overflow_is_explicit_and_only_affects_the_slow_subscription(monkeypatch, activate, limit):
    monkeypatch.setattr(
        Constant.Terminal, "MAX_PREVIEW_QUEUE_FRAMES", 1 if limit == "frames" else 10
    )
    monkeypatch.setattr(
        Constant.Terminal, "MAX_PREVIEW_QUEUE_BYTES", 1 if limit == "bytes" else 1000
    )
    slow = TerminalPreviewSubscription()
    fast = TerminalPreviewSubscription()
    fast.activate()
    if activate:
        slow.activate()
    for seq in [1, 2]:
        event = {"type": "output", "seq": seq}
        slow.publish(event, 1)
        fast.publish(event, 1)
        assert fast.get(0) == event
    assert slow.overflowed
    slow.activate()
    assert slow.get(0) == {"type": "subscriber_overflow"}
    slow.publish({"type": "output", "seq": 3}, 1)
    assert slow.get(0) is None
    assert not fast.overflowed
    slow.close()
    fast.close()


def test_close_discards_pending_and_live_queue():
    subscription = TerminalPreviewSubscription()
    subscription.publish({"type": "output"}, 1)
    subscription.activate()
    subscription.close()
    subscription.publish({"type": "output"}, 1)
    assert subscription.get(0) is None
