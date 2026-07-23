"""IDE debug entrypoint for the local backend server.

Run this file directly from IDEA / PyCharm when you want breakpoints to stay in
the same Python process. Unlike ``python -m app``, this entrypoint disables
uvicorn reload by default because reload starts a child process and makes
step-by-step debugging harder.
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
        Sets default development environment variables for host, port, and
        reload mode, then starts uvicorn through the standard backend main
        function.
    """

    os.environ.setdefault("CODING_AGENT_HOST", "127.0.0.1")
    os.environ.setdefault("CODING_AGENT_PORT", "8000")
    os.environ.setdefault("CODING_AGENT_RELOAD", "false")
    main()


if __name__ == "__main__":
    run_debug_server()
