"""Canonical names for every built-in Agent tool.

Tool names are part of the model, runtime, persistence-rebuild, and frontend transport
contracts.  Keep their string values in this module so a rename cannot silently leave one
of those boundaries comparing a stale literal.
"""

from typing import Final, Literal, TypeAlias

TOOL_READ_FILE: Final[str] = "read_file"
TOOL_WRITE_FILE: Final[str] = "write_file"
TOOL_REPLACE: Final[str] = "patch_write"
TOOL_APPLY_PATCH: Final[str] = "apply_patch"
TOOL_DELETE_FILE: Final[str] = "delete_file"
TOOL_MOVE_FILE: Final[str] = "move_file"
TOOL_SEARCH_CONTENT: Final[str] = "search_content"
TOOL_FIND_FILES: Final[str] = "find_files"
TOOL_LIST_DIRECTORY: Final[str] = "list_directory"
TOOL_EXECUTE_TERMINAL: Final[str] = "execute_terminal"
TOOL_TERMINAL_START: Final[str] = "terminal_start"
TOOL_TERMINAL_READ: Final[str] = "terminal_read"
TOOL_TERMINAL_WRITE: Final[str] = "terminal_write"
TOOL_TERMINAL_SIGNAL: Final[str] = "terminal_signal"
TOOL_TERMINAL_CLOSE: Final[str] = "terminal_close"
TOOL_WEB_SEARCH: Final[str] = "web_search"
TOOL_WEB_EXTRACT: Final[str] = "web_extract"
TOOL_DELEGATE_TASK: Final[str] = "delegate_task"
TOOL_CHILD_AGENT_SEND: Final[str] = "child_agent_send"
TOOL_CHILD_AGENT_STATUS: Final[str] = "child_agent_status"
TOOL_CHILD_AGENT_WAIT: Final[str] = "child_agent_wait"

ToolName: TypeAlias = Literal[
    "read_file",
    "write_file",
    "patch_write",
    "apply_patch",
    "delete_file",
    "move_file",
    "search_content",
    "find_files",
    "list_directory",
    "execute_terminal",
    "terminal_start",
    "terminal_read",
    "terminal_write",
    "terminal_signal",
    "terminal_close",
    "web_search",
    "web_extract",
    "delegate_task",
    "child_agent_send",
    "child_agent_status",
    "child_agent_wait",
]

ALL_TOOL_NAMES: Final[tuple[str, ...]] = (
    TOOL_READ_FILE,
    TOOL_WRITE_FILE,
    TOOL_REPLACE,
    TOOL_APPLY_PATCH,
    TOOL_DELETE_FILE,
    TOOL_MOVE_FILE,
    TOOL_SEARCH_CONTENT,
    TOOL_FIND_FILES,
    TOOL_LIST_DIRECTORY,
    TOOL_EXECUTE_TERMINAL,
    TOOL_TERMINAL_START,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_WRITE,
    TOOL_TERMINAL_SIGNAL,
    TOOL_TERMINAL_CLOSE,
    TOOL_WEB_SEARCH,
    TOOL_WEB_EXTRACT,
    TOOL_DELEGATE_TASK,
    TOOL_CHILD_AGENT_SEND,
    TOOL_CHILD_AGENT_STATUS,
    TOOL_CHILD_AGENT_WAIT,
)
