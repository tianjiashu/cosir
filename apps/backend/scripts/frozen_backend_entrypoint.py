"""PyInstaller entry point for the desktop backend child process."""

from multiprocessing import freeze_support


def main() -> None:
    """Start the FastAPI backend after configuring frozen multiprocessing support."""
    freeze_support()

    from app.__main__ import main as run_backend

    run_backend()


if __name__ == "__main__":
    main()
