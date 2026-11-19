"""baseline_schema

Revision ID: 8f726b74f22d
Revises: 
Create Date: 2026-11-03 02:37:52.913805

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f726b74f22d'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users table
    op.create_table(
        'users',
        sa.Column('user_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('firebase_uid', sa.String(length=128), nullable=True),
        sa.Column('preferred_language', sa.String(length=10), nullable=True),
        sa.Column('role', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.PrimaryKeyConstraint('user_id')
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    op.create_index('ix_users_firebase_uid', 'users', ['firebase_uid'], unique=True)

    # 2. cities table
    op.create_table(
        'cities',
        sa.Column('city_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('city_name', sa.String(length=100), nullable=False),
        sa.Column('country', sa.String(length=100), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=False),
        sa.Column('longitude', sa.Float(), nullable=False),
        sa.Column('population', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('city_id')
    )

    # 3. translations table
    op.create_table(
        'translations',
        sa.Column('translation_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('key_name', sa.String(length=100), nullable=False),
        sa.Column('language_code', sa.String(length=10), nullable=False),
        sa.Column('translated_text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.PrimaryKeyConstraint('translation_id')
    )
    op.create_index('ix_translations_key_name', 'translations', ['key_name'], unique=False)
    op.create_index('ix_translations_language_code', 'translations', ['language_code'], unique=False)

    # 4. air_quality_data table
    op.create_table(
        'air_quality_data',
        sa.Column('record_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('city_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=True),
        sa.Column('pm25', sa.Float(), nullable=True),
        sa.Column('pm10', sa.Float(), nullable=True),
        sa.Column('co2_ppm', sa.Float(), nullable=True),
        sa.Column('co', sa.Float(), nullable=True),
        sa.Column('no2', sa.Float(), nullable=True),
        sa.Column('so2', sa.Float(), nullable=True),
        sa.Column('o3', sa.Float(), nullable=True),
        sa.Column('aqi_score', sa.Float(), nullable=True),
        sa.Column('recorded_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['city_id'], ['cities.city_id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
        sa.PrimaryKeyConstraint('record_id')
    )

    # 5. carbon_footprint table
    op.create_table(
        'carbon_footprint',
        sa.Column('footprint_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('transport_emission', sa.Float(), nullable=False),
        sa.Column('energy_emission', sa.Float(), nullable=False),
        sa.Column('diet_emission', sa.Float(), nullable=False),
        sa.Column('total_emission', sa.Float(), nullable=False),
        sa.Column('calculated_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
        sa.PrimaryKeyConstraint('footprint_id')
    )

    # 6. chatbot_logs table
    op.create_table(
        'chatbot_logs',
        sa.Column('log_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('user_message', sa.Text(), nullable=False),
        sa.Column('bot_response', sa.Text(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
        sa.PrimaryKeyConstraint('log_id')
    )

    # 7. feedback table
    op.create_table(
        'feedback',
        sa.Column('feedback_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
        sa.PrimaryKeyConstraint('feedback_id')
    )

    # 8. city_comparison table
    op.create_table(
        'city_comparison',
        sa.Column('comparison_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('city1_id', sa.UUID(), nullable=False),
        sa.Column('city2_id', sa.UUID(), nullable=False),
        sa.Column('compared_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['city1_id'], ['cities.city_id'], ),
        sa.ForeignKeyConstraint(['city2_id'], ['cities.city_id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
        sa.PrimaryKeyConstraint('comparison_id')
    )

    # 9. aqi_history table
    op.create_table(
        'aqi_history',
        sa.Column('history_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('city_id', sa.UUID(), nullable=False),
        sa.Column('city_name', sa.String(length=100), nullable=False),
        sa.Column('pm25', sa.Float(), nullable=True),
        sa.Column('pm10', sa.Float(), nullable=True),
        sa.Column('aqi_score', sa.Float(), nullable=True),
        sa.Column('risk_level', sa.String(length=20), nullable=True),
        sa.Column('recorded_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['city_id'], ['cities.city_id'], ),
        sa.PrimaryKeyConstraint('history_id')
    )

    # 10. city_alerts table
    op.create_table(
        'city_alerts',
        sa.Column('alert_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('city_id', sa.UUID(), nullable=False),
        sa.Column('city_name', sa.String(length=100), nullable=False),
        sa.Column('alert_type', sa.String(length=20), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('aqi_value', sa.Float(), nullable=True),
        sa.Column('strategy_name', sa.String(length=100), nullable=True),
        sa.Column('forecast_peak', sa.Float(), nullable=True),
        sa.Column('triggered_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['city_id'], ['cities.city_id'], ),
        sa.PrimaryKeyConstraint('alert_id')
    )
    op.create_index('ix_city_alerts_city_id', 'city_alerts', ['city_id'], unique=False)

    # 11. ml_predictions table
    op.create_table(
        'ml_predictions',
        sa.Column('prediction_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('record_id', sa.UUID(), nullable=False),
        sa.Column('predicted_aqi', sa.Float(), nullable=False),
        sa.Column('risk_level', sa.String(length=20), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=True),
        sa.Column('predicted_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['record_id'], ['air_quality_data.record_id'], ),
        sa.PrimaryKeyConstraint('prediction_id')
    )

    # 12. mitigation_strategies table
    op.create_table(
        'mitigation_strategies',
        sa.Column('strategy_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('record_id', sa.UUID(), nullable=True),
        sa.Column('strategy_name', sa.String(length=100), nullable=False),
        sa.Column('action_id', sa.Integer(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('rl_reward', sa.Float(), nullable=True),
        sa.Column('selected_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.ForeignKeyConstraint(['record_id'], ['air_quality_data.record_id'], ),
        sa.PrimaryKeyConstraint('strategy_id')
    )

    # 13. alert_notifications table
    op.create_table(
        'alert_notifications',
        sa.Column('notification_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('alert_id', sa.UUID(), nullable=False),
        sa.Column('city_name', sa.String(length=100), nullable=False),
        sa.Column('alert_type', sa.String(length=20), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('NOW()'), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['alert_id'], ['city_alerts.alert_id'], ),
        sa.PrimaryKeyConstraint('notification_id')
    )
    op.create_index('ix_alert_notifications_alert_id', 'alert_notifications', ['alert_id'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse dependency order
    op.drop_index('ix_alert_notifications_alert_id', table_name='alert_notifications')
    op.drop_table('alert_notifications')

    op.drop_table('mitigation_strategies')
    op.drop_table('ml_predictions')

    op.drop_index('ix_city_alerts_city_id', table_name='city_alerts')
    op.drop_table('city_alerts')

    op.drop_table('aqi_history')
    op.drop_table('city_comparison')
    op.drop_table('feedback')
    op.drop_table('chatbot_logs')
    op.drop_table('carbon_footprint')
    op.drop_table('air_quality_data')

    op.drop_index('ix_translations_language_code', table_name='translations')
    op.drop_index('ix_translations_key_name', table_name='translations')
    op.drop_table('translations')

    op.drop_table('cities')

    op.drop_index('ix_users_firebase_uid', table_name='users')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
