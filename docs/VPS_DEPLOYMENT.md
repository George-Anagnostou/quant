# Private VPS deployment

Quant runs as one long-lived process: FastAPI serves stored data while one
background thread owns scheduled ingestion. This template targets a single VPS
with SQLite on local persistent storage. It is not a multi-host deployment.

## Security boundary

Keep Quant bound to `127.0.0.1`. Access it through SSH forwarding, a private VPN,
or an authenticated reverse proxy. Do not expose the current `local-admin` write
surface directly to the Internet; hosted authentication is not implemented.

Run exactly one `quant serve` process against a database. Do not configure multiple
Uvicorn workers. SQLite and the ingestion lock protect one host, not a distributed
deployment.

## systemd setup

Install the source at `/opt/quant`, create a dedicated non-login `quant` user, and
create its environment with `uv sync --locked`. The supplied
[`deploy/quant.service`](../deploy/quant.service) assumes that layout and stores
the database at `/var/lib/quant/quant.db`. Frozen research artifacts are retained
alongside it in `/var/lib/quant/artifacts`.

```sh
sudo useradd --system --user-group --home-dir /var/lib/quant --shell /usr/sbin/nologin quant
sudo install -m 0644 deploy/quant.service /etc/systemd/system/quant.service
sudo systemctl daemon-reload
sudo systemctl enable --now quant
sudo systemctl status quant
sudo journalctl -u quant --follow
```

The service restarts after process failure. On startup, the worker recovers every
durable ingestion run still marked `running`, then schedules one run for each new
completed US equity session. Provider availability is conservatively treated as
20:00 Eastern. Each completed daily cycle creates a verified local SQLite backup.

## Operations

Check process liveness and persisted data separately:

```sh
curl --fail http://127.0.0.1:8001/api/health
uv run quant query quality
uv run quant query gaps
```

`/api/health` only confirms that the HTTP process responds. Review `quality`,
`gaps`, and ingestion-run status to determine whether data is current and whether
provider failures or coverage gaps remain. Failed jobs are retained as durable,
visible failures; a later execution may retry them without silently treating them
as complete.

Local verified backups are not disaster recovery. Select an off-host destination
and retention policy before relying on the VPS for irreplaceable personal records.
Archive `quant.db`, its SQLite backups, and the sibling `artifacts` directory
together so frozen-run replay remains possible.
