"""add aqi timestamps and provenance

Revision ID: c4b1a8d9e2f0
Revises: 8f726b74f22d
Create Date: 2026-11-03 10:00:00.000000

Milestone 1.6E-B: Additive database migration for AQI timestamps, calculation
classification, and data provenance.
Adds 10 nullable fields and 1 composite index each to 'air_quality_data' and 'aqi_history'.
All new columns are strictly NULL for existing records; no server defaults are applied.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4b1a8d9e2f0'
down_revision: Union[str, None] = '8f726b74f22d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Additive columns to air_quality_data
    # -------------------------------------------------------------------------
    op.add_column('air_quality_data', sa.Column('observation_time', sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column('air_quality_data', sa.Column('ingested_at', sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column('air_quality_data', sa.Column('data_source', sa.String(length=100), nullable=True))
    op.add_column('air_quality_data', sa.Column('calculation_type', sa.String(length=50), nullable=True))
    op.add_column('air_quality_data', sa.Column('calculation_formula', sa.String(length=50), nullable=True))
    op.add_column('air_quality_data', sa.Column('is_cpcb_compliant', sa.Boolean(), nullable=True))
    op.add_column('air_quality_data', sa.Column('verification_status', sa.String(length=50), nullable=True))
    op.add_column('air_quality_data', sa.Column('averaging_period_status', sa.String(length=50), nullable=True))
    op.add_column('air_quality_data', sa.Column('averaging_periods', sa.JSON(), nullable=True))
    op.add_column('air_quality_data', sa.Column('provenance', sa.JSON(), nullable=True))

    # Composite index for filtering/sorting by city and source observation time
    op.create_index(
        'ix_air_quality_data_city_obs_time',
        'air_quality_data',
        ['city_id', 'observation_time'],
        unique=False
    )

    # -------------------------------------------------------------------------
    # 2. Additive columns to aqi_history
    # -------------------------------------------------------------------------
    op.add_column('aqi_history', sa.Column('observation_time', sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column('aqi_history', sa.Column('ingested_at', sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column('aqi_history', sa.Column('data_source', sa.String(length=100), nullable=True))
    op.add_column('aqi_history', sa.Column('calculation_type', sa.String(length=50), nullable=True))
    op.add_column('aqi_history', sa.Column('calculation_formula', sa.String(length=50), nullable=True))
    op.add_column('aqi_history', sa.Column('is_cpcb_compliant', sa.Boolean(), nullable=True))
    op.add_column('aqi_history', sa.Column('verification_status', sa.String(length=50), nullable=True))
    op.add_column('aqi_history', sa.Column('averaging_period_status', sa.String(length=50), nullable=True))
    op.add_column('aqi_history', sa.Column('averaging_periods', sa.JSON(), nullable=True))
    op.add_column('aqi_history', sa.Column('provenance', sa.JSON(), nullable=True))

    # Composite index for time-series history retrieval by city and observation time
    op.create_index(
        'ix_aqi_history_city_obs_time',
        'aqi_history',
        ['city_id', 'observation_time'],
        unique=False
    )


def downgrade() -> None:
    """
    WARNING: Downgrading this migration drops the newly added columns and indexes.
    Any observation timestamps, ingestion records, or provenance metadata persisted
    since this migration was applied will be permanently deleted.
    """
    # 1. Downgrade aqi_history
    op.drop_index('ix_aqi_history_city_obs_time', table_name='aqi_history')
    op.drop_column('aqi_history', 'provenance')
    op.drop_column('aqi_history', 'averaging_periods')
    op.drop_column('aqi_history', 'averaging_period_status')
    op.drop_column('aqi_history', 'verification_status')
    op.drop_column('aqi_history', 'is_cpcb_compliant')
    op.drop_column('aqi_history', 'calculation_formula')
    op.drop_column('aqi_history', 'calculation_type')
    op.drop_column('aqi_history', 'data_source')
    op.drop_column('aqi_history', 'ingested_at')
    op.drop_column('aqi_history', 'observation_time')

    # 2. Downgrade air_quality_data
    op.drop_index('ix_air_quality_data_city_obs_time', table_name='air_quality_data')
    op.drop_column('air_quality_data', 'provenance')
    op.drop_column('air_quality_data', 'averaging_periods')
    op.drop_column('air_quality_data', 'averaging_period_status')
    op.drop_column('air_quality_data', 'verification_status')
    op.drop_column('air_quality_data', 'is_cpcb_compliant')
    op.drop_column('air_quality_data', 'calculation_formula')
    op.drop_column('air_quality_data', 'calculation_type')
    op.drop_column('air_quality_data', 'data_source')
    op.drop_column('air_quality_data', 'ingested_at')
    op.drop_column('air_quality_data', 'observation_time')

