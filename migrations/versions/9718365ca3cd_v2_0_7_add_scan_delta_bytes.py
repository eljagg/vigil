"""v2.0.7 add scan delta-bytes

Revision ID: 9718365ca3cd
Revises: ee3cb4c08d36
Create Date: 2026-04-29 10:38:36.205782

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9718365ca3cd'
down_revision = 'ee3cb4c08d36'
branch_labels = None
depends_on = None


def upgrade():
    # Add the three nullable byte-magnitude columns
    with op.batch_alter_table('scans', schema=None) as batch_op:
        batch_op.add_column(sa.Column('new_bytes', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('grew_bytes', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('shrunk_bytes', sa.BigInteger(), nullable=True))

    # Backfill from existing FileSnapshots so historical scans show the
    # right magnitudes immediately. COALESCE to 0 — a scan with no prior
    # data will have all-NULL deltas, which sums to NULL; we want 0 there.
    op.execute("""
        UPDATE scans s SET
          new_bytes    = COALESCE((
            SELECT SUM(size_bytes) FROM file_snapshots
             WHERE scan_id = s.id AND is_new = TRUE
          ), 0),
          grew_bytes   = COALESCE((
            SELECT SUM(size_delta_bytes) FROM file_snapshots
             WHERE scan_id = s.id AND size_delta_bytes > 0
          ), 0),
          shrunk_bytes = COALESCE((
            SELECT -SUM(size_delta_bytes) FROM file_snapshots
             WHERE scan_id = s.id AND size_delta_bytes < 0
          ), 0)
    """)


def downgrade():
    with op.batch_alter_table('scans', schema=None) as batch_op:
        batch_op.drop_column('shrunk_bytes')
        batch_op.drop_column('grew_bytes')
        batch_op.drop_column('new_bytes')
