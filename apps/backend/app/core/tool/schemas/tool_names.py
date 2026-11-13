"""Canonical names for every built-in Agent tool.

Tool names are part of the model, runtime, persistence-rebuild, and frontend transport
contracts.  Keep their string values in this module so a rename cannot silently leave one
of those boundaries comparing a stale literal.
"""

from typing import Final, Literal, TypeAlias

TOOL_READ_FILE: Final[str] = "read_file"
TOOL_READ_IMAGE: Final[str] = "read_image"
TOOL_WRITE_FILE: Final[str] = "write_file"
TOOL_REPLACE: Final[str] = "patch_write"
TOOL_APPLY_PATCH: Final[str] = "apply_patch"
TOOL_DELETE_FILE: Final[str] = "delete_file"
TOOL_MOVE_FILE: Final[str] = "move_file"
TOOL_SEARCH_CONTENT: Final[str] = "search_content"
TOOL_FIND_FILES: Final[str] = "find_files"
TOOL_LIST_DIRECTORY: Final[str] = "list_directory"
TOOL_EXECUTE_TERMINAL: Final[str] = "execute_terminal"
TOOL_TERMINAL_LIST: Final[str] = "terminal_list"
TOOL_TERMINAL_READ: Final[str] = "terminal_read"
TOOL_TERMINAL_WRITE: Final[str] = "terminal_write"
TOOL_TERMINAL_STOP: Final[str] = "terminal_stop"
TOOL_WEB_SEARCH: Final[str] = "web_search"
TOOL_WEB_EXTRACT: Final[str] = "web_extract"
TOOL_BROWSER_NAVIGATE: Final[str] = "browser_navigate"
TOOL_BROWSER_SNAPSHOT: Final[str] = "browser_snapshot"
TOOL_BROWSER_ACT: Final[str] = "browser_act"
TOOL_BROWSER_SCREENSHOT: Final[str] = "browser_screenshot"
TOOL_BROWSER_TABS: Final[str] = "browser_tabs"
TOOL_BROWSER_LOGS: Final[str] = "browser_logs"
TOOL_BROWSER_EVALUATE: Final[str] = "browser_evaluate"
TOOL_BROWSER_HANDOFF: Final[str] = "browser_handoff"
TOOL_DELEGATE_TASK: Final[str] = "delegate_task"
TOOL_FORK_TASK: Final[str] = "fork_task"
TOOL_SEND_MESSAGE: Final[str] = "send_message"
TOOL_TASK_STATUS: Final[str] = "task_status"
TOOL_WAIT_MESSAGES: Final[str] = "wait_messages"
TOOL_PROPOSE_AGENT_CONFIGURATION: Final[str] = "propose_agent_configuration"
TOOL_MCP: Final[str] = "mcp"
TOOL_SKILL: Final[str] = "skill"

ToolName: TypeAlias = Literal[
    "read_file",
    "read_image",
    "write_file",
    "patch_write",
    "apply_patch",
    "delete_file",
    "move_file",
    "search_content",
    "find_files",
    "list_directory",
    "execute_terminal",
    "terminal_list",
    "terminal_read",
    "terminal_write",
    "terminal_stop",
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_snapshot",
    "browser_act",
    "browser_screenshot",
    "browser_tabs",
    "browser_logs",
    "browser_evaluate",
    "browser_handoff",
    "delegate_task",
    "fork_task",
    "send_message",
    "task_status",
    "wait_messages",
    "propose_agent_configuration",
    "mcp",
    "skill",
]

ALL_TOOL_NAMES: Final[tuple[str, ...]] = (
    TOOL_READ_FILE,
    TOOL_READ_IMAGE,
    TOOL_WRITE_FILE,
    TOOL_REPLACE,
    TOOL_APPLY_PATCH,
    TOOL_DELETE_FILE,
    TOOL_MOVE_FILE,
    TOOL_SEARCH_CONTENT,
    TOOL_FIND_FILES,
    TOOL_LIST_DIRECTORY,
    TOOL_EXECUTE_TERMINAL,
    TOOL_TERMINAL_LIST,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_WRITE,
    TOOL_TERMINAL_STOP,
    TOOL_WEB_SEARCH,
    TOOL_WEB_EXTRACT,
    TOOL_BROWSER_NAVIGATE,
    TOOL_BROWSER_SNAPSHOT,
    TOOL_BROWSER_ACT,
    TOOL_BROWSER_SCREENSHOT,
    TOOL_BROWSER_TABS,
    TOOL_BROWSER_LOGS,
    TOOL_BROWSER_EVALUATE,
    TOOL_BROWSER_HANDOFF,
    TOOL_DELEGATE_TASK,
    TOOL_FORK_TASK,
    TOOL_SEND_MESSAGE,
    TOOL_TASK_STATUS,
    TOOL_WAIT_MESSAGES,
    TOOL_PROPOSE_AGENT_CONFIGURATION,
    TOOL_MCP,
    TOOL_SKILL,
)
