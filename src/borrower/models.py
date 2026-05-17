"""
Bruno — MesiCap loan portfolio data model.

Tables:
- counterparties: lenders and the borrower (MesiCap itself)
- loans: loan agreements (term loans, amortizing loans, revolving facilities)
- loan_movements: principal disbursements and repayments
- loan_amendments: non-principal changes (rate, maturity, etc.)
- interest_accruals: daily/periodic interest accumulation snapshots
- payments: scheduled and actual payment records
- audit_log: every change to any record

The schema is generic enough to handle: shareholder loans, external private loans,
intercompany loans, bank credit facilities, bond instruments, multiple currencies,
back-to-back structures, and capitalizing vs amortizing interest.

Database file: data/bruno.db (separate from Maggy's trading.db).
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    Enum as SqlEnum,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
import enum

Base = declarative_base()


# =============================================================================
# Enums (stored as strings in DB for readability)
# =============================================================================

class CounterpartyType(str, enum.Enum):
    INDIVIDUAL = "individual"
    COMPANY = "company"
    BANK = "bank"
    INTERNAL = "internal"  # MesiCap itself


class CounterpartyTier(str, enum.Enum):
    # For external lenders only; internal/borrower entities use None
    TIER_A = "tier_a"  # closest trust
    TIER_B = "tier_b"  # friends/extended network
    TIER_C = "tier_c"  # business contacts
    TIER_D = "tier_d"  # sophisticated lenders
    SHAREHOLDER = "shareholder"  # MesiCap shareholders / their entities


class LoanType(str, enum.Enum):
    SHAREHOLDER_LOAN = "shareholder_loan"
    BILATERAL_PRIVATE = "bilateral_private"
    REVOLVING_FACILITY = "revolving_facility"
    TERM_LOAN = "term_loan"
    AMORTIZING_LOAN = "amortizing_loan"
    INTERCOMPANY = "intercompany"
    BOND = "bond"


class InterestRateType(str, enum.Enum):
    FIXED = "fixed"
    FLOATING = "floating"
    ZERO = "zero"  # explicit 0% loans


class DayCountConvention(str, enum.Enum):
    ACT_360 = "act/360"
    ACT_365 = "act/365"
    THIRTY_360 = "30/360"


class InterestTreatment(str, enum.Enum):
    CAPITALIZING = "capitalizing"  # accrued, paid at maturity
    AMORTIZING = "amortizing"  # paid each period as part of installment
    PAID_PERIODICALLY = "paid_periodically"  # paid each period, separate from principal


class RepaymentStructure(str, enum.Enum):
    BULLET = "bullet"  # principal repaid in full at maturity
    AMORTIZING = "amortizing"  # principal repaid in installments per schedule
    REVOLVING = "revolving"  # drawn down and repaid flexibly within facility
    INTEREST_ONLY_THEN_BULLET = "interest_only_then_bullet"


class PaymentFrequency(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"
    AT_MATURITY = "at_maturity"


class LoanStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    MATURED = "matured"
    REPAID = "repaid"
    DEFAULTED = "defaulted"
    CANCELLED = "cancelled"


class LoanPurpose(str, enum.Enum):
    OPERATING = "operating"
    TRADING_CAPITAL = "trading_capital"
    INFRASTRUCTURE = "infrastructure"  # e.g. octoserver hardware
    OTHER = "other"


class MovementType(str, enum.Enum):
    DISBURSEMENT = "disbursement"  # money flowing IN to MesiCap
    PRINCIPAL_REPAYMENT = "principal_repayment"  # money flowing OUT to lender
    PRINCIPAL_RESTRUCTURE = "principal_restructure"  # paper adjustment (e.g. premium added)


class PaymentType(str, enum.Enum):
    INTEREST = "interest"
    PRINCIPAL = "principal"
    COMBINED = "combined"  # for amortizing loans, single combined payment


class PaymentStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    PAID = "paid"
    OVERDUE = "overdue"
    WAIVED = "waived"
    CANCELLED = "cancelled"


# =============================================================================
# Tables
# =============================================================================

class Counterparty(Base):
    __tablename__ = "counterparties"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    type = Column(SqlEnum(CounterpartyType), nullable=False)
    tier = Column(SqlEnum(CounterpartyTier), nullable=True)

    # Identity
    legal_form = Column(String(64), nullable=True)  # "OÜ", "AS", etc.
    registration_number = Column(String(64), nullable=True)
    country = Column(String(2), nullable=True, default="EE")  # ISO 2-letter

    # Contact
    address = Column(Text, nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(64), nullable=True)
    iban = Column(String(64), nullable=True)  # primary IBAN; multiple IBANs possible
    secondary_iban = Column(String(64), nullable=True)

    # Relationship management
    notes = Column(Text, nullable=True)
    related_principal = Column(String(255), nullable=True)  # which MesiCap principal this entity belongs to

    # KYC
    kyc_status = Column(String(32), nullable=True, default="not_required")  # for now
    kyc_completed_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    loans_as_lender = relationship("Loan", foreign_keys="Loan.lender_id", back_populates="lender")
    loans_as_borrower = relationship("Loan", foreign_keys="Loan.borrower_id", back_populates="borrower")


class Loan(Base):
    __tablename__ = "loans"

    id = Column(Integer, primary_key=True)

    # Parties
    lender_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
    borrower_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)

    # Identification
    contract_reference = Column(String(128), nullable=True)  # e.g. "Master loan agreement 23.10.2025"
    description = Column(String(512), nullable=True)  # short human-readable description

    # Loan type and structure
    loan_type = Column(SqlEnum(LoanType), nullable=False)
    repayment_structure = Column(SqlEnum(RepaymentStructure), nullable=False)
    purpose = Column(SqlEnum(LoanPurpose), nullable=False, default=LoanPurpose.OTHER)

    # Principal
    principal_max = Column(Float, nullable=False)  # max facility size; for non-revolving = original principal
    currency = Column(String(3), nullable=False, default="EUR")  # ISO 4217

    # Interest
    interest_rate_type = Column(SqlEnum(InterestRateType), nullable=False, default=InterestRateType.FIXED)
    interest_rate_annual = Column(Float, nullable=False)  # decimal: 0.05 = 5%; for FIXED and current effective rate for FLOATING
    floating_benchmark = Column(String(64), nullable=True)  # e.g. "EURIBOR_6M"
    floating_spread = Column(Float, nullable=True)  # decimal: 0.089 = 8.9%
    day_count_convention = Column(SqlEnum(DayCountConvention), nullable=False, default=DayCountConvention.ACT_360)
    interest_treatment = Column(SqlEnum(InterestTreatment), nullable=False)

    # Payment schedule
    payment_frequency = Column(SqlEnum(PaymentFrequency), nullable=False, default=PaymentFrequency.AT_MATURITY)
    payment_day_of_month = Column(Integer, nullable=True)  # e.g. 14 means paid on 14th of each month
    installment_amount = Column(Float, nullable=True)  # for amortizing loans with fixed installment

    # Dates
    contract_date = Column(Date, nullable=False)  # when the contract was signed
    origination_date = Column(Date, nullable=False)  # when the first disbursement happened
    maturity_date = Column(Date, nullable=False)

    # Collateral
    collateral_description = Column(Text, nullable=True)

    # Back-to-back / pass-through structure
    parent_loan_description = Column(Text, nullable=True)  # e.g. "Thirona's bank loan at 11.337%, maturing 15.10.2031"

    # Subordination
    is_subordinated = Column(Boolean, nullable=False, default=False)

    # Early repayment
    early_repayment_allowed = Column(Boolean, nullable=False, default=True)
    early_repayment_notice_days = Column(Integer, nullable=True)

    # Status
    status = Column(SqlEnum(LoanStatus), nullable=False, default=LoanStatus.ACTIVE)

    # Documents
    agreement_document_path = Column(String(512), nullable=True)

    # Notes
    notes = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    lender = relationship("Counterparty", foreign_keys=[lender_id], back_populates="loans_as_lender")
    borrower = relationship("Counterparty", foreign_keys=[borrower_id], back_populates="loans_as_borrower")
    movements = relationship("LoanMovement", back_populates="loan", cascade="all, delete-orphan")
    amendments = relationship("LoanAmendment", back_populates="loan", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="loan", cascade="all, delete-orphan")
    interest_accruals = relationship("InterestAccrual", back_populates="loan", cascade="all, delete-orphan")


class LoanMovement(Base):
    __tablename__ = "loan_movements"

    id = Column(Integer, primary_key=True)
    loan_id = Column(Integer, ForeignKey("loans.id"), nullable=False)

    movement_date = Column(Date, nullable=False)
    movement_type = Column(SqlEnum(MovementType), nullable=False)
    amount = Column(Float, nullable=False)  # always positive; type indicates direction
    currency = Column(String(3), nullable=False)

    # For matching to bank statements
    bank_reference = Column(String(128), nullable=True)
    bank_account_iban = Column(String(64), nullable=True)

    description = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    loan = relationship("Loan", back_populates="movements")


class LoanAmendment(Base):
    __tablename__ = "loan_amendments"

    id = Column(Integer, primary_key=True)
    loan_id = Column(Integer, ForeignKey("loans.id"), nullable=False)

    amendment_date = Column(Date, nullable=False)
    field_changed = Column(String(64), nullable=False)  # e.g. "interest_rate_annual", "maturity_date"
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)

    description = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    loan = relationship("Loan", back_populates="amendments")


class InterestAccrual(Base):
    __tablename__ = "interest_accruals"

    id = Column(Integer, primary_key=True)
    loan_id = Column(Integer, ForeignKey("loans.id"), nullable=False)

    accrual_date = Column(Date, nullable=False)
    principal_balance = Column(Float, nullable=False)  # outstanding principal on this date
    days_in_period = Column(Integer, nullable=False)
    interest_rate = Column(Float, nullable=False)  # rate used for this period
    accrued_amount = Column(Float, nullable=False)  # interest accrued in this period
    cumulative_accrued = Column(Float, nullable=False)  # total accrued since origination

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    loan = relationship("Loan", back_populates="interest_accruals")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    loan_id = Column(Integer, ForeignKey("loans.id"), nullable=False)

    # Schedule
    scheduled_date = Column(Date, nullable=False)
    scheduled_amount = Column(Float, nullable=False)
    payment_type = Column(SqlEnum(PaymentType), nullable=False)

    # For amortizing payments, split into components
    scheduled_principal_component = Column(Float, nullable=True)
    scheduled_interest_component = Column(Float, nullable=True)

    # Actual
    paid_date = Column(Date, nullable=True)
    paid_amount = Column(Float, nullable=True)
    bank_reference = Column(String(128), nullable=True)

    status = Column(SqlEnum(PaymentStatus), nullable=False, default=PaymentStatus.SCHEDULED)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    loan = relationship("Loan", back_populates="payments")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    actor = Column(String(255), nullable=True)  # user identifier; "system" for automated actions
    action = Column(String(64), nullable=False)  # e.g. "create", "update", "delete"
    entity_type = Column(String(64), nullable=False)  # e.g. "loan", "payment"
    entity_id = Column(Integer, nullable=True)

    before_json = Column(Text, nullable=True)
    after_json = Column(Text, nullable=True)

    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)
    notes = Column(Text, nullable=True)


# =============================================================================
# Database engine and session helpers
# =============================================================================

DB_PATH = "data/bruno.db"
DB_URL = f"sqlite:///{DB_PATH}"


def get_engine(db_url: str = DB_URL):
    """Get a SQLAlchemy engine for the Bruno database."""
    return create_engine(db_url, echo=False, future=True)


def get_session_factory(engine=None):
    """Get a session factory bound to the engine."""
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def init_db(db_url: str = DB_URL):
    """Create all tables. Idempotent — won't recreate existing tables."""
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)
    return engine
