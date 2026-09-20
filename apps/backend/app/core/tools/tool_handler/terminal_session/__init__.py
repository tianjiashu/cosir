"""Interactive terminal Agent tool handlers and definition builders.

The handlers reuse the existing in-process terminal session service. The browser
only attaches to the read-only preview endpoint; it never owns the PTY or sends
input to it.
"""

from app.core.tools.schemas import ToolDefinition
from app.core.tools.tool_handler.terminal_session.close import TerminalCloseTool
from app.core.tools.tool_handler.terminal_session.read import TerminalReadTool
from app.core.tools.tool_handler.terminal_session.signal import TerminalSignalTool
from app.core.tools.tool_handler.terminal_session.start import TerminalStartTool
from app.core.tools.tool_handler.terminal_session.write import TerminalWriteTool


def build_terminal_start_definition() -> ToolDefinition:
    """Build the registered ``terminal_start`` definition."""

    return TerminalStartTool().to_definition_if_avaliable()


def build_terminal_read_definition() -> ToolDefinition:
    """Build the registered ``terminal_read`` definition."""

    return TerminalReadTool().to_definition_if_avaliable()


def build_terminal_write_definition() -> ToolDefinition:
    """Build the registered ``terminal_write`` definition."""

    return TerminalWriteTool().to_definition_if_avaliable()


def build_terminal_signal_definition() -> ToolDefinition:
    """Build the registered ``terminal_signal`` definition."""

    return TerminalSignalTool().to_definition_if_avaliable()


def build_terminal_close_definition() -> ToolDefinition:
    """Build the registered ``terminal_close`` definition."""

    return TerminalCloseTool().to_definition_if_avaliable()

__all__ = [
    "TerminalCloseTool",
    "TerminalReadTool",
    "TerminalSignalTool",
    "TerminalStartTool",
    "TerminalWriteTool",
    "build_terminal_close_definition",
    "build_terminal_read_definition",
    "build_terminal_signal_definition",
    "build_terminal_start_definition",
    "build_terminal_write_definition",
]
