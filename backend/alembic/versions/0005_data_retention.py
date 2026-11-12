"""data retention

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workflow_executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('details_purged_at', app.db.UTCDateTime(), nullable=True))
    op.create_index('ix_workflow_executions_status_finished', 'workflow_executions', ['status', 'finished_at'])
    op.create_index('ix_webhook_events_received', 'webhook_events', ['received_at'])
    op.create_index('ix_usage_records_occurred', 'usage_records', ['occurred_at'])
    op.create_index('ix_notifications_created', 'notifications', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_notifications_created', table_name='notifications')
    op.drop_index('ix_usage_records_occurred', table_name='usage_records')
    op.drop_index('ix_webhook_events_received', table_name='webhook_events')
    op.drop_index('ix_workflow_executions_status_finished', table_name='workflow_executions')
    with op.batch_alter_table('workflow_executions', schema=None) as batch_op:
        batch_op.drop_column('details_purged_at')
