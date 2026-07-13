"""针对 SQLite 运行时状态持久化的测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.events.types import EventType, RuntimeEvent
from app.storage.sqlite import SQLiteTaskStore


class SQLiteTaskStoreTests(unittest.TestCase):
    """校验 SQLite 存储会跨实例持久化运行时状态。"""

    def test_task_turn_step_and_event_survive_store_reopen(self) -> None:
        """校验核心运行时记录在重新打开 SQLite 存储后仍然存在。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果重新打开后仍无法读取任何持久化记录。

        副作用:
            创建一个临时的 SQLite 数据库文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "app.sqlite3"
            first_store = SQLiteTaskStore(database_path)
            task = first_store.create_task("persist me", agent_id="developer")
            turn = first_store.get_turn_for_task(task.task_id)
            step = first_store.create_step(
                turn_id=turn.turn_id,
                step_type="model_call",
                status="running",
                input_summary="test",
            )
            updated_step = first_store.update_step_status(
                step.step_id,
                "completed",
                "done",
            )
            checkpoint = first_store.create_checkpoint(
                task_id=task.task_id,
                stage="test_stage",
                summary="test checkpoint",
                snapshot={"task_id": task.task_id, "stage": "test_stage"},
            )
            first_store.append_event(
                RuntimeEvent(
                    event_type=EventType.RUN_FINISHED,
                    task_id=task.task_id,
                    payload={"status": "completed"},
                )
            )

            second_store = SQLiteTaskStore(database_path)
            reopened_task = second_store.get_task(task.task_id)
            reopened_turn = second_store.get_turn_for_task(task.task_id)
            reopened_events = second_store.list_events(task.task_id)
            reopened_checkpoints = second_store.list_checkpoints(task.task_id)

            self.assertEqual(reopened_task.input_text, "persist me")
            self.assertEqual(reopened_task.agent_id, "developer")
            self.assertEqual(reopened_turn.turn_id, turn.turn_id)
            self.assertEqual(updated_step.status, "completed")
            self.assertEqual(updated_step.output_summary, "done")
            self.assertEqual(reopened_events[0].event_type, "run_finished")
            self.assertEqual(reopened_checkpoints[0].checkpoint_id, checkpoint.checkpoint_id)
            self.assertEqual(reopened_checkpoints[0].snapshot["stage"], "test_stage")

    def test_update_missing_step_raises_key_error(self) -> None:
        """校验当步骤不存在时步骤更新会清晰地失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果缺失步骤的更新未抛出 KeyError。

        副作用:
            创建一个临时的 SQLite 数据库文件。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteTaskStore(Path(temp_dir) / "app.sqlite3")

            with self.assertRaises(KeyError):
                store.update_step_status("missing-step", "failed")

    def test_initialization_adds_agent_id_to_legacy_tasks_table(self) -> None:
        """校验旧任务表会被迁移，并带有默认的 Agent id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果迁移未添加或回填 agent_id。

        副作用:
            创建一个临时的遗留 SQLite 数据库并初始化存储。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.sqlite3"
            now = "2026-07-13T00:00:00+00:00"
            with sqlite3.connect(database_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        project_path TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE tasks (
                        task_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        input_text TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sessions(session_id, project_path, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("legacy-session", None, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, session_id, input_text, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("legacy-task", "legacy-session", "old task", "pending", now, now),
                )

            store = SQLiteTaskStore(database_path)
            legacy_task = store.get_task("legacy-task")
            new_task = store.create_task("new task", agent_id="developer")

            with sqlite3.connect(database_path) as connection:
                column_rows = connection.execute("PRAGMA table_info(tasks)").fetchall()
            column_names = {row[1] for row in column_rows}

            self.assertIn("agent_id", column_names)
            self.assertEqual(legacy_task.agent_id, "developer")
            self.assertEqual(new_task.agent_id, "developer")


if __name__ == "__main__":
    unittest.main()
