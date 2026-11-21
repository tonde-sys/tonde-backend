"""Durable creation intents; no production data backfill.

Revision ID: 017_qape_create_operations
Revises: 016_qape_history_provenance
"""
from alembic import op
from app.models.qape_operation import QapeCreateOperation

revision = "017_qape_create_operations"
down_revision = "016_qape_history_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_branches_id_org_qape", "branches", ["id", "org_id"])
    table = QapeCreateOperation.__table__
    op.create_table(table.name, *[column.copy() for column in table.columns],
                    *[constraint.copy() for constraint in list(table.constraints)
                      if constraint.__class__.__name__ != 'PrimaryKeyConstraint'])
    for index in table.indexes:
        op.create_index(index.name, table.name, [column.name for column in index.columns], unique=index.unique)


def downgrade():
    op.drop_table("qape_create_operations")
    op.drop_constraint("uq_branches_id_org_qape", "branches", type_="unique")
