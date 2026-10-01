import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.parametrize('existing_scope', [False, True])
def test_tool_monitoring_repair_scopes_late_created_table_and_is_idempotent(existing_scope):
    path = Path('src/langbot/pkg/persistence/alembic/versions/0035_repair_monitoring_tool_workspace.py')
    spec = importlib.util.spec_from_file_location('monitoring_tool_repair', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE workspaces (uuid VARCHAR(36) PRIMARY KEY, source VARCHAR(32))')
        conn.exec_driver_sql("INSERT INTO workspaces VALUES ('workspace-1', 'local')")
        conn.exec_driver_sql('CREATE TABLE bots (uuid VARCHAR(36) PRIMARY KEY, workspace_uuid VARCHAR(36))')
        conn.exec_driver_sql("INSERT INTO bots VALUES ('bot-1', 'workspace-1')")
        scope = ', workspace_uuid VARCHAR(36) NOT NULL' if existing_scope else ''
        conn.exec_driver_sql(
            'CREATE TABLE monitoring_tool_calls (id VARCHAR(255) PRIMARY KEY, bot_id VARCHAR(255), '
            'timestamp DATETIME, session_id VARCHAR(255), message_id VARCHAR(255)' + scope + ')'
        )
        values = ", 'workspace-1'" if existing_scope else ''
        conn.exec_driver_sql(
            "INSERT INTO monitoring_tool_calls VALUES ('call-1', 'bot-1', NULL, 'session-1', NULL" + values + ')'
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        assert conn.exec_driver_sql('SELECT id, workspace_uuid FROM monitoring_tool_calls').all() == [
            ('call-1', 'workspace-1')
        ]
        columns = {c['name']: c for c in sa.inspect(conn).get_columns('monitoring_tool_calls')}
        assert columns['workspace_uuid']['nullable'] is False
    engine.dispose()
