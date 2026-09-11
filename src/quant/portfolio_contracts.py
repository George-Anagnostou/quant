"""Agent-facing holdings commands. Money and quantities use decimal strings."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from quant.contracts import StrictModel, Text, Symbol

Amount = Annotated[Decimal, Field(max_digits=28, decimal_places=12, ge=0, le=10**15)]
Quantity = Annotated[Decimal, Field(max_digits=28, decimal_places=12, gt=0, le=10**15)]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class AccountInput(StrictModel):
    name: Text
    currency: Currency = 'USD'
    accountType: Text | None = None
    institution: Text | None = None


class LotInput(StrictModel):
    symbol: Symbol
    account: Text
    quantity: Quantity
    totalCost: Amount
    currency: Currency = 'USD'
    acquired: date | None = None
    description: Text | None = None
    cusip: Annotated[str, Field(pattern=r'^[A-Z0-9]{9}$')] | None = None
    assetClass: Text | None = None
    strategy: Text | None = None
    sector: Text | None = None
    sourceLotId: Text | None = None
    reportedPrice: Amount | None = None
    reportedValue: Amount | None = None
    priceDate: date | None = None
    original: dict[str, str] = Field(default_factory=dict)

    @field_validator('symbol', mode='before')
    @classmethod
    def normalize_symbol(cls, value):
        from quant.market_data import normalize_symbol
        return normalize_symbol(value) if isinstance(value, str) else value


class AccountAction(StrictModel):
    action: Literal['account']
    account: AccountInput


class AddAction(StrictModel):
    action: Literal['add', 'buy']
    lot: LotInput
    fees: Amount = Decimal(0)


class EditAction(StrictModel):
    action: Literal['edit']
    lotId: Text
    lot: LotInput


class RemoveAction(StrictModel):
    action: Literal['remove']
    lotId: Text


class SellAction(StrictModel):
    action: Literal['sell']
    lotId: Text | None = None
    sourceLotId: Text | None = None
    account: Text | None = None
    quantity: Quantity
    unitPrice: Amount
    fees: Amount = Decimal(0)

    @model_validator(mode='after')
    def explicit_lot(self):
        if self.lotId:
            if self.sourceLotId or self.account:
                raise ValueError('Use lotId or account plus sourceLotId, not both')
        elif not self.sourceLotId or not self.account:
            raise ValueError('Sale requires lotId or account plus sourceLotId')
        return self


class SplitAction(StrictModel):
    action: Literal['split']
    account: Text
    symbol: Symbol
    ratio: Quantity


class CashAction(StrictModel):
    action: Literal['cash', 'deposit', 'withdrawal', 'dividend', 'fee']
    account: Text
    amount: Amount
    symbol: Symbol | None = None


Action = Annotated[AccountAction | AddAction | EditAction | RemoveAction | SellAction | CashAction | SplitAction,
                   Field(discriminator='action')]


class PortfolioWrite(StrictModel):
    importKey: Text
    expectedRevision: Annotated[int, Field(strict=True, ge=0)]
    effectiveDate: date
    source: Text
    reason: Text
    actions: Annotated[list[Action], Field(min_length=1, max_length=1000)]

    @model_validator(mode='after')
    def validate_dates(self):
        if self.effectiveDate > date.today():
            raise ValueError('effectiveDate cannot be in the future')
        for action in self.actions:
            lot = getattr(action, 'lot', None)
            if lot:
                if lot.acquired and lot.acquired > self.effectiveDate:
                    raise ValueError('Acquisition cannot follow the effective date')
                if lot.priceDate and lot.priceDate > self.effectiveDate:
                    raise ValueError('Reported price cannot follow the effective date')
                if (lot.reportedPrice is not None or lot.reportedValue is not None) and lot.priceDate is None:
                    raise ValueError('Reported valuation requires priceDate')
            if isinstance(action, AddAction):
                if action.action == 'add' and action.fees:
                    raise ValueError('Opening lots use totalCost including any known fees')
                if action.action == 'buy' and lot.acquired != self.effectiveDate:
                    raise ValueError('A purchase requires acquired equal to effectiveDate')
        return self


class CurrentSnapshotInput(StrictModel):
    importKey: Text
    expectedRevision: Annotated[int, Field(strict=True, ge=0)]
    account: Text
    date: date


class ImportedTransaction(StrictModel):
    sourceId: Text
    date: date
    entry: Annotated[AddAction | SellAction | CashAction | SplitAction, Field(discriminator='action')]

    @model_validator(mode='after')
    def transactions_only(self):
        if self.entry.action in {'add', 'cash'}:
            raise ValueError('Transaction imports accept trades and cash movements, not opening balances or corrections')
        return self


class TransactionImport(StrictModel):
    importKey: Text
    expectedRevision: Annotated[int, Field(strict=True, ge=0)]
    source: Text
    reason: Text
    transactions: Annotated[list[ImportedTransaction], Field(min_length=1, max_length=1000)]

    @model_validator(mode='after')
    def unique_chronological(self):
        if len({t.sourceId for t in self.transactions}) != len(self.transactions):
            raise ValueError('Duplicate transaction sourceId')
        if [t.date for t in self.transactions] != sorted(t.date for t in self.transactions):
            raise ValueError('Transactions must be ordered by date; same-day order is preserved')
        return self


class NavInput(StrictModel):
    importKey: Text
    account: Text
    openingSnapshotId: Text
    closingSnapshotId: Text
    complete: bool = False
    benchmark: Symbol = 'SPY'


class ReportedNavInput(StrictModel):
    importKey: Text
    account: Text
    date: date
    currency: Literal['USD'] = 'USD'
    value: Amount
    source: Text

    @field_validator('date')
    @classmethod
    def past_date(cls, value):
        if value > date.today():
            raise ValueError('Reported NAV cannot be in the future')
        return value


class HoldingsScenario(StrictModel):
    account: Text
    date: date
    dimension: Literal['symbol','sector','strategy'] = 'symbol'
    shocks: Annotated[dict[str, Annotated[float, Field(ge=-1, le=10, allow_inf_nan=False)]], Field(min_length=1, max_length=200)]


class SaleSimulation(StrictModel):
    account: Text
    date: date
    sales: Annotated[list[SellAction], Field(min_length=1, max_length=200)]


class PositionValuation(StrictModel):
    symbol: str
    quantity: float
    totalCost: float
    averageCost: float
    lotCount: int
    unpricedLotCount: int
    pricedMarketValue: float
    marketValue: float | None
    unrealizedGain: float | None
    unrealizedReturn: float | None
    dayGain: float | None
    dayReturn: float | None
    lastPrice: float | None
    priceDate: str | None
    weight: float | None
    recordedRealizedGain: float


class AllocationValue(StrictModel):
    name: str
    lotCount: int
    totalCost: float
    pricedMarketValue: float
    marketValue: float | None
    unpricedLotCount: int
    unrealizedGain: float | None
    unrealizedReturn: float | None
    dayGain: float | None
    weight: float | None


class AnalysisSummary(StrictModel):
    totalCost: float
    pricedSecuritiesValue: float
    securitiesValue: float | None
    knownCash: float
    cash: float | None
    accountValue: float | None
    unrealizedGain: float | None
    unrealizedReturn: float | None
    unpricedLotCount: int
    recordedRealizedGain: float


class PortfolioAnalysisData(StrictModel):
    date: str
    revision: int
    account: str | None
    currency: Literal['USD']
    positions: list[PositionValuation]
    lots: list[dict]
    accounts: list[dict]
    summary: AnalysisSummary
    allocations: dict[str,list[AllocationValue]]
    recordedRealizedGainsBySymbol: dict[str,float]
    methodologyVersion: str
    provider: str
    priceBasis: str
    warnings: list[str]
    conventions: dict[str,str]


class TransactionData(StrictModel):
    id: str
    revision: int
    sourceId: str
    source: str
    date: str
    account: str
    kind: Literal['buy','sell','deposit','withdrawal','dividend','fee','split']
    symbol: str | None
    lotId: str | None
    quantity: str | None
    amount: str
    fees: str
    removedCost: str | None
    realizedGain: str | None
    ratio: str | None


class AccountRiskData(StrictModel):
    timeWeightedReturn: float | None
    annualizedVolatility: float | None
    sharpeRatio: float | None
    sortinoRatio: float | None
    maxDrawdown: float | None
    observations: int
    beta: float | None
    annualizedAlpha: float | None
    benchmarkObservations: int


class PerformanceData(StrictModel):
    id: str
    date: str
    account: str
    currency: Literal['USD']
    performanceAvailable: bool
    reconciled: bool
    risk: AccountRiskData | None
    timeWeightedReturn: float | None
    moneyWeightedReturnAnnualized: float | None
    profitContributions: list[dict]
    reportedComparisons: list[dict]
    warnings: list[str]
    methodologyVersion: str
    priceBasis: str | None
    priceDigest: str | None
    sourceRevision: int | None
