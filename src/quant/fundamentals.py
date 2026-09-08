"""Explicit SEC ingestion boundary and dated research evidence services."""
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from urllib.request import Request, urlopen

from pydantic import TypeAdapter

from quant.contracts import EvidenceInput, Symbol, ThesisInput
from quant.record_store import RecordRepository

_SYMBOL = TypeAdapter(Symbol)


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class SECProvider:
    def __init__(self, user_agent=None, transport=None):
        self.user_agent = user_agent or os.environ.get("QUANT_SEC_USER_AGENT")
        self.transport = transport

    def get(self, path):
        if self.transport:
            return self.transport(path)
        if not self.user_agent:
            raise ValueError("Set QUANT_SEC_USER_AGENT to an identifying name and contact email")
        request = Request("https://data.sec.gov/"+path,
                          headers={"User-Agent":self.user_agent, "Accept":"application/json"})
        with urlopen(request, timeout=30) as response:
            content = response.read(32*1024*1024+1)
        if len(content) > 32*1024*1024:
            raise ValueError("SEC response exceeds 32 MiB")
        return json.loads(content)


class FundamentalService:
    def __init__(self, path, provider=None):
        self.store = RecordRepository(path)
        self.provider = provider or SECProvider()

    def save(self, body: EvidenceInput):
        if body.category == "facts":
            facts=body.data.get("facts")
            if not isinstance(facts,list):
                raise ValueError("Financial evidence requires a facts list")
            for fact in facts:
                if not isinstance(fact, dict):
                    raise ValueError("Each financial fact must be an object")
                value=fact.get("val")
                if any(not isinstance(fact.get(key), str) or not fact[key].strip() for key in ("tag", "unit")) or not _finite_number(value):
                    raise ValueError("Facts require tag, unit and finite numeric val")
                periods = {}
                for key in ("start","end"):
                    if fact.get(key) is not None:
                        if not isinstance(fact[key], str):
                            raise ValueError("Fact dates must use YYYY-MM-DD strings")
                        periods[key] = date.fromisoformat(fact[key])
                        if periods[key].isoformat() != fact[key]:
                            raise ValueError("Fact dates must use YYYY-MM-DD strings")
                if "start" in periods and "end" in periods and periods["start"] > periods["end"]:
                    raise ValueError("Fact period start must not follow end")
        if body.category == "fund_holdings":
            holdings = body.data.get("holdings")
            if not isinstance(holdings, list) or not holdings or len(holdings) > 10000:
                raise ValueError("Fund evidence requires a nonempty holdings list")
            seen, total = set(), 0.0
            for row in holdings:
                if not isinstance(row, dict):
                    raise ValueError("Each fund holding must be an object")
                symbol, weight = row.get("symbol"), row.get("weight")
                symbol = _SYMBOL.validate_python(symbol)
                if symbol in seen:
                    raise ValueError("Fund constituents must be unique symbols")
                if not _finite_number(weight) or not 0 <= weight <= 1:
                    raise ValueError("Fund weights must be finite fractions")
                seen.add(symbol)
                total += weight
            if total > 1.000001:
                raise ValueError("Fund holdings exceed 100%")
        return self.store.put("evidence", body.importKey, body.model_dump(mode="json"))

    def ingest_sec(self, symbol, cik):
        if not cik.isdigit() or len(cik) > 10:
            raise ValueError("CIK must contain at most ten digits")
        cik = cik.zfill(10)
        filings = self.provider.get(f"submissions/CIK{cik}.json")
        if symbol not in filings.get("tickers", []):
            raise ValueError("SEC issuer does not identify the requested ticker")
        facts = self.provider.get(f"api/xbrl/companyfacts/CIK{cik}.json")
        recent = filings["filings"]["recent"]
        saved = []
        for index, accession in enumerate(recent.get("accessionNumber", [])):
            if recent["form"][index] not in {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A"}:
                continue
            if len(saved) >= 20:
                break
            accepted = recent.get("acceptanceDateTime", [None]*len(recent["accessionNumber"]))[index]
            availability = (datetime.fromisoformat(accepted.replace("Z", "+00:00")) if accepted
                            else datetime.fromisoformat(recent["filingDate"][index]).replace(tzinfo=timezone.utc)+timedelta(days=1))
            if availability.tzinfo is None:
                raise ValueError("SEC acceptance timestamp lacks timezone")
            observations = []
            for taxonomy, concepts in facts.get("facts", {}).items():
                for tag, concept in concepts.items():
                    for unit, values in concept.get("units", {}).items():
                        observations.extend({"taxonomy":taxonomy, "tag":tag, "unit":unit, **v}
                                            for v in values if v.get("accn") == accession)
            document = recent["primaryDocument"][index]
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"
            payload = EvidenceInput(importKey=f"sec:{cik}:{accession}", symbol=symbol,
                category="facts", source="SEC EDGAR", sourceUrl=url,
                availableAt=availability, accession=accession,
                periodEnd=recent["reportDate"][index] or None,
                data={"cik":cik, "form":recent["form"][index], "filed":recent["filingDate"][index],
                      "facts":observations, "availabilityPrecision":"timestamp" if accepted else "conservative_next_day"})
            existing = self.store.by_key("evidence", payload.importKey)
            if existing and existing["payload"] != payload.model_dump(mode="json"):
                # Preserve revisions to the extraction, not just formal amended filings.
                import hashlib
                from quant.record_store import canonical
                digest = hashlib.sha256(canonical(payload.model_dump(mode="json")).encode()).hexdigest()[:16]
                payload.importKey += ":"+digest
                revision=self.store.by_key("evidence",payload.importKey)
                if revision:
                    saved.append(revision)
                    continue
                original_availability=payload.availableAt.isoformat()
                payload.availableAt=datetime.now(timezone.utc)
                payload.data={**payload.data,"originalFilingAvailableAt":original_availability,
                              "availabilityPrecision":"extraction_revision_observed"}
            saved.append(self.save(payload))
        return {"records":saved, "coverage":"Most recent 20 annual/quarterly filings in SEC recent submissions; custom taxonomy facts may be absent"}

    def evidence(self, symbol, as_of=None):
        from quant.database import database_connection
        with database_connection(self.store.path, read_only=True) as db:
            rows = db.execute("""SELECT * FROM records WHERE user_id=? AND kind='evidence'
                AND json_extract(payload,'$.symbol')=? ORDER BY created_at,id""", (self.store.user_id,symbol)).fetchall()
        records = [self.store._decode(row) for row in rows]
        if as_of:
            cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                raise ValueError("asOf requires timezone")
            records = [r for r in records if datetime.fromisoformat(r["payload"]["availableAt"].replace("Z", "+00:00")) <= cutoff]
        return records

    def summary(self, symbol, as_of=None):
        evidence = self.evidence(symbol, as_of)
        tags = {"revenues":["RevenueFromContractWithCustomerExcludingAssessedTax","Revenues","SalesRevenueNet"],
                "netIncome":["NetIncomeLoss"], "operatingCashFlow":["NetCashProvidedByUsedInOperatingActivities"],
                "operatingIncome":["OperatingIncomeLoss"],
                "capitalExpenditures":["PaymentsToAcquirePropertyPlantAndEquipment"],
                "debt":["LongTermDebtCurrent","LongTermDebtNoncurrent"],
                "shares":["CommonStockSharesOutstanding"]}
        observations = []
        for record in evidence:
            if record["payload"]["category"] != "facts":
                continue
            for metric, alternatives in tags.items():
                for fact in record["payload"]["data"].get("facts", []):
                    if fact["tag"] in alternatives:
                        observations.append({**fact, "metric":metric, "evidenceId":record["id"],
                                             "availableAt":record["payload"]["availableAt"], "retrievedAt":record["createdAt"]})
        return {"symbol":symbol, "observations":observations,
                "periodMetrics":company_metrics(observations),
                "balanceMetrics":balance_metrics(observations),
                "warnings":["Fiscal periods, units and tags must match before comparison; alternative tags are not additive",
                            "Missing facts and sector-specific accounting are not estimated"]}

    def thesis(self, body: ThesisInput):
        for identifier in body.evidenceIds:
            self.store.get("evidence", identifier)
        if body.supersedes:
            old = self.store.get("thesis", body.supersedes)
            if old["payload"]["symbol"] != body.symbol:
                raise ValueError("A thesis revision must retain its symbol")
        return self.store.put("thesis", body.importKey, body.model_dump(mode="json"))

    def fund_overlap(self, snapshot):
        exposure, missing = {}, []
        values = [p["marketValue"] for p in snapshot["positions"]]
        if any(v is None for v in values) or not sum(values):
            raise ValueError("Fund look-through requires observed position market values")
        total = sum(values)
        coverage = 0
        for position in snapshot["positions"]:
            weight = position["marketValue"]/total
            rows = [r for r in self.evidence(position["symbol"], snapshot["date"]+"T23:59:59+00:00")
                    if r["payload"]["category"] == "fund_holdings"
                    and r["payload"]["periodEnd"] and r["payload"]["periodEnd"] <= snapshot["date"]]
            if not rows:
                if position["assetClass"] == "equity":
                    exposure[position["symbol"]] = exposure.get(position["symbol"],0)+weight
                    coverage += weight
                else:
                    missing.append(position["symbol"])
                continue
            row = max(rows, key=lambda r:(r["payload"]["periodEnd"],r["createdAt"]))
            for holding in row["payload"]["data"]["holdings"]:
                value = weight*holding["weight"]
                exposure[holding["symbol"]] = exposure.get(holding["symbol"],0)+value
                coverage += value
        return {"exposures":sorted(({"symbol":s,"weight":w} for s,w in exposure.items()), key=lambda r:(-r["weight"],r["symbol"])),
                "coverage":coverage, "unresolvedSymbols":missing, "basis":"observed_priced_securities"}


def company_metrics(observations):
    """Calculate only matched-duration USD flow metrics, retaining source ids."""
    from datetime import date
    chosen = {}
    priorities = {"RevenueFromContractWithCustomerExcludingAssessedTax":3,"Revenues":2,"SalesRevenueNet":1}
    for row in observations:
        if row.get("unit") != "USD" or not row.get("start") or not row.get("end"):
            continue
        duration = (date.fromisoformat(row["end"])-date.fromisoformat(row["start"])).days
        if not (60<=duration<=120 or 300<=duration<=400):
            continue
        key = (row["start"],row["end"],row["metric"])
        priority = (row["availableAt"],priorities.get(row["tag"],0),row["retrievedAt"])
        if key not in chosen or priority>chosen[key][0]:
            chosen[key]=(priority,row)
    periods = sorted({key[:2] for key in chosen})
    output=[]
    for start,end in periods:
        metrics={key[2]:value[1] for key,value in chosen.items() if key[:2]==(start,end)}
        value=lambda name: metrics[name]["val"] if name in metrics else None
        revenue,income,operating,cfo,capex = (value(n) for n in ("revenues","netIncome","operatingIncome","operatingCashFlow","capitalExpenditures"))
        row={"start":start,"end":end,"period":"annual" if (date.fromisoformat(end)-date.fromisoformat(start)).days>=300 else "quarterly",
            "revenue":revenue,"netMargin":income/revenue if income is not None and revenue and revenue>0 else None,
            "operatingMargin":operating/revenue if operating is not None and revenue and revenue>0 else None,
            "operatingCashFlow":cfo,"freeCashFlowProxy":cfo-capex if cfo is not None and capex is not None else None,
            "cashConversion":cfo/income if cfo is not None and income and income>0 else None,
            "evidenceIds":sorted({r["evidenceId"] for r in metrics.values()})}
        prior=[r for r in output if r["period"]==row["period"] and 330<=(date.fromisoformat(end)-date.fromisoformat(r["end"])).days<=400 and r["revenue"] and r["revenue"]>0]
        row["revenueGrowthYearOverYear"]=revenue/prior[-1]["revenue"]-1 if prior and revenue is not None else None
        output.append(row)
    return output


def valuation_sensitivity(body):
    rows=[]
    for case in body.cases:
        pv=sum(body.freeCashFlowToFirm*(1+case.growth)**year/(1+case.discountRate)**year for year in range(1,body.years+1))
        terminal=body.freeCashFlowToFirm*(1+case.growth)**body.years*(1+case.terminalGrowth)/(case.discountRate-case.terminalGrowth)
        enterprise=pv+terminal/(1+case.discountRate)**body.years
        equity=enterprise+body.cash-body.debt
        rows.append({"name":case.name,"enterpriseValue":enterprise,"equityValue":equity,"valuePerShare":equity/body.shares})
    return {"methodologyVersion":"constant-growth-fcff-v1","assumptions":body.model_dump(mode="json"),"cases":rows,
            "warnings":["User-supplied FCFF and rates; operating cash flow minus capex is not automatically FCFF", "Sensitivity illustration, not an estimated target price"]}


def balance_metrics(observations):
    from datetime import date
    chosen={}
    for row in observations:
        if row.get("start") or not row.get("end") or row["metric"] not in {"debt","shares"}:
            continue
        if row.get("unit") != ("shares" if row["metric"]=="shares" else "USD"):
            continue
        key=(row["end"],row["tag"])
        if key not in chosen or (row["availableAt"],row["retrievedAt"])>(chosen[key]["availableAt"],chosen[key]["retrievedAt"]):
            chosen[key]=row
    output=[]
    for end in sorted({key[0] for key in chosen}):
        group={tag:value for (day,tag),value in chosen.items() if day==end}
        current,noncurrent=group.get("LongTermDebtCurrent"),group.get("LongTermDebtNoncurrent")
        shares=group.get("CommonStockSharesOutstanding")
        row={"date":end,"sharesOutstanding":shares["val"] if shares else None,
             "longTermDebtIncludingCurrent":current["val"]+noncurrent["val"] if current and noncurrent else None,
             "evidenceIds":sorted({r["evidenceId"] for r in group.values()})}
        prior=[r for r in output if r["sharesOutstanding"] and r["sharesOutstanding"]>0 and 330<=(date.fromisoformat(end)-date.fromisoformat(r["date"])).days<=400]
        row["shareCountGrowthYearOverYear"]=row["sharesOutstanding"]/prior[-1]["sharesOutstanding"]-1 if prior and shares else None
        output.append(row)
    return output
