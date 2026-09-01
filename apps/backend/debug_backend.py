"""Development and IDE debug entrypoint for the local backend server.

By default this entrypoint enables uvicorn hot reload and debug-level logging.
For reliable same-process IDE breakpoints, set ``CODING_AGENT_RELOAD=false``;
uvicorn reload runs the application in a child process, which most IDEs do not
debug automatically.
"""

import os

from app.__main__ import main


def run_debug_server() -> None:
    """Start the backend server with IDE-friendly defaults.

    Parameters:
        None.

    Returns:
        None.

    Raises:
        Exception: Propagates startup/runtime errors from the normal backend
            entrypoint so the IDE debugger can stop on them.

    Side effects:
        Sets default development environment variables for host, port, reload
        mode, and log level, then starts uvicorn through the standard backend
        main function.
    """

    os.environ.setdefault("CODING_AGENT_HOST", "127.0.0.1")
    os.environ.setdefault("CODING_AGENT_PORT", "8000")
    os.environ.setdefault("CODING_AGENT_RELOAD", "true")
    os.environ.setdefault("CODING_AGENT_LOG_LEVEL", "debug")
    main()


if __name__ == "__main__":
    run_debug_server()
