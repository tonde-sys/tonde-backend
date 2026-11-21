-- Run against a representative COPY only. No data changes. Stop on ANY anomaly.
-- ORM AbuseEvent.details maps to the PHYSICAL PostgreSQL column abuse_events.metadata.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT table_name, column_name, data_type FROM information_schema.columns
WHERE table_schema = current_schema() AND
 ((table_name='abuse_events' AND column_name IN ('metadata','details','reason_codes','request_id','created_at','provenance','policy_version'))
 OR (table_name='qape_create_operations' AND column_name IN ('id','state','decision_code','review_generation')))
ORDER BY table_name,column_name;
SELECT version_num FROM alembic_version;
-- STOP if metadata is missing/not json(b), if details exists unexpectedly, or schema differs from 017.
SELECT state,count(*) AS operations FROM qape_create_operations GROUP BY state ORDER BY state;
SELECT org_id,request_id,count(*) AS duplicates FROM abuse_events
WHERE event_type='QAPE_HUMAN_REVIEW'
GROUP BY org_id,request_id HAVING count(*)>1 OR org_id IS NULL OR request_id IS NULL;
WITH checks AS (
 SELECT e.id,e.org_id,e.request_id,e.metadata->>'operation_id' AS operation_id,
 array_remove(ARRAY[
 CASE WHEN o.id IS NULL THEN 'ORPHAN_OR_SCOPE' END,
 CASE WHEN e.request_id IS NULL THEN 'NULL_REQUEST_ID' END,
 CASE WHEN e.created_at IS NULL THEN 'NULL_CREATED_AT' END,
 CASE WHEN nullif(btrim(e.metadata->>'actor_id'),'') IS NULL THEN 'MISSING_ACTOR_ID' END,
 CASE WHEN nullif(btrim(e.metadata->>'reason'),'') IS NULL THEN 'MISSING_REASON' END,
 CASE WHEN e.provenance IS DISTINCT FROM 'LIVE' THEN 'NON_LIVE' END,
 CASE WHEN e.user_id IS NULL OR e.service_id IS NULL OR o.id IS NULL
   OR e.user_id IS DISTINCT FROM o.user_id
   OR e.service_id IS DISTINCT FROM o.service_id THEN 'USER_OR_SERVICE_MISMATCH' END,
 CASE WHEN e.policy_version IS DISTINCT FROM o.policy_version THEN 'POLICY_VERSION_MISMATCH' END,
 CASE WHEN e.reason_codes IS NULL OR jsonb_typeof(e.reason_codes::jsonb) <> 'array'
   THEN 'INVALID_REASON_CODES' END,
 CASE WHEN e.decision IS DISTINCT FROM 'APPROVE' AND e.decision IS DISTINCT FROM 'REJECT'
   THEN 'UNKNOWN_DECISION'
   WHEN e.decision = 'APPROVE' AND NOT
     (e.reason_codes::jsonb ? 'HUMAN_APPROVED' AND NOT e.reason_codes::jsonb ? 'HUMAN_REJECTED')
   THEN 'APPROVE_CODES_MISMATCH'
   WHEN e.decision = 'REJECT' AND NOT
     (e.reason_codes::jsonb ? 'HUMAN_REJECTED' AND NOT e.reason_codes::jsonb ? 'HUMAN_APPROVED')
   THEN 'REJECT_CODES_MISMATCH' END
 ],NULL) AS anomalies
 FROM abuse_events e LEFT JOIN qape_create_operations o
 ON o.id=e.metadata->>'operation_id' AND o.org_id=e.org_id
 WHERE e.event_type='QAPE_HUMAN_REVIEW'
)
SELECT * FROM checks WHERE cardinality(anomalies)>0 ORDER BY id;
-- Legacy self-decisions are reported separately, even when the operation is terminal.
-- Terminal history is never rewritten; nonterminal self-decisions BLOCK backfill.
SELECT e.id AS audit_id, o.id AS operation_id, o.state, e.decision,
       e.metadata->>'actor_id' AS actor_id, o.user_id,
       CASE WHEN o.state IN ('DENIED','COMMITTED','COMPENSATED')
            THEN 'LEGACY_SELF_DECISION_TERMINAL_REPORT_ONLY'
            ELSE 'LEGACY_SELF_DECISION_BLOCK_BACKFILL' END AS finding
FROM abuse_events e JOIN qape_create_operations o
 ON o.id=e.metadata->>'operation_id' AND o.org_id=e.org_id
WHERE e.event_type='QAPE_HUMAN_REVIEW'
 AND e.metadata->>'actor_id'=o.user_id
ORDER BY o.state,o.id,e.id;
WITH grouped AS (
 SELECT o.id,o.state,o.decision_code,o.ticket_id,
 count(e.id) AS n,
 count(e.id) FILTER (WHERE e.decision='APPROVE') AS approvals,
 count(e.id) FILTER (WHERE e.decision='REJECT') AS rejections,
 count(e.id) FILTER (WHERE e.metadata->>'actor_id'=o.user_id) AS self_decisions
 FROM qape_create_operations o LEFT JOIN abuse_events e ON e.org_id=o.org_id
 AND e.event_type='QAPE_HUMAN_REVIEW' AND e.metadata->>'operation_id'=o.id
 GROUP BY o.id,o.state,o.decision_code,o.ticket_id
), classified AS (
 SELECT id,state, CASE
 WHEN n>1 THEN 'AMBIGUOUS_MULTIPLE_AUDITS'
 WHEN self_decisions>0 THEN 'LEGACY_SELF_DECISION'
 WHEN n=1 AND approvals+rejections<>1 THEN 'AMBIGUOUS_DECISION'
 WHEN n=0 AND decision_code IN ('HUMAN_APPROVED','HUMAN_REJECTED') THEN 'AMBIGUOUS_MISSING_AUDIT'
 WHEN rejections=1 AND (state<>'DENIED' OR decision_code IS DISTINCT FROM 'HUMAN_REJECTED' OR ticket_id IS NOT NULL)
 THEN 'AMBIGUOUS_REJECT_STATE'
 WHEN approvals=1 AND decision_code='HUMAN_REJECTED' THEN 'AMBIGUOUS_APPROVE_STATE'
 WHEN approvals=1 AND state='REVIEW_REQUIRED' THEN 'NEW_REVIEW_AFTER_LEGACY_APPROVE'
 WHEN approvals=1 THEN 'LEGACY_APPROVE'
 WHEN rejections=1 THEN 'LEGACY_REJECT'
 WHEN state='REVIEW_REQUIRED' THEN 'FIRST_REVIEW'
 ELSE 'NO_HUMAN_AUDIT' END AS category FROM grouped
)
SELECT category,state,CASE WHEN category LIKE 'AMBIGUOUS%' OR
   (category='LEGACY_SELF_DECISION' AND state NOT IN ('DENIED','COMMITTED','COMPENSATED'))
   THEN NULL
 WHEN category='LEGACY_SELF_DECISION' THEN 1
 WHEN category='NEW_REVIEW_AFTER_LEGACY_APPROVE' THEN 2
 WHEN category IN ('LEGACY_APPROVE','LEGACY_REJECT','FIRST_REVIEW') THEN 1 ELSE 0 END AS proposed_generation,
 count(*) AS operations FROM classified
GROUP BY category,state,proposed_generation ORDER BY category,state;
ROLLBACK;
