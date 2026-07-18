"""Tests for durable run storage."""

from pathlib import Path
import tempfile
import unittest

from app.storage.crud.durable import DurableRunStore


class DurableRunStateTests(unittest.TestCase):
    """Validate durable run persistence that remains after cleanup."""

    def test_run_create_update_list_and_delete(self) -> None:
        """Verify durable run records can be created, updated, listed, and deleted.

        Parameters:
            None.

        Returns:
            None.

        Raises:
            AssertionError: If durable run persistence does not match expectations.

        Side effects:
            Creates a temporary SQLite database.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = DurableRunStore(Path(temp_dir) / "app.sqlite3")
            try:
                run = store.create_for_turn("task-1", "turn-1", "created", thread_id="thread-1")

                self.assertEqual(store.get(run.run_id).status, "created")
                self.assertEqual(store.get_by_turn("turn-1").run_id, run.run_id)

                updated = store.mark_status(run.run_id, "running", active_step_id="step-1")

                self.assertEqual(updated.status, "running")
                self.assertEqual(updated.active_step_id, "step-1")
                self.assertEqual([item.run_id for item in store.list_by_task("task-1")], [run.run_id])
                self.assertEqual(store.delete_by_task_ids(["task-1"]), [run.run_id])
                self.assertIsNone(store.get_by_turn("turn-1"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
