"""File snapshot schema migrations preserve recoverable ChangeSet state."""

import json

from sqlalchemy import create_engine, inspect, text

from app.storage.init_schema import _ensure_file_snapshot_schema


def test_file_snapshot_migration_drops_redundant_columns_and_keeps_recovery_data() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE conversation_runs (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            """CREATE TABLE file_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                run_id INTEGER NOT NULL REFERENCES conversation_runs(id),
                tool_call_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                mutation_id TEXT NOT NULL,
                mutation_state TEXT NOT NULL,
                mutation_json TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                path TEXT NOT NULL,
                action TEXT NOT NULL,
                op_json TEXT NOT NULL,
                seq INTEGER NOT NULL,
                additions INTEGER NOT NULL,
                deletions INTEGER NOT NULL,
                stable INTEGER NOT NULL,
                status TEXT NOT NULL,
                reverted_at TEXT NOT NULL
            )"""
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_file_snapshots_task_seq ON file_snapshots(task_id, seq)"
        )
        connection.exec_driver_sql("INSERT INTO tasks (id) VALUES (1)")
        connection.exec_driver_sql("INSERT INTO conversation_runs (id) VALUES (2)")
        canonical_envelope = {
            "operation": {
                "type": "UPDATE",
                "operation_id": "legacy-operation",
                "tool": "write_file",
            },
            "path_states": [
                {
                    "file_identity": "legacy-file",
                    "path": "legacy.txt",
                    "before": {"exists": False},
                    "after": {"exists": True, "entry_type": "file"},
                }
            ],
        }
        legacy_op = json.dumps(canonical_envelope)
        connection.execute(
            text(
                """INSERT INTO file_snapshots VALUES
                (:id, 1, 2, :tool_call_id, :operation_id, :mutation_id, :mutation_state,
                 :mutation_json, :tool_name, :path, :action, :op_json, :seq, 2, 1, 1,
                 :status, :reverted_at)"""
            ),
            {
                "id": 7,
                "tool_call_id": "legacy-call",
                "operation_id": "legacy-operation",
                "mutation_id": "",
                "mutation_state": "applied",
                "mutation_json": "",
                "tool_name": "write_file",
                "path": "legacy.txt",
                "action": "modified",
                "op_json": legacy_op,
                "seq": 0,
                "status": "pending",
                "reverted_at": "",
            },
        )
        prepared_manifest = json.dumps(
            {
                "kind": "agent_mutation",
                "mutation_id": "mut-1",
                "workspace_id": 4,
                "task_id": 1,
                "run_id": 2,
                "tool_name": "delete",
                "tool_call_id": "prepared-call",
                "atomic_group_id": "legacy-recursive-delete-group",
                "paths_before": {"a.txt": {"restore_ref": "blob-a"}},
                "paths_after": {},
                "snapshot_paths": ["a.txt", "b.txt"],
                "implicit_directory_paths": [],
                "move_pairs": [],
                "staging": [{"id": "stage-a", "sha256": "digest-a"}],
            }
        )
        for row_id, seq, path in ((8, 1, "a.txt"), (9, 2, "b.txt")):
            prepared_envelope = json.dumps(
                {
                    "operation": {
                        "type": "PREPARED",
                        "operation_id": "mut-1",
                        "tool": "write_file",
                    },
                    "path_states": [
                        {
                            "file_identity": f"file-{path}",
                            "path": path,
                            "before": {"exists": False},
                            "after": {"exists": False},
                        }
                    ],
                }
            )
            connection.execute(
                text(
                    """INSERT INTO file_snapshots VALUES
                    (:id, 1, 2, 'prepared-call', 'mut-1', 'mut-1', 'prepared',
                     :mutation_json, 'write_file', :path, 'prepared', :op_json, :seq,
                     0, 0, 0, 'pending', '')"""
                ),
                {
                    "id": row_id,
                    "mutation_json": prepared_manifest,
                    "path": path,
                    "op_json": prepared_envelope,
                    "seq": seq,
                },
            )

    _ensure_file_snapshot_schema(engine)
    _ensure_file_snapshot_schema(engine)

    with engine.connect() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("file_snapshots")}
        assert columns == {
            "id",
            "task_id",
            "run_id",
            "tool_call_id",
            "mutation_id",
            "mutation_state",
            "mutation_json",
            "path",
            "op_json",
            "seq",
            "status",
            "created_at",
            "updated_at",
        }
        rows = (
            connection.execute(text("SELECT * FROM file_snapshots ORDER BY seq")).mappings().all()
        )
        migrated_envelope = json.loads(rows[0]["op_json"])
        assert migrated_envelope == canonical_envelope
        assert rows[0]["id"] == 7 and rows[0]["status"] == "pending"

        prepared = rows[1:]
        assert [row["id"] for row in prepared] == [8, 9]
        assert bool(prepared[0]["mutation_json"])
        assert prepared[1]["mutation_json"] == ""
        manifest = json.loads(prepared[0]["mutation_json"])
        assert "paths_before" not in manifest
        assert "paths_after" not in manifest
        assert manifest["staging"] == [{"id": "stage-a", "sha256": "digest-a"}]
        assert "atomic_group_id" not in manifest
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        indexes = {index["name"] for index in inspect(connection).get_indexes("file_snapshots")}
        assert {
            "idx_file_snapshots_turn_seq",
            "idx_file_snapshots_task_path_seq",
            "idx_file_snapshots_mutation",
            "uq_file_snapshots_task_seq",
        } <= indexes
