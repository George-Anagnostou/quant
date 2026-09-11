"""Strict public input contracts for explicit research and portfolio writes."""
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=200)]
Symbol = Annotated[str, Field(pattern=r"^[A-Z0-9^][A-Z0-9.^=_-]{0,31}$")]
Positive = Annotated[FiniteFloat, Field(gt=0)]
Nonnegative = Annotated[FiniteFloat, Field(ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Position(StrictModel):
    symbol: Symbol
    quantity: Positive
    averageCost: Nonnegative | None = None
    marketValue: Nonnegative | None = None
    sector: Text | None = None
    strategy: Text | None = None
    assetClass: Text | None = None
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")] = "USD"


class SnapshotInput(StrictModel):
    importKey: Text
    date: date
    account: Text
    source: Text
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")] = "USD"
    cash: FiniteFloat | None = None
    positions: Annotated[list[Position], Field(max_length=200)]
    original: Annotated[str, Field(max_length=1_000_000)] | None = None

    @field_validator("date")
    @classmethod
    def not_future(cls, value):
        if value > date.today():
            raise ValueError("Snapshot cannot be in the future")
        return value

    @model_validator(mode="after")
    def unique_symbols(self):
        if len({p.symbol for p in self.positions}) != len(self.positions):
            raise ValueError("Aggregate lots by symbol within each account snapshot")
        return self


class RunInput(StrictModel):
    importKey: Text
    snapshotId: Text
    period: Literal["1mo", "3mo", "6mo", "1y", "2y", "5y"] = "1y"
    benchmark: Symbol = "SPY"


class Claim(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=5000)]
    kind: Literal["fact", "interpretation", "hypothesis"]
    evidence: Annotated[list[Text], Field(min_length=1, max_length=50)]


class ReportInput(StrictModel):
    importKey: Text
    runId: Text
    model: Text
    claims: Annotated[list[Claim], Field(min_length=1, max_length=100)]
    counterarguments: Annotated[list[str], Field(max_length=100)]
    openQuestions: Annotated[list[str], Field(max_length=100)]
    acknowledgedWarnings: list[Text]
    narrative: Annotated[str, Field(max_length=100_000)]


class EvidenceInput(StrictModel):
    importKey: Text
    symbol: Symbol
    category: Literal["filing", "facts", "fund_holdings", "document"]
    source: Text
    sourceUrl: Annotated[str, Field(min_length=1, max_length=2000)]
    availableAt: datetime
    periodEnd: date | None = None
    accession: Text | None = None
    data: dict

    @field_validator("availableAt")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("availableAt requires a timezone")
        if value > datetime.now(timezone.utc):
            raise ValueError("Evidence cannot be available in the future")
        return value.astimezone(timezone.utc)


class ThesisInput(StrictModel):
    importKey: Text
    symbol: Symbol
    statement: Annotated[str, Field(min_length=1, max_length=10000)]
    evidenceIds: Annotated[list[Text], Field(min_length=1, max_length=100)]
    counterarguments: list[str]
    assumptions: dict[str, FiniteFloat | str]
    invalidationConditions: Annotated[list[str], Field(min_length=1, max_length=100)]
    supersedes: Text | None = None


class LedgerEvent(StrictModel):
    sourceId: Text
    date: date
    kind: Literal["buy", "sell", "dividend", "fee", "deposit", "withdrawal", "transfer_in", "transfer_out", "split"]
    symbol: Symbol | None = None
    quantity: Positive | None = None
    amount: Nonnegative = 0
    ratio: Positive | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.kind in {"buy", "sell"} and (not self.symbol or not self.quantity):
            raise ValueError("Trades require symbol and quantity; amount is total consideration")
        if self.kind == "split" and (not self.symbol or not self.ratio or self.amount != 0):
            raise ValueError("Splits require symbol and ratio and zero cash amount")
        if self.kind == "dividend" and not self.symbol:
            raise ValueError("Dividends require symbol")
        if self.kind not in {"buy", "sell"} and self.quantity is not None:
            raise ValueError("Only trades accept quantity")
        if self.kind != "split" and self.ratio is not None:
            raise ValueError("Only splits accept ratio")
        return self


class LedgerInput(StrictModel):
    importKey: Text
    account: Text
    source: Text
    currency: Literal["USD"] = "USD"
    openingSnapshotId: Text
    closingSnapshotId: Text
    complete: bool = False
    events: Annotated[list[LedgerEvent], Field(max_length=10000)]
    original: Annotated[str, Field(max_length=1_000_000)] | None = None
    supersedes: Text | None = None

    @model_validator(mode="after")
    def unique_event_ids(self):
        if len({event.sourceId for event in self.events}) != len(self.events):
            raise ValueError("Transaction source identities must be unique within an import")
        return self


class ScenarioInput(StrictModel):
    snapshotId: Text
    shocks: Annotated[dict[str, FiniteFloat], Field(min_length=1, max_length=200)]


class ExperimentInput(StrictModel):
    importKey: Text
    runId: Text
    hypothesis: Annotated[str, Field(min_length=1, max_length=10000)]
    symbol: Symbol
    trainingEnd: date
    lookback: Annotated[int, Field(ge=2, le=252)] = 20
    costBps: Annotated[FiniteFloat, Field(ge=0, le=1000)] = 10


class EvaluationInput(StrictModel):
    importKey: Text
    reportId: Text
    factualAccuracy: Annotated[FiniteFloat, Field(ge=0, le=1)]
    numericalConsistency: Annotated[FiniteFloat, Field(ge=0, le=1)]
    evidenceUse: Annotated[FiniteFloat, Field(ge=0, le=1)]
    missingDataHandling: Annotated[FiniteFloat, Field(ge=0, le=1)]
    costUsd: Nonnegative
    evaluator: Text
    notes: Annotated[str, Field(max_length=10000)]


class SecurityMetadataInput(StrictModel):
    importKey: Text
    symbol: Symbol
    currency: Annotated[str, Field(pattern="^[A-Z]{3}$")]
    calendar: Literal["XNYS"] | None
    instrumentType: Literal["equity", "etf", "index", "other"]
    source: Text


class ValuationCase(StrictModel):
    name: Text
    growth: Annotated[FiniteFloat, Field(gt=-1,le=1)]
    discountRate: Annotated[FiniteFloat, Field(gt=0,le=1)]
    terminalGrowth: Annotated[FiniteFloat, Field(gt=-1,le=0.1)]

    @model_validator(mode="after")
    def valid_rates(self):
        if self.terminalGrowth >= self.discountRate:
            raise ValueError("Terminal growth must be below discount rate")
        return self


class ValuationInput(StrictModel):
    freeCashFlowToFirm: Positive
    shares: Positive
    cash: Nonnegative
    debt: Nonnegative
    years: Annotated[int,Field(ge=1,le=20)] = 5
    cases: Annotated[list[ValuationCase],Field(min_length=1,max_length=20)]
