"""Scope the tool monitoring table when its creation ran after tenant scoping."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0035_monitoring_tool_workspace'
down_revision = '0034_merge_fork_rag_repair'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    table = 'monitoring_tool_calls'
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    if table not in tables or 'workspaces' not in tables:
        return
    if 'workspace_uuid' in {c['name'] for c in inspector.get_columns(table)}:
        return
    op.add_column(table, sa.Column('workspace_uuid', sa.String(36), nullable=True))
    if 'bots' in tables:
        op.execute(
            sa.text(
                'UPDATE monitoring_tool_calls SET workspace_uuid = '
                '(SELECT workspace_uuid FROM bots WHERE bots.uuid = monitoring_tool_calls.bot_id)'
            )
        )
    remaining = conn.execute(
        sa.text('SELECT count(*) FROM monitoring_tool_calls WHERE workspace_uuid IS NULL')
    ).scalar()
    if remaining:
        workspaces = conn.execute(sa.text("SELECT uuid FROM workspaces WHERE source = 'local'")).scalars().all()
        if len(workspaces) != 1:
            raise RuntimeError('Tool monitoring rows require an unambiguous Workspace owner')
        conn.execute(
            sa.text('UPDATE monitoring_tool_calls SET workspace_uuid = :workspace WHERE workspace_uuid IS NULL'),
            {'workspace': workspaces[0]},
        )
    with op.batch_alter_table(table) as batch:
        batch.alter_column('workspace_uuid', existing_type=sa.String(36), nullable=False)
        batch.create_foreign_key(
            'fk_monitoring_tool_calls_workspace', 'workspaces', ['workspace_uuid'], ['uuid'], ondelete='CASCADE'
        )
    for suffix, columns in (
        ('timestamp', ['workspace_uuid', 'timestamp']),
        ('session', ['workspace_uuid', 'session_id']),
        ('message', ['workspace_uuid', 'message_id']),
    ):
        op.create_index(f'ix_monitoring_tool_calls_workspace_{suffix}', table, columns)
    if conn.dialect.name == 'postgresql':
        op.execute(sa.text('ALTER TABLE monitoring_tool_calls ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text('ALTER TABLE monitoring_tool_calls FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text('DROP POLICY IF EXISTS langbot_workspace_isolation ON monitoring_tool_calls'))
        expression = "workspace_uuid::text = NULLIF(current_setting('langbot.workspace_uuid', true), '')"
        op.execute(
            sa.text(
                f'CREATE POLICY langbot_workspace_isolation ON monitoring_tool_calls USING ({expression}) WITH CHECK ({expression})'
            )
        )


def downgrade() -> None:
    # Keep tenant ownership: both parent histories expect scoped monitoring data.
    pass
