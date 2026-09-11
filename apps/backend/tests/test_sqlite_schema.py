from sqlalchemy import create_engine

from app.storage.init_schema import initialize_app_schema


def test_tasks_table_uses_sqlite_autoincrement() -> None:
    engine = create_engine("sqlite://")
    try:
        initialize_app_schema(engine)
        with engine.connect() as connection:
            sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            ).scalar_one()
        assert "AUTOINCREMENT" in sql.upper()
    finally:
        engine.dispose()
