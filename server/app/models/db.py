"""SQLAlchemy ORM models for OpenInterest Lens.

Tables: raw_cot_reports, raw_settlements, contracts, signal_positioning,
signal_term_structure, signal_roll_pressure, contango_alerts,
api_keys, webhook_subscriptions.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# ---------------------------------------------------------------------------
# Contract mapping
# ---------------------------------------------------------------------------


class Contract(Base):
    """Tracked futures contract with metadata."""

    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(30), nullable=False)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False)
    contract_size: Mapped[float] = mapped_column(Float, nullable=False)
    months_traded: Mapped[str] = mapped_column(Text, nullable=False, comment="JSON array of month codes, e.g. '[\"H\",\"M\",\"U\",\"Z\"]'")
    data_available_from: Mapped[str] = mapped_column(String(10), nullable=False, comment="YYYY-MM-DD")
    cftc_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, comment="CFTC report name for mapping")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Raw data tables
# ---------------------------------------------------------------------------


class RawCOTReport(Base):
    """Raw CFTC Commitments of Traders report data."""

    __tablename__ = "raw_cot_reports"
    __table_args__ = (
        UniqueConstraint("contract_id", "as_of_date", name="uq_cot_contract_date"),
        Index("ix_cot_contract_date", "contract_id", "as_of_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="Tuesday reference date")
    published_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="Friday publication date")
    commercial_long: Mapped[int] = mapped_column(Integer, nullable=False)
    commercial_short: Mapped[int] = mapped_column(Integer, nullable=False)
    commercial_net: Mapped[int] = mapped_column(Integer, nullable=False)
    non_commercial_long: Mapped[int] = mapped_column(Integer, nullable=False)
    non_commercial_short: Mapped[int] = mapped_column(Integer, nullable=False)
    non_commercial_net: Mapped[int] = mapped_column(Integer, nullable=False)
    non_reportable_long: Mapped[int] = mapped_column(Integer, nullable=False)
    non_reportable_short: Mapped[int] = mapped_column(Integer, nullable=False)
    non_reportable_net: Mapped[int] = mapped_column(Integer, nullable=False)
    total_open_interest: Mapped[int] = mapped_column(Integer, nullable=False)
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class RawSettlement(Base):
    """CME daily settlement data per contract month."""

    __tablename__ = "raw_settlements"
    __table_args__ = (
        UniqueConstraint("contract_id", "month_code", "settlement_date", name="uq_settle_contract_month_date"),
        Index("ix_settle_contract_date", "contract_id", "settlement_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month_code: Mapped[str] = mapped_column(String(10), nullable=False, comment="e.g. 'Jun 26'")
    settlement_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    settlement_price: Mapped[float] = mapped_column(Float, nullable=False)
    open_interest: Mapped[int] = mapped_column(Integer, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    ingestion_timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Signal tables
# ---------------------------------------------------------------------------


class SignalPositioning(Base):
    """Computed positioning signals from COT data."""

    __tablename__ = "signal_positioning"
    __table_args__ = (
        UniqueConstraint("contract_id", "timestamp", name="uq_positioning_contract_ts"),
        Index("ix_positioning_contract_ts", "contract_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    as_of_friday: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Net positions
    net_commercial: Mapped[int] = mapped_column(Integer, nullable=False)
    net_non_commercial: Mapped[int] = mapped_column(Integer, nullable=False)
    net_non_reportable: Mapped[int] = mapped_column(Integer, nullable=False)
    # Smart money metrics
    sm_z_score: Mapped[float] = mapped_column(Float, nullable=False)
    sm_percentile: Mapped[float] = mapped_column(Float, nullable=False)
    sm_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    sm_conviction: Mapped[str] = mapped_column(String(10), nullable=False)
    # Retail metrics
    retail_z_score: Mapped[float] = mapped_column(Float, nullable=False)
    retail_percentile: Mapped[float] = mapped_column(Float, nullable=False)
    retail_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    retail_contrarian_signal: Mapped[str] = mapped_column(String(15), nullable=False)
    # Signal
    signal_overall: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_strength: Mapped[float] = mapped_column(Float, nullable=False)
    signal_divergence: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Week-over-week change
    wow_commercial: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    wow_non_commercial: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    wow_non_reportable: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SignalTermStructure(Base):
    """Computed term structure curves."""

    __tablename__ = "signal_term_structure"
    __table_args__ = (
        UniqueConstraint("contract_id", "as_of_date", name="uq_term_contract_date"),
        Index("ix_term_contract_date", "contract_id", "as_of_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    as_of_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    structure_type: Mapped[str] = mapped_column(String(20), nullable=False)
    curve_json: Mapped[str] = mapped_column(Text, nullable=False, comment="JSON blob of TermStructureCurve months")
    front_month_oi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_oi: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    oi_concentration_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_daily_volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    steepness: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SignalRollPressure(Base):
    """Computed roll pressure index."""

    __tablename__ = "signal_roll_pressure"
    __table_args__ = (
        UniqueConstraint("contract_id", "timestamp", name="uq_roll_contract_ts"),
        Index("ix_roll_contract_ts", "contract_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Nearby contract
    nearby_month: Mapped[str] = mapped_column(String(10), nullable=False)
    nearby_oi: Mapped[int] = mapped_column(Integer, nullable=False)
    nearby_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    nearby_settlement: Mapped[float] = mapped_column(Float, nullable=False)
    # Deferred contract
    deferred_month: Mapped[str] = mapped_column(String(10), nullable=False)
    deferred_oi: Mapped[int] = mapped_column(Integer, nullable=False)
    deferred_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    deferred_settlement: Mapped[float] = mapped_column(Float, nullable=False)
    # Roll pressure metrics
    rp_index: Mapped[float] = mapped_column(Float, nullable=False)
    oi_decay_pct: Mapped[float] = mapped_column(Float, nullable=False)
    spread_basis: Mapped[float] = mapped_column(Float, nullable=False)
    days_to_expiry: Mapped[int] = mapped_column(Integer, nullable=False)
    roll_window: Mapped[str] = mapped_column(String(15), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ContangoAlertRecord(Base):
    """Contango/backwardation transition alerts."""

    __tablename__ = "contango_alerts"
    __table_args__ = (
        Index("ix_contango_contract_ts", "contract_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    structure: Mapped[str] = mapped_column(String(20), nullable=False)
    alert_type: Mapped[str] = mapped_column(String(30), nullable=False)
    front_month_price: Mapped[float] = mapped_column(Float, nullable=False)
    next_month_price: Mapped[float] = mapped_column(Float, nullable=False)
    m1_m2_spread: Mapped[float] = mapped_column(Float, nullable=False)
    m1_m2_annualized: Mapped[float] = mapped_column(Float, nullable=False)
    z_score: Mapped[float] = mapped_column(Float, nullable=False)
    prior_structure: Mapped[str] = mapped_column(String(20), nullable=False)
    days_in_current_state: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Auth tables
# ---------------------------------------------------------------------------


class APIKey(Base):
    """API keys with tier metadata."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True, comment="SHA-256 hash of the API key")
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False, comment="First 12 chars for identification, e.g. 'oil_sk_live_'")
    tier: Mapped[str] = mapped_column(String(20), nullable=False, comment="free | pro | enterprise")
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    contracts_allowed: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="JSON array of allowed contract symbols, or null for all within tier")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class WebhookSubscription(Base):
    """Webhook subscriptions for signal push notifications."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    contracts: Mapped[str] = mapped_column(Text, nullable=False, comment="JSON array of contract symbols")
    signal_types: Mapped[str] = mapped_column(Text, nullable=False, comment="JSON array: positioning, roll_pressure, contango_alert, term_structure")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_delivery_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="HTTP status code of last delivery")