"""Canonical identity for a create-ticket intention (never derive user from payload)."""
import hashlib
import json

CANONICALIZATION_VERSION = 1


def payload_hash(agency_id: str, service_id: str, priority: str) -> str:
    if not all(isinstance(x, str) and x for x in (agency_id, service_id, priority)):
        raise ValueError('agency_id, service_id and priority required')
    body = {'v': CANONICALIZATION_VERSION, 'agency_id': agency_id,
            'service_id': service_id, 'priority': priority.strip().lower()}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
