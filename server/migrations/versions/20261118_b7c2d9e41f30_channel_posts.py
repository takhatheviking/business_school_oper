"""channel posts: files, publication and reminders for the content plan (docs/CHANNEL.md)

Revision ID: b7c2d9e41f30
Revises: 7a1b953bc8d1
Create Date: 2026-11-18 15:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'b7c2d9e41f30'
down_revision: str | None = '7a1b953bc8d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('channel_posts',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('sheet_id', sa.String(length=64), nullable=False),
    sa.Column('post_no', sa.String(length=16), nullable=False),
    sa.Column('file_id', sa.String(length=255), nullable=True),
    sa.Column('file_kind', sa.String(length=16), nullable=True),
    sa.Column('file_name', sa.String(length=255), nullable=True),
    sa.Column('file_by', sa.String(length=128), nullable=True),
    sa.Column('file_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('skipped', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('posted_by', sa.String(length=128), nullable=True),
    sa.Column('channel_id', sa.BigInteger(), nullable=True),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('sheet_marked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('notified', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_channel_posts')),
    sa.UniqueConstraint('sheet_id', 'post_no', name='uq_channel_posts_sheet_id_post_no')
    )


def downgrade() -> None:
    op.drop_table('channel_posts')
