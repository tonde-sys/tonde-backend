"""Queue Engine completion — enums alignés, champs ticket, audit_logs

Revision ID: 007_queue_engine_completion
Revises: 006_add_notifications_and_queue_logs
Create Date: 2026-11-10 00:00:00.000000

Cette migration fait trois choses :

1. ALIGNE LES ENUMS POSTGRESQL SUR LE MODÈLE ORM.
   SQLAlchemy stocke les NOMS des membres d'un Enum Python ('STANDARD'), alors que
   les migrations 001 et 006 avaient créé les types avec les valeurs en minuscules
   ('standard'). Une base construite par Alembic refusait donc toute insertion
   (« invalid input value for enum ticketpriority: "STANDARD" »). Seul le mode
   développement (create_all) fonctionnait. Les libellés sont renommés en majuscules
   (ALTER TYPE … RENAME VALUE : aucune perte de données, aucune réécriture de table).
   Idempotent : sans effet sur une base déjà alignée (créée par create_all).

2. Ajoute au ticket : requested_priority (priorité demandée en attente de
   confirmation d'un agent), service_seconds (durée réelle du service, pour l'ETA),
   client_action_id (idempotence offline) + contrainte unique et index de requêtes.

3. Crée audit_logs (exports de données, décisions de priorité, transferts…).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '007_queue_engine_completion'
down_revision: Union[str, None] = '006_add_notifications_and_queue_logs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_TYPES = (
    'userrole', 'employeestatus', 'ticketstatus', 'ticketpriority',
    'queuelogaction', 'notificationchannel', 'notificationstatus',
)


def upgrade() -> None:
    # ── 1. Enums : libellés en majuscules, comme le modèle ───────────────────
    op.execute(f"""
        DO $$
        DECLARE r RECORD;
        BEGIN
            FOR r IN
                SELECT t.typname, e.enumlabel
                FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
                WHERE t.typname IN ({", ".join(f"'{t}'" for t in _ENUM_TYPES)})
                  AND e.enumlabel <> upper(e.enumlabel)
            LOOP
                EXECUTE format('ALTER TYPE %I RENAME VALUE %L TO %L',
                               r.typname, r.enumlabel, upper(r.enumlabel));
            END LOOP;
        END $$;
    """)

    # ── 2. Tickets : nouveaux champs ──────────────────────────────────────────
    priority_enum = postgresql.ENUM(
        'STANDARD', 'PRIORITY', 'VIP', 'EMERGENCY', name='ticketpriority', create_type=False
    )
    op.add_column('tickets', sa.Column('requested_priority', priority_enum, nullable=True))
    op.add_column('tickets', sa.Column('service_seconds', sa.Integer(), nullable=True))
    op.add_column('tickets', sa.Column('client_action_id', sa.String(64), nullable=True))

    # Un même (user, client_action_id) ne crée qu'un ticket. NULL est exclu (index unique
    # PostgreSQL : plusieurs NULL autorisés) : les tickets sans client_action_id ne sont pas concernés.
    op.create_unique_constraint(
        'uq_tickets_user_client_action', 'tickets', ['user_id', 'client_action_id']
    )
    op.create_index('ix_tickets_user_status', 'tickets', ['user_id', 'status'])
    op.create_index('ix_tickets_queue_lookup', 'tickets', ['agency_id', 'service_id', 'status'])

    # ── 3. Journal d'audit ────────────────────────────────────────────────────
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('org_id', sa.String(36), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('actor_id', sa.String(36), nullable=True),
        sa.Column('actor_role', sa.String(30), nullable=True),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('target_type', sa.String(30), nullable=True),
        sa.Column('target_id', sa.String(36), nullable=True),
        sa.Column('extra_data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
    )
    op.create_index('ix_audit_logs_org_id', 'audit_logs', ['org_id'])
    op.create_index('ix_audit_logs_actor_id', 'audit_logs', ['actor_id'])
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'])
    op.create_index('ix_audit_logs_created_at', 'audit_logs', ['created_at'])


def downgrade() -> None:
    op.drop_table('audit_logs')
    op.drop_index('ix_tickets_queue_lookup', table_name='tickets')
    op.drop_index('ix_tickets_user_status', table_name='tickets')
    op.drop_constraint('uq_tickets_user_client_action', 'tickets', type_='unique')
    op.drop_column('tickets', 'client_action_id')
    op.drop_column('tickets', 'service_seconds')
    op.drop_column('tickets', 'requested_priority')
    # Les libellés d'enum restent en MAJUSCULES : c'est ce qu'attend le modèle ORM,
    # revenir aux minuscules rendrait de nouveau la base inutilisable par l'application.
