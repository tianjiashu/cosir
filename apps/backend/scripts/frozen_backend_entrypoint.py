"""PyInstaller entry point for the desktop backend child process."""

from multiprocessing import freeze_support
import sys


def verify_packaged_provider_imports() -> None:
    """Exercise LiteLLM's dynamically imported provider path without making a request."""
    from litellm import acompletion, get_llm_provider

    _, provider, _, _ = get_llm_provider("deepseek/deepseek-chat")
    if provider != "deepseek" or not callable(acompletion):
        raise RuntimeError("frozen LiteLLM provider imports are incomplete")
    print("frozen provider import check passed")


def main() -> None:
    """Start the FastAPI backend after configuring frozen multiprocessing support."""
    freeze_support()

    if "--packaging-check" in sys.argv:
        verify_packaged_provider_imports()
        return

    from app.__main__ import main as run_backend

    run_backend()


if __name__ == "__main__":
    main()
