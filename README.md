# NZeroC staging cloud

A small, **self-contained** stand-in for the NZeroC cloud control plane.

It speaks the *exact* HTTP contract the NZeroC Windows agent already uses, so a
real agent can enrol against it, send heartbeats, pull camera config + update
manifests, and **stream live ~2-second MPEG-TS video segments** to it -- all
against a URL you deploy yourself. It is meant for staging / integration
testing, not production video infrastructure.

This repository has **no dependency on the NZeroC source tree**. The handful of
agent&harr;cloud wire models the request handlers validate against are vendored
under [`staging_cloud/contracts/`](staging_cloud/contracts/) as byte-compatible
copies.

```
staging_cloud/            <- this repo root
├── Dockerfile            <- standalone: `docker build .`
├── render.yaml           <- Render Blueprint (Free plan)
├── docker-compose.yml    <- local run against a real mongo:7
├── requirements.txt      <- runtime deps (used by the image)
├── requirements-dev.txt  <- + test / lint / type-check
├── pyproject.toml        <- ruff / mypy / pytest config
├── .env.example
├── staging_cloud/        <- the Python package
│   ├── api.py  domain.py  repo.py  memory_repo.py  video_store.py  ...
│   └── contracts/        <- vendored agent<->cloud models (no nzerox import)
└── tests/                <- pytest suite (memory:// backend, no network)
```

## What it implements

| Route | Status |
| --- | --- |
| `POST /v1/agents/enroll` | real |
| `POST /v1/agents/{id}/rotate-credential` | real |
| `POST /v1/agents/{id}/heartbeat` | real |
| `GET /v1/agents/{id}/config` | real |
| `GET /v1/agents/{id}/update-manifest` | real |
| `POST /v1/ingest/video/{site}/{camera}` | real — **live video test receiver** (data-plane bearer) |
| `GET /video`, `/video/status`, `/video/{site}/{camera}[/index.m3u8, /seg/{seq}.ts]` | real (admin-token gated) |
| `POST /v1/whip/...`, `GET /v1/context/...`, `POST /v1/alerts` | registered, returns `501` |
| `GET /healthz` | real (platform health check) |
| `GET /`, `GET/POST /admin/*` | real (admin-token gated) |

Bearer credentials are stored **only as `sha256` hex** -- a database dump cannot
be replayed against the API.

---

## 1. Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `MONGODB_URI` | **yes** | – | `memory://` for a persistence-free run, or a real `mongodb://` / `mongodb+srv://` connection string |
| `STAGING_ADMIN_TOKEN` | **yes** | – | shared secret for `/`, `/admin/*` and `/video*` |
| `MONGODB_DB` | no | `nzerox_staging` | database name |
| `HOST` | no | `0.0.0.0` | bind address |
| `PORT` | no | `8080` | bind port (Render injects this) |
| `PAIRING_CODE_TTL_SECONDS` | no | `3600` | lifetime of a minted pairing code |
| `CREDENTIAL_TTL_DAYS` | no | `30` | lifetime of an issued / rotated credential |
| `HEARTBEAT_HISTORY_TTL_DAYS` | no | `7` | heartbeat-row retention (Mongo TTL index) |
| `VIDEO_MAX_SEGMENTS_PER_CAMERA` | no | `20` | in-memory video-segment ring depth per camera |
| `UPDATE_CHANNEL` | no | `default` | release channel key for the update manifest |
| `ENV_NAME` | no | `staging` | free-text label shown by `/healthz` |
| `LOG_LEVEL` | no | `INFO` | root logging level |

A missing required variable aborts startup with a clear message. Copy
[`.env.example`](.env.example) to `.env` for local runs (`.env` is git-ignored).

---

## 2. Run it locally with the in-memory repository (no Mongo, no Docker)

```bash
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate
pip install -r requirements.txt

MONGODB_URI=memory:// \
STAGING_ADMIN_TOKEN=dev-admin \
PORT=8080 \
python -m staging_cloud
```

`memory://` keeps everything in process memory: no database, nothing persisted,
gone on restart. Perfect for a quick end-to-end test.

### Verify `/healthz`

```bash
curl -s localhost:8080/healthz
# {"status":"ok","db":"ok","env":"staging"}
```

### Mint a pairing code + open the dashboard

```bash
curl -s -H "X-Admin-Token: dev-admin" -X POST localhost:8080/admin/pairing-codes \
  -d '{"site_id":"site-dev","tenant_id":"tenant-dev"}'
# {"codes":["pc-XXXXXXXXXX"], ...}

open "http://localhost:8080/?token=dev-admin"          # operations dashboard
```

`python -m staging_cloud.admin mint --site s --tenant t` does the same straight
against the backend (handy before the service is publicly reachable).

## 3. Run it locally against a real MongoDB (Docker)

```bash
docker compose up --build
# staging cloud on http://localhost:8080, backed by mongo:7
```

---

## 4. Configure MongoDB Atlas (free M0)

1. Create a free Atlas account &rarr; **Build a Database** &rarr; **M0** (free).
2. Pick a region close to your Render region.
3. **Database Access** &rarr; add a user with `readWrite` on `nzerox_staging`.
4. **Network Access** &rarr; allow `0.0.0.0/0` (staging only) so Render's egress
   IPs can connect.
5. **Connect &rarr; Drivers** &rarr; copy the `mongodb+srv://…` string and put
   the real password in it. The `+srv` form needs SRV DNS lookups, which
   `dnspython` (pinned in `requirements.txt`) provides.

No collections or indexes need creating by hand -- `ensure_indexes()` runs on
every startup and is idempotent.

---

## 5. Deploy to Render (Free plan)

Full walk-through in [`DEPLOY.md`](DEPLOY.md). Short version:

1. Push this repo to GitHub.
2. Render dashboard &rarr; **New &rarr; Blueprint**, select the repo. It reads
   [`render.yaml`](render.yaml) (`dockerfilePath: ./Dockerfile`,
   `dockerContext: .` -- both relative to this repo root) and creates one free
   Docker web service.
3. When prompted, set the two `sync: false` secrets:
   - `MONGODB_URI` = the Atlas string from step 4
   - `STAGING_ADMIN_TOKEN` = a long random value
     (`python -c "import secrets; print(secrets.token_urlsafe(32))"`)
4. Deploy. Render terminates HTTPS and serves the app at
   **`https://<service>.onrender.com`**.

```bash
curl -s https://<service>.onrender.com/healthz
# {"status":"ok","db":"ok","env":"staging"}   <- proves Atlas connectivity
```

> Render Free spins the service down after ~15 min idle and cold-starts on the
> next request (tens of seconds). Fine for testing; a paid instance type is
> needed for always-on. See `DEPLOY.md`.

---

## 6. How the NZeroC agent connects

No agent change. In the agent's `agent.yaml`:

```yaml
cloud_base_url: https://<service>.onrender.com   # (or http://127.0.0.1:8080 for a local run)
streaming:
  enabled: true
  site_id: <your-site-id>
  transport:
    mode: https_only
```

Then run the packaged MSI with `PAIRING_CODE=pc-… CLOUD_URL=https://<service>.onrender.com`,
or `nzeroc-agent admin enroll --code pc-… --cloud-url https://<service>.onrender.com`.
The agent enrols, heartbeats, pulls config, and (with `streaming.enabled`) POSTs
`~2 s` MPEG-TS segments to `POST /v1/ingest/video/{site_id}/{camera_id}` with its
`Authorization: Bearer` credential, `X-Segment-Seq` and `X-Segment-Meta` headers
-- exactly as it does today.

---

## 7. Watch live video

The `/video*` routes are gated by the same `STAGING_ADMIN_TOKEN`; pass it in the
query string so the browser's HLS fetches stay authenticated:

```
https://<service>.onrender.com/video?token=<STAGING_ADMIN_TOKEN>
```

- **`/video`** -- status page: per camera, last sequence, segment counts, bytes
  received, last-received timestamp, and codec/fps/resolution decoded from
  `X-Segment-Meta`. Auto-refreshes every 3 s.
- **`/video/{site}/{camera}?token=…`** -- a live player. hls.js plays a *sliding*
  HLS playlist built from the recent segments, so it keeps updating while the
  camera runs. Safari uses native HLS.
- **`/video/status?token=…`** -- the same counters as JSON.

Segments are held in a **bounded in-process ring** (`VIDEO_MAX_SEGMENTS_PER_CAMERA`
newest per camera, ~2 s each) -- **never in MongoDB, never on disk**. MongoDB is a
poor fit for video bytes and Render Free's filesystem is ephemeral and spins
down when idle, so everything here is lost on a restart / deploy / idle-shutdown.
That is intentional: this is a test receiver, not permanent storage.

### Test live video ingestion by hand (no agent)

```bash
BASE=https://<service>.onrender.com      # or http://localhost:8080
ADMIN=<STAGING_ADMIN_TOKEN>

# 1. mint a code + enrol to get a bearer credential
CODE=$(curl -s -H "X-Admin-Token: $ADMIN" -X POST $BASE/admin/pairing-codes \
  -d '{"site_id":"site-test","tenant_id":"tenant-test"}' | python -c "import sys,json;print(json.load(sys.stdin)['codes'][0])")
CRED=$(curl -s -X POST $BASE/v1/agents/enroll -H 'content-type: application/json' \
  -d "{\"pairing_code\":\"$CODE\",\"fingerprint\":{\"machine_id\":\"m\",\"primary_mac\":\"x\",\"os_name\":\"L\",\"os_version\":\"1\",\"cpu_count\":1,\"total_memory_gb\":1,\"declared_max_cameras\":1}}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['credential'])")

# 2. POST a couple of fake MPEG-TS segments
META=$(python -c "import base64,json;print(base64.b64encode(json.dumps({'camera_id':'cam-test','site_id':'site-test','seq':0,'agent_boot_id':'b','codec':'hevc','resolution':'1280x720','fps':5.0,'pts_base_ms':1000,'duration_ms':2000,'config_version':'0','gop_frames':10,'byte_length':188,'keyframe':True,'gap_flag':False},sort_keys=True).encode()).decode())")
for S in 0 1 2; do
  printf 'FAKE-TS-%d' $S | curl -s -X POST "$BASE/v1/ingest/video/site-test/cam-test" \
    -H "Authorization: Bearer $CRED" -H "Content-Type: video/mp2t" \
    -H "X-Segment-Seq: $S" -H "X-Segment-Meta: $META" --data-binary @- \
    -o /dev/null -w "seq $S -> %{http_code}\n"
done

# 3. see it arrive
curl -s "$BASE/video/status?token=$ADMIN"
open "$BASE/video/site-test/cam-test?token=$ADMIN"
```

(A real webcam &rarr; local RTSP &rarr; NZeroC agent produces genuine
keyframe-aligned segments that play in the browser; the fake bytes above just
prove the receive/store/serve path.)

---

## Tests, lint, type-check

```bash
pip install -r requirements-dev.txt
pytest          # 64 tests, memory:// backend, no network
ruff check .
mypy            # config in pyproject.toml
```

`tests/test_staging_mongo_repo.py` additionally exercises `MongoRepo` via
`mongomock-motor` when that dev dep is installed (skipped otherwise).
