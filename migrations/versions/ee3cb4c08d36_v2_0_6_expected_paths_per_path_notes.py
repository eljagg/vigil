"""v2.0.6 expected paths + per-path notes

Revision ID: ee3cb4c08d36
Revises: b80b11b67b16
Create Date: 2026-04-29 10:11:54.938036

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ee3cb4c08d36'
down_revision = 'b80b11b67b16'
branch_labels = None
depends_on = None


def upgrade():
    # is_expected gets server_default=false so existing path_entries (the
    # ones discovered ad-hoc by scans up to this point) backfill cleanly.
    # The other two columns are nullable; no backfill needed.
    with op.batch_alter_table('path_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'is_expected', sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))
        batch_op.add_column(sa.Column('expected_cadence', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('notes', sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            'ck_path_entries_cadence',
            "expected_cadence IS NULL OR expected_cadence IN ('daily','weekly','fortnightly')",
        )


def downgrade():
    with op.batch_alter_table('path_entries', schema=None) as batch_op:
        batch_op.drop_constraint('ck_path_entries_cadence', type_='check')
        batch_op.drop_column('notes')
        batch_op.drop_column('expected_cadence')
        batch_op.drop_column('is_expected')
