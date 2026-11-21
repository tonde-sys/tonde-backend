"""Review cycles; fail closed on inconsistent legacy human audits.

Deploy only with legacy review writers stopped. No historical audit is rewritten.
Revision ID: 018_qape_review_generation
Revises: 017_qape_create_operations
"""
from alembic import op
import os
import sqlalchemy as sa

revision = '018_qape_review_generation'
down_revision = '017_qape_create_operations'
branch_labels = None
depends_on = None


_VALID = """
SELECT EXISTS (
 SELECT 1 FROM abuse_events e
 LEFT JOIN qape_create_operations o
   ON o.id = e.metadata->>'operation_id' AND o.org_id = e.org_id
 WHERE e.event_type = 'QAPE_HUMAN_REVIEW' AND (
    o.id IS NULL OR e.provenance IS DISTINCT FROM 'LIVE'
    OR e.user_id IS DISTINCT FROM o.user_id
    OR e.service_id IS DISTINCT FROM o.service_id
    OR e.policy_version IS DISTINCT FROM o.policy_version
    OR e.request_id IS NULL OR e.created_at IS NULL
    OR nullif(btrim(e.metadata->>'actor_id'), '') IS NULL
    OR nullif(btrim(e.metadata->>'reason'), '') IS NULL
    OR e.decision NOT IN ('APPROVE', 'REJECT') OR e.decision IS NULL
    OR e.reason_codes IS NULL
    OR jsonb_typeof(e.reason_codes::jsonb) IS DISTINCT FROM 'array'
    OR (e.decision = 'APPROVE' AND NOT
        (e.reason_codes::jsonb ? 'HUMAN_APPROVED' AND NOT e.reason_codes::jsonb ? 'HUMAN_REJECTED'))
    OR (e.decision = 'REJECT' AND NOT
        (e.reason_codes::jsonb ? 'HUMAN_REJECTED' AND NOT e.reason_codes::jsonb ? 'HUMAN_APPROVED'))
 )
)
OR EXISTS (
 SELECT 1 FROM abuse_events e JOIN qape_create_operations o
 ON o.id = e.metadata->>'operation_id' AND o.org_id = e.org_id
 WHERE e.event_type = 'QAPE_HUMAN_REVIEW'
 AND e.metadata->>'actor_id' = o.user_id
 AND o.state NOT IN ('DENIED', 'COMMITTED', 'COMPENSATED')
 )
 OR EXISTS (
 SELECT 1 FROM abuse_events WHERE event_type = 'QAPE_HUMAN_REVIEW'
 GROUP BY org_id, request_id HAVING count(*) > 1
 )
 OR EXISTS (
 SELECT 1 FROM qape_create_operations o
 LEFT JOIN abuse_events e ON e.org_id = o.org_id
   AND e.event_type = 'QAPE_HUMAN_REVIEW' AND e.metadata->>'operation_id' = o.id
 GROUP BY o.id, o.state, o.decision_code, o.ticket_id
 HAVING count(e.id) > 1
 OR (count(e.id) = 0 AND o.decision_code IN ('HUMAN_APPROVED', 'HUMAN_REJECTED'))
 OR (count(e.id) FILTER (WHERE e.decision = 'REJECT') = 1
     AND (o.state <> 'DENIED' OR o.decision_code IS DISTINCT FROM 'HUMAN_REJECTED'
          OR o.ticket_id IS NOT NULL))
 OR (count(e.id) FILTER (WHERE e.decision = 'APPROVE') = 1
     AND o.decision_code = 'HUMAN_REJECTED')
 )
"""


def upgrade():
    bind = op.get_bind()
    # The safety check runs BEFORE any DDL, in Alembic's migration transaction.
    if bind.execute(sa.text(_VALID)).scalar():
        raise RuntimeError('Ambiguous QAPE human audit: abort migration; run read-only dry-run')
    existing = bind.execute(sa.text('SELECT count(*) FROM qape_create_operations')).scalar()
    if existing and os.getenv('QAPE_REVIEW_BACKFILL_AUTHORIZED') != 'explicitly-approved-after-dry-run':
        raise RuntimeError('Existing QAPE operations: explicit dry-run report and backfill authorization required')
    # 017 imports today's ORM model dynamically: a fresh 017 may already have
    # this column, whereas databases migrated before this release do not.
    cols = {c['name'] for c in sa.inspect(bind).get_columns('qape_create_operations')}
    if 'review_generation' not in cols:
        op.add_column('qape_create_operations', sa.Column('review_generation', sa.Integer(), nullable=True))
    op.execute(sa.text("""
        UPDATE qape_create_operations o SET review_generation = CASE
          WHEN o.state = 'REVIEW_REQUIRED' AND EXISTS (
            SELECT 1 FROM abuse_events e WHERE e.org_id = o.org_id
              AND e.event_type = 'QAPE_HUMAN_REVIEW'
              AND e.metadata->>'operation_id' = o.id AND e.decision = 'APPROVE') THEN 2
          WHEN o.state = 'REVIEW_REQUIRED' OR EXISTS (
            SELECT 1 FROM abuse_events e WHERE e.org_id = o.org_id
              AND e.event_type = 'QAPE_HUMAN_REVIEW'
              AND e.metadata->>'operation_id' = o.id) THEN 1
          ELSE 0 END
    """))
    op.alter_column('qape_create_operations', 'review_generation', nullable=False)
    checks = {c['name'] for c in sa.inspect(bind).get_check_constraints('qape_create_operations')}
    if 'ck_qape_review_generation' not in checks:
        op.create_check_constraint('ck_qape_review_generation', 'qape_create_operations', 'review_generation >= 0')
    op.add_column('abuse_events', sa.Column('review_audit_version', sa.Integer(), nullable=True))
    op.add_column('abuse_events', sa.Column('review_operation_id', sa.String(36), nullable=True))
    op.add_column('abuse_events', sa.Column('review_generation', sa.Integer(), nullable=True))
    op.create_check_constraint('ck_qape_review_v2_complete', 'abuse_events',
        "(review_audit_version IS NULL AND review_operation_id IS NULL AND review_generation IS NULL) OR (event_type = 'QAPE_HUMAN_REVIEW' AND review_audit_version IS NOT NULL AND review_audit_version = 2 AND review_operation_id IS NOT NULL AND review_generation IS NOT NULL AND review_generation > 0 AND request_id IS NOT NULL)")
    op.create_index('uq_qape_human_review_cycle', 'abuse_events',
        ['review_operation_id', 'review_generation'], unique=True,
        postgresql_where=sa.text("event_type = 'QAPE_HUMAN_REVIEW' AND review_audit_version = 2"),
        sqlite_where=sa.text("event_type = 'QAPE_HUMAN_REVIEW' AND review_audit_version = 2"))


def downgrade():
    raise RuntimeError('Unsafe downgrade: review generations and audits must be preserved; roll forward')
