"""Serialize task-scoped file snapshot sequence allocation."""

from __future__ import annotations

import threading

# Snapshot sequence slots are reserved before file effects begin. This lock prevents
# concurrent operations in one backend process from reserving overlapping task ranges.
task_file_sequence_lock = threading.Lock()
