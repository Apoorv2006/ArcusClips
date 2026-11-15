"""add drive file target id

Revision ID: 4c2f9a1b7e0d
Revises: 031e8657161e
Create Date: 2026-11-15 14:00:00.000000

Adds ``drive_files.target_id`` so shortcut-discovered videos store the actual
target file ID used for media downloads (the shortcut ID is kept in
``drive_file_id`` for dedup/display), and ``drive_files.drive_id`` to scope
media downloads to the correct Shared Drive.
"""
from alembic import op
import sqlalchemy as sa


revision = '4c2f9a1b7e0d'
down_revision = '031e8657161e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'drive_files',
        sa.Column('target_id', sa.String(length=255), nullable=False, server_default=''),
    )
    op.add_column(
        'drive_files',
        sa.Column('drive_id', sa.String(length=255), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_column('drive_files', 'drive_id')
    op.drop_column('drive_files', 'target_id')
