"""courses and leads: course catalog, leads from clients, users.source (deep-link tag)

Revision ID: 5a10c002b4b3
Revises: 3e018e1fdf62
Create Date: 2026-10-16 18:35:15.063955
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '5a10c002b4b3'
down_revision: str | None = '3e018e1fdf62'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('courses',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('schedule', sa.String(length=255), nullable=True),
    sa.Column('starts_on', sa.Date(), nullable=True),
    sa.Column('price_tiyin', sa.BigInteger(), nullable=True),
    sa.Column('poster_file_id', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='draft', nullable=False),
    sa.Column('created_by_id', sa.BigInteger(), nullable=True),
    sa.Column('i18n', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('price_tiyin IS NULL OR price_tiyin >= 0', name=op.f('ck_courses_price_non_negative')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_courses_created_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_courses'))
    )
    op.create_table('leads',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('course_id', sa.BigInteger(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('lang', sa.String(length=2), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='new', nullable=False),
    sa.Column('manager_tg_id', sa.BigInteger(), nullable=True),
    sa.Column('manager_name', sa.String(length=128), nullable=True),
    sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('card_chat_id', sa.BigInteger(), nullable=True),
    sa.Column('card_message_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['courses.id'], name=op.f('fk_leads_course_id_courses'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_leads_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_leads'))
    )
    op.create_index(op.f('ix_leads_course_id'), 'leads', ['course_id'], unique=False)
    op.create_index('ix_leads_status_created_at', 'leads', ['status', 'created_at'], unique=False)
    op.create_index(op.f('ix_leads_user_id'), 'leads', ['user_id'], unique=False)
    op.create_index('uq_leads_one_open_per_user_course', 'leads', ['user_id', sa.literal_column('coalesce(course_id, 0)')], unique=True, postgresql_where=sa.text("status IN ('new', 'in_work')"))
    op.add_column('users', sa.Column('source', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'source')
    op.drop_index('uq_leads_one_open_per_user_course', table_name='leads', postgresql_where=sa.text("status IN ('new', 'in_work')"))
    op.drop_index(op.f('ix_leads_user_id'), table_name='leads')
    op.drop_index('ix_leads_status_created_at', table_name='leads')
    op.drop_index(op.f('ix_leads_course_id'), table_name='leads')
    op.drop_table('leads')
    op.drop_table('courses')
