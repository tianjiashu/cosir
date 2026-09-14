import logging
from datetime import date
from pathlib import Path

import app.config.logging.handler.date_size_rotating as rotating_module
from app.config.logging.handler.date_size_rotating import (
    DateSizeRotatingFileHandler,
    dated_log_path,
)


def test_dated_log_path_keeps_directory_and_extension(tmp_path: Path) -> None:
    assert dated_log_path(tmp_path / "backend.log", date(2026, 9, 13)) == (
        tmp_path / "backend-2026-09-13.log"
    )


def test_handler_rotates_before_same_day_file_exceeds_limit(tmp_path: Path) -> None:
    logger = logging.getLogger("test.date_size_rotating")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = DateSizeRotatingFileHandler(
        tmp_path / "backend.log",
        maxBytes=100,
        backupCount=2,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("a" * 60)
        logger.info("b" * 60)
    finally:
        logger.removeHandler(handler)
        handler.close()

    active = tmp_path / f"backend-{date.today().isoformat()}.log"
    first_shard = tmp_path / f"backend-{date.today().isoformat()}.1.log"
    assert active.exists()
    assert first_shard.exists()
    assert active.stat().st_size <= 100
    assert first_shard.read_text(encoding="utf-8").startswith("a")


def test_handler_switches_to_a_new_date_file(monkeypatch, tmp_path: Path) -> None:
    current_date = date(2026, 9, 13)

    class ControlledDate(date):
        @classmethod
        def today(cls) -> "ControlledDate":
            return cls(current_date.year, current_date.month, current_date.day)

    monkeypatch.setattr(rotating_module, "date", ControlledDate)
    logger = logging.getLogger("test.date_size_rotating.date_switch")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = DateSizeRotatingFileHandler(tmp_path / "backend.log", maxBytes=100)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("before date switch")
        current_date = date(2026, 9, 14)
        logger.info("after date switch")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert (tmp_path / "backend-2026-09-13.log").exists()
    assert (tmp_path / "backend-2026-09-14.log").exists()
