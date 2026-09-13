# Put the dashboard online without installing anything

This release includes the password-protected dashboard and a continuously running
collector in one app. You can deploy and operate it from a browser. It remains
paper-only and cannot move real funds.

## Browser-only setup with Render

1. Unzip this package on your laptop. No administrator permissions are needed.
2. In GitHub, create a **private repository**. Choose **Add file → Upload files**.
   Upload the contents of `dualchain-radar`, including its `radar` and `docs`
   folders. `render.yaml`, `pyproject.toml`, and `config.toml` must be at the
   repository root, not inside another `dualchain-radar` folder. Commit the files.
3. In [Render](https://dashboard.render.com/), choose **New → Blueprint** and connect
   that repository. Render reads `render.yaml`.
4. Set `DASHBOARD_PASSWORD` to a unique password with at least 16 characters.
   Set it only in Render's secret environment settings, never in GitHub.
5. Review the paid web-service and persistent-disk charges shown by Render, then
   deploy if acceptable. The included plan is an always-on paid service with a
   1 GB disk. It is not a free deployment.
6. Open the resulting HTTPS service URL and sign in with that password. Choose
   **View demo** to explore, or **Start paper trading** to collect real market data
   and place simulated orders.

The app automatically uses Render's `RENDER_EXTERNAL_URL` as its trusted origin.
For a custom domain, set `DASHBOARD_ORIGIN` to the exact HTTPS origin you use,
for example `https://radar.example.com` (no trailing path).

No terminal or Docker installation is needed on your laptop. This package has
not been deployed into your account; a hosting account and the steps above are
still required. Render's plan availability/pricing may change; use the provider's
deployment screen as the source of current charges.

## Using the dashboard

* **Start paper trading** starts the collector and enables eligible paper entries.
* **Pause & exit** cancels pending buys and requests exits for existing positions
  on later valid prices. It leaves collection running. It does not guarantee an
  immediate exit when a pool has no liquidity or prices are unavailable.
* **Settings → Stop collection** is allowed only when positions and pending
  orders are clear. This avoids quietly abandoning active paper positions.
* **Settings → Create experiment** changes strategy, size, filters or targets by
  starting a fresh simulated account. Previous experiments remain available in
  the history menu. Stop collection first. This is not a way to modify an open
  position's risk controls mid-trade.
* **Activity** shows recent decisions/fills and downloads up to 50,000 records.
* **View demo** is read-only synthetic data, isolated from your account/database.
* Closing your laptop does not stop the hosted collector. After a server restart,
  a previously requested collector resumes with its persisted pause/risk state.
* Signing out does not stop collection. Sessions expire after 12 hours or a server
  restart. Changing the password in the hosting settings and restarting invalidates
  existing sessions.

## Storage, backups, and operations

The disk retains SQLite databases, experiment settings and collection status.
Do not remove the disk or run multiple replicas/processes over the same directory.
Use one web process only; the collector is a thread within that process. The
provided setup does not support horizontal scaling. The dashboard is single-user.

The public `/healthz` route verifies that the web service responds, not that data
feeds are healthy. Check each chain's collection timestamp/error in the dashboard.
Zero eligible entries can be normal: missing verified market caps and buyer counts
are rejected. Enabling the FDV option uses a different measure and is labeled as such.

The database grows with collection. Monitor disk capacity; use SQLite's backup
API for consistent backups. The app does not implement scheduled offsite backups
or automatic retention. Provider disk snapshots are not a substitute for verifying
a restore. Keep snapshots if you later need full strategy replay.

## Optional local developer run

This is only for a machine where you can install Python dependencies:

```bash
pip install -e '.[dev]'
export DASHBOARD_PASSWORD='replace-with-your-own-long-password'
export DASHBOARD_ORIGIN='http://127.0.0.1:8080'
dualradar-web
```

Open `http://127.0.0.1:8080`. Never use the example password for hosting.
For Docker, provide these environment variables and mount writable persistent
storage at `/app/data` (owned by the container's `radar` user). Public access must
be behind HTTPS. The Render blueprint uses native Python and handles its disk
independently of the Dockerfile.

Sources checked when preparing this package:
[Render Blueprint reference](https://render.com/docs/blueprint-spec),
[persistent disks](https://render.com/docs/disks).
