# Deploying the staging cloud

Target: a public **`https://<service>.onrender.com`** endpoint backed by a
persistent MongoDB Atlas cluster, that a packaged NZeroC agent MSI on a clean
Windows VM can enrol against and heartbeat to.

Everything up to "Deploy" can be validated locally on the `memory://` backend
first -- see [`README.md`](README.md) &sect; 2.

This is a **standalone repository**: `docker build .` from this repo root builds
the image, and `render.yaml` (`dockerContext: .`, `dockerfilePath: ./Dockerfile`)
deploys it -- no parent repository is referenced.

---

## 1. MongoDB Atlas M0 (free)

1. Create a free Atlas account → **Build a Database** → **M0** (free forever).
2. Pick a cloud provider + region close to your Render region (Render Free is in
   Oregon by default -- `render.yaml` sets `region: oregon`).
3. **Database Access** → add a database user (username + password). Give it
   `readWrite` on `nzerox_staging` (or `Atlas admin` for simplicity in staging).
4. **Network Access** → add `0.0.0.0/0` (staging only; lock this down for
   anything real) so Render's dynamic egress IPs can connect.
5. **Connect** → **Drivers** → copy the `mongodb+srv://…` connection string.
   Substitute the real password. It looks like:

   ```
   mongodb+srv://USER:PASS@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
   ```

   The `+srv` form needs SRV DNS lookups; `dnspython` (pinned in
   `requirements.txt`) provides them.

No collections or indexes need to be created by hand -- `ensure_indexes()` runs
on every startup and is idempotent.

---

## 2. Render service + secrets

You need a free Render account connected to the Git host this repo lives on.

**Option A -- Blueprint (recommended).** In the Render dashboard: **New →
Blueprint**, select this repo, and Render reads `render.yaml` at the repo root.
It creates one Docker web service on the Free plan and prompts for the two
`sync: false` secrets:

| env var | value |
| --- | --- |
| `MONGODB_URI` | the `mongodb+srv://…` string from step 1 |
| `STAGING_ADMIN_TOKEN` | a long random string: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

`MONGODB_DB`, `ENV_NAME` and `LOG_LEVEL` come from `render.yaml` as plain values.
Keep a copy of the admin token -- you need it for `/admin/*` and the dashboard.

**Option B -- manual service.** **New → Web Service** → connect the repo →
Runtime **Docker**, Dockerfile path `./Dockerfile`, Docker build context
directory `.` (this repo root -- the Dockerfile only `COPY`s `requirements.txt`
and the `staging_cloud/` package). Plan **Free**. Health check path `/healthz`.
Add the same env vars as above under **Environment**.

Render injects `PORT` at runtime and the app binds `0.0.0.0:$PORT` automatically
(`StagingSettings.port` reads `PORT`); the `EXPOSE 8080` in the Dockerfile is
just metadata and is ignored.

---

## 3. Deploy

Blueprint deploys on save. For a manual service, click **Manual Deploy → Deploy
latest commit** (or push to the tracked branch; `render.yaml` sets
`autoDeploy: false`, flip it to `true` in the dashboard if you want push-to-deploy).

Render builds the Docker image from the repo, runs it, and once `/healthz`
returns 200 marks the service **Live**. The service URL is
**`https://nzerox-staging-cloud.onrender.com`** (Render may append a random
suffix if the name is taken -- use whatever the dashboard shows).

---

## 4. Verify the deployment

```bash
BASE="https://nzerox-staging-cloud.onrender.com"
ADMIN="the admin token from step 2"

curl -s "$BASE/healthz"
# {"status":"ok","db":"ok","env":"staging"}   <- proves Atlas connectivity + SRV

curl -s -H "X-Admin-Token: $ADMIN" -X POST "$BASE/admin/pairing-codes" \
  -d '{"site_id":"site-vm","tenant_id":"tenant-vm"}'
# note the pc-... code
```

First hit after an idle period pays a cold start (see step 6) -- give `curl` a
generous timeout or just retry once.

---

## 4a. Watch the live video (end-to-end)

Once an enrolled agent is running with `streaming.enabled: true` and pointed at
`$BASE`, it POSTs a ~2 s MPEG-TS segment per camera to
`POST /v1/ingest/video/{site}/{camera}`. The staging cloud keeps the most recent
`VIDEO_MAX_SEGMENTS_PER_CAMERA` (default 20) per camera **in memory only**
(Atlas is a poor fit for video bytes; Render Free's disk is ephemeral and spins
down when idle -- so this is intentionally lost on any restart).

Open in a browser (the admin token goes in the query string so the browser's
HLS fetches stay authenticated):

```
$BASE/video?token=$ADMIN                      # per-camera status, auto-refreshing
$BASE/video/{site}/{camera}?token=$ADMIN      # live player (hls.js sliding playlist)
$BASE/video/status?token=$ADMIN               # same counters as JSON
```

The status row shows `last_seq`, kept/total segment counts, bytes received,
`last_received_at`, and codec/fps/resolution decoded from `X-Segment-Meta` --
all updating while the camera runs.

---

## 5. Clean-Windows MSI test

On a fresh Windows VM, run the packaged MSI with:

```
msiexec /i nzeroc-agent.msi PAIRING_CODE=pc-XXXXXXXXXX CLOUD_URL=https://nzerox-staging-cloud.onrender.com /l*v install.log
```

Confirm:

- the install **fails and rolls back** if enrollment fails (the `EnrollAgent`
  custom action is now `Return="check"` and runs before the service starts);
- the `NZerocAgent` service reaches **RUNNING**;
- **Start Menu** and **Desktop** "NZeroC Agent" shortcuts open the local
  dashboard;
- the agent appears at `"$BASE/?token=$ADMIN"` after its first heartbeat;
- uninstall removes both shortcuts (and, with `REMOVE_USER_DATA=1`, the stored
  credential + camera vault).

---

## 6. Render Free behaviour

Render's Free web service plan gives 512 MB RAM and a shared CPU, with HTTPS and
a `*.onrender.com` hostname included. **The service spins down after ~15 minutes
of no inbound traffic** and cold-starts on the next request (typically tens of
seconds while the container image is rehydrated and the app reconnects to Atlas).
That is fine for interactive testing and for the agent's minute-scale heartbeat
cadence -- a cold start just delays one heartbeat, which the agent retries.

Render Free has **no always-on / no minimum-instance option**. If you need
genuinely warm, uninterrupted service (e.g. measuring heartbeat latency), move
the service to a paid instance type in the dashboard (**Settings → Instance
Type**); paid instances do not spin down. Switch it back to Free when done to
stop billing.

Free services also have a monthly build-minute and bandwidth allowance; a single
small staging service stays well inside it.

---

## 7. Teardown

1. Render dashboard → the service → **Settings → Delete Service** (this removes
   the service and its stored env-var secrets).
2. If you used a Blueprint, also delete the Blueprint entry.
3. Atlas UI → the cluster → **Terminate** (deletes the M0 cluster and its data).

---

## Provider migration notes

The image serves plain HTTP on `0.0.0.0:$PORT` and reads all config from env
vars, so moving off Render is: run the same container anywhere that terminates
TLS and injects `PORT` (Fly.io, Railway, a bare VM behind Caddy/nginx, Google
Cloud Run, …), and point `MONGODB_URI` at any MongoDB (Atlas, self-hosted,
DocumentDB with the Mongo API). `/healthz` is the load-balancer health check.
Nothing in `staging_cloud/` is Render-specific except `render.yaml` itself.
