"""Join the fork's historical monitoring repair branch to the upstream head.

Revision ID: 0057_merge_fork_upstream_heads
Revises: 0056_daily_usage_quota_warning, 0035_monitoring_tool_workspace
"""

revision = '0057_merge_fork_upstream_heads'
down_revision = ('0056_daily_usage_quota_warning', '0035_monitoring_tool_workspace')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
