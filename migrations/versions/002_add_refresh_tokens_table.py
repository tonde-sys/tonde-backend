"""Add refresh_tokens table for persistent session management

Revision ID: 002_add_refresh_tokens
Revises: 001_initial
Create Date: 2026-06-22 15:00:00.000000

Ajoute la table refresh_tokens pour permettre :
  - Logout propre et sécurisé
  - Révocation de session à distance
  - Support multi-device (plusieurs sessions par compte)
  - Rotation automatique des tokens

Le token JWT brut n'est jamais stocké — uniquement son SHA-256 (64 hex chars).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = '002_add_refresh_tokens'
down_revision: Union[str, None] = '001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 001_initial crée un ancien brouillon de 'refresh_tokens' (is_revoked,
    # device_info) incompatible avec le modèle actuel (revoked_at, device_id).
    # Sur une base neuve la table est vide : on la remplace. Si elle est déjà au
    # nouveau format, il n'y a rien à faire (migration idempotente).
    inspector = sa.inspect(op.get_bind())
    if 'refresh_tokens' in inspector.get_table_names():
        existing_columns = {c['name'] for c in inspector.get_columns('refresh_tokens')}
        if 'revoked_at' in existing_columns:
            return
        op.drop_table('refresh_tokens')

    op.create_table(
        'refresh_tokens',

        # ── Identifiant ──────────────────────────────────────────
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),

        # ── Propriétaire ─────────────────────────────────────────
        sa.Column(
            'user_id', sa.String(36),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),

        # ── Hash SHA-256 du token JWT (64 hex chars) ─────────────
        # Jamais le token en clair — UNIQUE pour lookup rapide
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),

        # ── Tracking multi-device ─────────────────────────────────
        sa.Column('device_id', sa.String(255), nullable=True),

        # ── IP du client à l'émission (audit) ────────────────────
        sa.Column('ip_address', sa.String(45), nullable=True),

        # ── Expiration (copie du payload JWT exp) ─────────────────
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),

        # ── Révocation : NULL = actif, non-NULL = révoqué ─────────
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),

        # ── Date d'émission ───────────────────────────────────────
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('NOW()'),
            nullable=False,
        ),
    )

    # Index sur user_id pour les requêtes par utilisateur (logout, logout/all)
    op.create_index(
        'ix_refresh_tokens_user_id',
        'refresh_tokens',
        ['user_id'],
    )

    # Index sur device_id pour révoquer les anciennes sessions du même device
    op.create_index(
        'ix_refresh_tokens_device_id',
        'refresh_tokens',
        ['device_id'],
    )

    # Index composite (user_id, revoked_at) pour les sessions actives par user
    op.create_index(
        'ix_refresh_tokens_user_active',
        'refresh_tokens',
        ['user_id', 'revoked_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_refresh_tokens_user_active', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_device_id', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
