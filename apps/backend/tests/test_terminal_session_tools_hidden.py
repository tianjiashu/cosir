from app.core.tools.tool_handler.terminal_session import (
    TerminalCloseTool,
    TerminalReadTool,
    TerminalSignalTool,
    TerminalStartTool,
    TerminalWriteTool,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalStartArgs
from app.core.tools.tool_system import ToolSystem
from app.service.terminal.terminal_session_service import TerminalSessionService
from app.service.terminal.worker import ProcessTerminalWorker


def test_terminal_handlers_implement_handler_base_but_are_not_registered() -> None:
    handlers = (
        TerminalStartTool,
        TerminalWriteTool,
        TerminalReadTool,
        TerminalSignalTool,
        TerminalCloseTool,
    )

    assert all(issubclass(handler, HandlerBase) for handler in handlers)
    registered_names = ToolSystem.build_tool_system().registry.get_all_tool_names()
    assert not any(name.startswith("terminal_") for name in registered_names)
    assert not hasattr(ProcessTerminalWorker, "resize")
    assert not hasattr(TerminalSessionService, "resize")
    assert "terminal_resize" not in {handler.name for handler in handlers}


def test_terminal_start_schema_matches_fixed_initial_dimensions() -> None:
    assert TerminalStartArgs.model_validate({"cols": 20, "rows": 5}).cols == 20
    for payload in ({"cols": 19}, {"rows": 4}, {"rows": 201}):
        try:
            TerminalStartArgs.model_validate(payload)
        except ValueError:
            continue
        raise AssertionError(f"invalid initial dimensions accepted: {payload}")
