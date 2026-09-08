# Manual acceptance tests

Run these in order from the repository root on `master`. Allow about 15–20 minutes.
The default path is entirely offline: synthetic prices, an isolated database,
and an API server on port **8011**. It does not use `data/quant.db`.
The setup script refuses to overwrite an existing directory's contents.

These tests exercise the current CLI/API and data behavior. The dashboard remains
a compatibility client; imported snapshots are separate from its legacy lots.

## Setup — one terminal, synthetic data

```sh
git status --short --branch
uv sync
export QUANT_MANUAL_DIR="$(mktemp -d /tmp/quant-manual.XXXXXX)"
uv run python scripts/prepare_manual_tests.py --directory "$QUANT_MANUAL_DIR"
export QUANT_MANUAL_API="http://127.0.0.1:8011/api/alpha"

q() { uv run quant query --base-url "$QUANT_MANUAL_API" "$@"; }
record_id() {
  uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["id"])' "$1"
}

uv run quant serve --no-sync --port 8011 --database "$QUANT_MANUAL_DIR/manual-quant.db" > "$QUANT_MANUAL_DIR/server.log" 2>&1 &
QUANT_MANUAL_PID=$!
```

Wait for `Application startup complete` in `cat "$QUANT_MANUAL_DIR/server.log"`.
If the log says the port is occupied, stop here and select another unused port in
both server and API settings. Keep this terminal open: subsequent commands reuse
its variables and helper functions. All expected values are also recorded in
`$QUANT_MANUAL_DIR/expected.json`.

## 1. Discover the API and inspect data

```sh
q health --pretty
q capabilities --pretty
q quotes AAPL SPY --pretty
q quality --pretty
```

**Pass:** Responses have `data`, `meta`, and `warnings`. Capabilities include
`portfolio_readiness`. AAPL's closing price is **150**, SPY's is **600**, and both
are dated **2026-08-31**. Quality can correctly call these prices stale relative
to today; later tests assess readiness for the dated August snapshot.

Open [the API explorer](http://127.0.0.1:8011/api/alpha/docs). Find the snapshot
readiness route and its `ReadinessData` schema. The [dashboard](http://127.0.0.1:8011)
should load; its holdings can remain empty because no legacy lots were imported.
Stay on analytical views for this offline test; research views can call providers.

## 2. Import a snapshot and retry it

```sh
q request portfolio/snapshots --method POST --json-file "$QUANT_MANUAL_DIR/snapshot.json" > "$QUANT_MANUAL_DIR/snapshot-response.json"
cat "$QUANT_MANUAL_DIR/snapshot-response.json"
QUANT_SNAPSHOT_ID="$(record_id "$QUANT_MANUAL_DIR/snapshot-response.json")"
q request portfolio/snapshots --method POST --json-file "$QUANT_MANUAL_DIR/snapshot.json"
q snapshots --pretty
```

**Pass:** The second import returns the **same id**. The snapshot list contains
one snapshot, with 10 AAPL shares and USD 500 cash. Retrying does not duplicate it.

## 3. Reject conflicting and invalid requests

Run each separately, then check `echo $?` immediately after it:

```sh
q request portfolio/snapshots --method POST --json-file "$QUANT_MANUAL_DIR/snapshot-conflict.json"
q request portfolio/snapshots --method POST --json-file "$QUANT_MANUAL_DIR/snapshot-invalid.json"
q request 'capabilities?typo=true'
```

**Pass:** Each prints a structured error to stderr and exits **5**, with HTTP
**422**. The first changes cash under an existing import key; the second duplicates
a position symbol; the third uses an unknown query parameter. `q snapshots`
still contains only the original snapshot. These failures are expected.

## 4. Distinguish complete and insufficient risk history

```sh
q readiness "$QUANT_SNAPSHOT_ID" --period 1mo --pretty
q readiness "$QUANT_SNAPSHOT_ID" --period 1y --pretty
q request "portfolio/snapshots/$QUANT_SNAPSHOT_ID/review?period=1mo" --pretty
```

**Pass:** One-month readiness is **ready**, requires **22 price points**, and ends
on **2026-08-31**. One-year historical risk is **blocked** because the fixture only
starts in June; valuation remains ready and overall readiness is limited. Missing
session samples have at most 20 dates and actionable explanations.

The one-month review reports **accountValue = 2000**, **cash = 500**, AAPL market
value **1500**, a single-security concentration weight of **1**, and **21** risk
return observations. Warnings distinguish the holdings backcast from actual
account performance and disclose zero-rate and cash-exclusion conventions.

## 5. Keep unknown cash unknown

```sh
q request portfolio/snapshots --method POST --json-file "$QUANT_MANUAL_DIR/snapshot-unknown-cash.json" > "$QUANT_MANUAL_DIR/unknown-response.json"
QUANT_UNKNOWN_ID="$(record_id "$QUANT_MANUAL_DIR/unknown-response.json")"
q readiness "$QUANT_UNKNOWN_ID" --period 1mo --pretty
q request "portfolio/snapshots/$QUANT_UNKNOWN_ID/review?period=1mo" --pretty
```

**Pass:** Account-value readiness is blocked, securities risk is ready, and overall
readiness is limited. The review's `accountValue` and `cash` are **null**, not zero.
It still reports the securities analysis and the `unknown_cash` warning.

## 6. Freeze a run and replay it

Prepare a request using the saved import response; this only formats JSON:

```sh
uv run python - "$QUANT_MANUAL_DIR" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
snapshot_id = json.loads((d / "snapshot-response.json").read_text())["data"]["id"]
(d / "run.json").write_text(json.dumps({
    "importKey": "manual-run", "snapshotId": snapshot_id,
    "period": "1mo", "benchmark": "SPY"
}))
PY
q request research/runs --method POST --json-file "$QUANT_MANUAL_DIR/run.json" > "$QUANT_MANUAL_DIR/run-response.json"
QUANT_RUN_ID="$(record_id "$QUANT_MANUAL_DIR/run-response.json")"
q request "research/runs/$QUANT_RUN_ID" --pretty
q request "research/runs/$QUANT_RUN_ID/readiness" --pretty
q request "research/runs/$QUANT_RUN_ID/replay" > "$QUANT_MANUAL_DIR/replay-response.json"
cat "$QUANT_MANUAL_DIR/replay-response.json"
```

**Pass:** The run contains dataset and calculation digests, a compact result, and a
full-record path. Frozen readiness is ready with the same dataset hash. Replay
reports **matches = true**. Reposting `run.json` returns the same run id.

## 7. Change live metadata; verify the frozen run is unaffected

```sh
q request securities/metadata --method POST --json-file "$QUANT_MANUAL_DIR/metadata-change.json"
q readiness "$QUANT_SNAPSHOT_ID" --period 1mo --pretty
q request "research/runs/$QUANT_RUN_ID/readiness" --pretty
q request "research/runs/$QUANT_RUN_ID/replay" > "$QUANT_MANUAL_DIR/replay-after-change.json"
cat "$QUANT_MANUAL_DIR/replay-after-change.json"
q request securities/metadata --method POST --json-file "$QUANT_MANUAL_DIR/metadata-restore.json"
q readiness "$QUANT_SNAPSHOT_ID" --period 1mo --pretty
```

**Pass:** The deliberate EUR/USD conflict blocks the live snapshot checks. Frozen
readiness stays ready, the dataset hash is unchanged, and replay still matches.
The new restore import returns live readiness to ready. The conflict and restoration
remain audited imports. Do this once per fixture: reusing either immutable import
key later will not reapply an old metadata change.

## 8. Require acknowledged warnings and retained evidence

```sh
uv run python - "$QUANT_MANUAL_DIR" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
run = json.loads((d / "run-response.json").read_text())["data"]
body = {
    "importKey": "manual-report", "runId": run["id"], "model": "human-manual-test",
    "claims": [{"text": "This synthetic account is valued at USD 2000.",
                "kind": "fact", "evidence": ["result/accountValue"]}],
    "counterarguments": ["Synthetic inputs do not establish investment effectiveness."],
    "openQuestions": ["Would a real account have the required history and metadata?"],
    "acknowledgedWarnings": sorted({w["code"] for w in run["result"]["warnings"]}),
    "narrative": "Manual evidence-link test. Historical risk is hypothetical, not actual account performance."
}
(d / "report.json").write_text(json.dumps(body))
(d / "report-missing-warnings.json").write_text(json.dumps({**body, "acknowledgedWarnings": []}))
(d / "report-invalid-evidence.json").write_text(json.dumps({**body, "claims": [
    {**body["claims"][0], "evidence": ["result/doesNotExist"]}
]}))
PY
q request research/reports --method POST --json-file "$QUANT_MANUAL_DIR/report-missing-warnings.json"
q request research/reports --method POST --json-file "$QUANT_MANUAL_DIR/report-invalid-evidence.json"
q request research/reports --method POST --json-file "$QUANT_MANUAL_DIR/report.json"
q request research/records/report --pretty
```

**Pass:** The first two writes fail with HTTP 422 / CLI exit 5. The valid report
saves exactly once and retains its evidence reference and all acknowledged warning
codes. This validates references, not whether the prose is a sound investment thesis.

## 9. Keep explicit synchronization queued while offline

```sh
q request data/sync --method POST --json-file "$QUANT_MANUAL_DIR/sync.json" > "$QUANT_MANUAL_DIR/sync-response.json"
QUANT_SYNC_ID="$(record_id "$QUANT_MANUAL_DIR/sync-response.json")"
q request "data/ingestion-runs/$QUANT_SYNC_ID" --pretty
q quality --pretty
```

**Pass:** Status is **queued**, with no jobs executed. `--no-sync` disables the
worker, including explicit queued work; analytical reads do not secretly start it.

## 10. Restart and back up

```sh
kill "$QUANT_MANUAL_PID"
wait "$QUANT_MANUAL_PID"
uv run quant serve --no-sync --port 8011 --database "$QUANT_MANUAL_DIR/manual-quant.db" > "$QUANT_MANUAL_DIR/server.log" 2>&1 &
QUANT_MANUAL_PID=$!
```

Wait for startup in the log, then:

```sh
q snapshots --pretty
q request research/records/report --pretty
q request "data/ingestion-runs/$QUANT_SYNC_ID" --pretty
q request "research/runs/$QUANT_RUN_ID/replay" > "$QUANT_MANUAL_DIR/replay-after-restart.json"
cat "$QUANT_MANUAL_DIR/replay-after-restart.json"
uv run quant data backup --database "$QUANT_MANUAL_DIR/manual-quant.db" --destination "$QUANT_MANUAL_DIR/manual-backup.db"
```

**Pass:** Both snapshots, the report, and queued synchronization survive. Replay
still matches. Backup reports **verified = true**. The database backup alone is
not a complete evidence archive: preserve the sibling `artifacts/` directory too.

Stop your test server when done with `kill "$QUANT_MANUAL_PID"` and
`wait "$QUANT_MANUAL_PID"`. Keep the printed test directory and logs if anything
failed. Starting a fresh empty directory is the simplest way to rerun all cases.

## Optional live ingestion check

Use a **different, empty database**; never mix provider data with the synthetic
fixture above. In a separate terminal, start:

```sh
QUANT_LIVE_DIR="$(mktemp -d /tmp/quant-live.XXXXXX)"
uv run quant serve --port 8012 --database "$QUANT_LIVE_DIR/quant.db" --horizon 2026-06-01
```

From another terminal, query `quant query quality`, `gaps`, and
`request data/ingestion-runs/ID` using `--base-url http://127.0.0.1:8012/api/alpha`.
Take `ID` from `data.lastIngestion.id` in quality after a run starts. Initial universe
discovery can take time; the API should remain available. This may request hundreds
of symbols and requires network/provider access. Errors or rate limits should appear
as inspectable outcomes; do not treat provider availability as a deterministic pass.
Stop with Ctrl-C. Automated tests cover synthetic interruption and adjustment-revision
failure cases; do not simulate these by damaging your personal database.

## Record your result

For each failed case, note the case number, command, expected/actual result,
`git rev-parse --short HEAD`, response JSON, and relevant server-log lines. Preserve
the temporary fixture directory. This helps distinguish CLI, server, data-coverage,
and calculation problems without sharing personal holdings.
