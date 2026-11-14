"""Electron 验收专用：向隔离数据根写入真实 context / Run，不构造 wire 快照。"""

# ruff: noqa: E402
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 此脚本只由测试在临时数据根下使用，绝不回落到用户数据。
assert os.environ["CODING_AGENT_DATA_DIR"]
from app.models.conversation_input import UserInputPresentation
from app.models.message_metadata import AssistantMessageMetadata, UserMessageMetadata
from app.models.session_context import SessionContextRecord
from app.service.depends import (
    get_run_crud,
    get_session_context_crud,
    get_session_crud,
)
from app.storage.store_engines import init_storage

init_storage()
session_id = int(sys.argv[1])
reason = sys.argv[2]
runs = get_run_crud()
run = runs.create(session_id, status="running")
get_session_crud().set_current_run_id(session_id, run.id)
context = get_session_context_crud()
context.create(
    SessionContextRecord(
        session_id=session_id,
        run_id=run.id,
        sequence=1,
        include_in_context=True,
        message={"role": "user", "content": "验收真实时间线"},
        metadata=UserMessageMetadata(
            kind="user_input", presentation=UserInputPresentation(text="验收真实时间线")
        ),
        turn=1,
    )
)
responses = [(2, "真实后端进展", None), (3, "真实后端结论", reason)]
if len(sys.argv) > 3 and sys.argv[3] == "plain":
    responses = [(2, "真实后端结论", reason)]
for seq, text, finish in responses:
    context.create(
        SessionContextRecord(
            session_id=session_id,
            run_id=run.id,
            sequence=seq,
            include_in_context=finish is not None,
            message={"role": "assistant", "content": text},
            metadata=AssistantMessageMetadata(finish_reason=finish),
            turn=1,
        )
    )
runs.update_status_if_in(
    run.id, "completed", ("running",), end_reason=reason, final_output="不同的结构化结果"
)
