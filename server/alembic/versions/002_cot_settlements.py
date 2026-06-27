"""Add raw_cot_reports and raw_settlements tables

Revision ID: 002_cot_settlements
Revises: 001_initial
Create Date: 2026-05-14 00:47:00.000000

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = '002_cot_settlements'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # raw_cot_reports table
    op.create_table(
        'raw_cot_reports',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('contract_id', sa.Integer(), nullable=False),
        sa.Column('as_of_date', sa.DateTime(), nullable=False, comment='Tuesday reference date'),
        sa.Column('published_date', sa.DateTime(), nullable=True, comment='Friday publication date'),
        sa.Column('commercial_long', sa.Integer(), nullable=False),
        sa.Column('commercial_short', sa.Integer(), nullable=False),
        sa.Column('commercial_net', sa.Integer(), nullable=False),
        sa.Column('non_commercial_long', sa.Integer(), nullable=False),
        sa.Column('non_commercial_short', sa.Integer(), nullable=False),
        sa.Column('non_commercial_net', sa.Integer(), nullable=False),
        sa.Column('non_reportable_long', sa.Integer(), nullable=False),
        sa.Column('non_reportable_short', sa.Integer(), nullable=False),
        sa.Column('non_reportable_net', sa.Integer(), nullable=False),
        sa.Column('total_open_interest', sa.Integer(), nullable=False),
        sa.Column('ingestion_timestamp', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('contract_id', 'as_of_date', name='uq_cot_contract_date'),
    )
    op.create_index('ix_cot_contract_date', 'raw_cot_reports', ['contract_id', 'as_of_date'])
    op.create_index(op.f('ix_raw_cot_reports_contract_id'), 'raw_cot_reports', ['contract_id'])

    # raw_settlements table
    op.create_table(
        'raw_settlements',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('contract_id', sa.Integer(), nullable=False),
        sa.Column('month_code', sa.String(length=10), nullable=False, comment="e.g. 'Jun 26'"),
        sa.Column('settlement_date', sa.DateTime(), nullable=False),
        sa.Column('settlement_price', sa.Float(), nullable=False),
        sa.Column('open_interest', sa.Integer(), nullable=False),
        sa.Column('volume', sa.Integer(), nullable=False),
        sa.Column('ingestion_timestamp', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('contract_id', 'month_code', 'settlement_date', name='uq_settle_contract_month_date'),
    )
    op.create_index('ix_settle_contract_date', 'raw_settlements', ['contract_id', 'settlement_date'])
    op.create_index(op.f('ix_raw_settlements_contract_id'), 'raw_settlements', ['contract_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_raw_settlements_contract_id'), table_name='raw_settlements')
    op.drop_index('ix_settle_contract_date', table_name='raw_settlements')
    op.drop_table('raw_settlements')

    op.drop_index(op.f('ix_raw_cot_reports_contract_id'), table_name='raw_cot_reports')
    op.drop_index('ix_cot_contract_date', table_name='raw_cot_reports')
    op.drop_table('raw_cot_reports')