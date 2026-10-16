"""staff tasks: task_states (last seen status, acceptance), users.is_team

Revision ID: 7a1b953bc8d1
Revises: 5a10c002b4b3
Create Date: 2026-10-16 20:07:50.284628
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '7a1b953bc8d1'
down_revision: str | None = '5a10c002b4b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('task_states',
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accepted_by', sa.String(length=128), nullable=True),
    sa.Column('accepted_by_tg_id', sa.BigInteger(), nullable=True),
    sa.Column('c_notified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.PrimaryKeyConstraint('task_id', name=op.f('pk_task_states'))
    )
    op.add_column('users', sa.Column('is_team', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'is_team')
    op.drop_table('task_states')
