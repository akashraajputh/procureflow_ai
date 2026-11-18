# Corvo Procurement Automation Platform

This workspace contains a working prototype of the Corvo Engineering procurement automation platform for the Full-Stack Intern Assignment.

## Included
- FastAPI backend with policy-driven PR evaluation engine
- React + Vite frontend dashboard and PR intake UI
- Policy version configuration for procurement rules
- Contract-aware supplier recommendations and policy-based routing
- Admin-only PR batch upload with validation, clubbing, PO consolidation, and decision CSV export
- Supplier-ranking CSV export endpoint
- SQLite-backed users, policy history, approval queue, and decision audit
- Decision logs and policy assumptions recorded in `DECISIONS.md`

## Run locally

### Backend
```bash
cd backend
py -3.13 -m uvicorn app.main:app --host 0.0.0.0 --port 8010
```

### Frontend
```bash
cd frontend
npm ci
npm run dev -- --host 0.0.0.0 --port 4173
```

Then open:
- Frontend: http://localhost:4173
- API: http://localhost:8010/docs

### Tests and checks
```bash
# Backend regression tests
py -3.13 -m pip install -r backend/requirements-dev.txt
py -3.13 -m pytest backend/tests -q

# Frontend production build and lint
cd frontend
npm run build
npm run lint
```

### PR batch processing
Sign in to the Admin console with the demo admin account, choose a CSV file, and select **Process CSV**. The batch endpoint requires an admin token:

```text
POST /api/process-batch
Content-Type: multipart/form-data
Form field: file (CSV)
```

The response includes row-level results and a `decision_csv` value. The UI displays the results and provides a download button. The 186-row sample can be used from `pr_batches/sample_pr_batch.csv`. Supplier rankings can also be exported from `GET /api/export/supplier-rankings?as_of=2026-11-01`.

## Admin access

The platform includes a local/demo enterprise layer backed by SQLite:
- Default admin credentials: `admin / admin123`
- Demo role accounts: `procurement_head / proc123`, `plant_manager / plant123`, `buyer / buyer123`
- Approval routing is determined by decision value and business role, and admin users can approve all pending items
- The app stores authenticated sessions in the browser and persists data locally in `backend/procurement.db`

This is not yet a complete production procurement system. In particular, persistence currently uses SQLite rather than PostgreSQL/SQLAlchemy/Alembic; policy changes do not yet have a draft/review/effective-date approval lifecycle; and full chat/PR creation, buyer, sourcing, and contract-management workflows are still outstanding. Demo credentials are local-only; production has a separately configured admin bootstrap, salted password hashes, and 24-hour JWTs, but still lacks broader identity-provider features such as MFA and password recovery.

## Deploy frontend to Netlify and backend to Render

Host the React/Vite frontend as a Netlify site and keep the FastAPI backend as a Render Web Service. Netlify builds the static site using the root `netlify.toml`, including the SPA rewrite; it does not host/run the Python API. The Render API can use the Free plan, but its SQLite database is ephemeral and data may be lost on restart, redeploy, or spin-down.

### A. Deploy the API on Render

1. Push the latest project to GitHub.
2. In Render, select **New → Web Service**, connect the repository, and select the branch.
3. Keep **Root Directory** blank (repository root). The API requires the `master_data/`, `contracts/`, and `pr_batches/` sibling folders; do not set the root directory to `backend`.
4. Configure:
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free
5. Add environment variables:
   - `APP_ENV` = `production`
   - `DATABASE_PATH` = `/tmp/procurement.db`
   - `SECRET_KEY` = a unique private random secret
   - `ADMIN_USERNAME` = `admin`
   - `ADMIN_EMAIL` = an email address you control
   - `ADMIN_PASSWORD` = a new unique password of at least 12 characters
   - `CORS_ORIGINS` = `https://procureflow-ai-zwox.netlify.app`
6. Confirm the price shows `$0/month`, create the service, and wait for deployment.
7. The current Render backend URL is `https://procureflow-ai-dl2o.onrender.com`. Check `https://procureflow-ai-dl2o.onrender.com/api/health` returns `{"status":"ok", ...}`.

If Render says it cannot find `requirements.txt`, confirm it is building the latest GitHub commit, Root Directory is blank, and the repository-root `requirements.txt` exists. The root file includes `backend/requirements.txt`.

### B. Deploy the frontend on Netlify

1. In Netlify, select **Add new site → Import an existing project**, connect the same GitHub repository, and choose the branch.
2. Netlify reads the root [netlify.toml](./netlify.toml), which sets:
   - Base directory: `frontend`
   - Build command: `npm ci && npm run build`
   - Publish directory: `dist`
   - SPA rewrite: `/*` → `/index.html`
3. The root `netlify.toml` configures `VITE_API_BASE_URL` as `https://procureflow-ai-dl2o.onrender.com/api`. If you change backend services, update that value in `netlify.toml` or override it in the Netlify site's **Site configuration → Environment variables**.
4. Trigger a deploy so the value is embedded in the built frontend. The current Netlify site URL is `https://procureflow-ai-zwox.netlify.app`.

### C. Allow Netlify to call the API

1. In the Render API service's **Environment** settings, set `CORS_ORIGINS` to `https://procureflow-ai-zwox.netlify.app` (no trailing slash and no `/api`).
2. Save and wait for Render to redeploy.
3. Open the Netlify URL, go to **Admin**, and sign in as `admin` with the `ADMIN_PASSWORD` set on Render.
4. If API requests fail, verify the Netlify `VITE_API_BASE_URL`, Render `CORS_ORIGINS`, and both actual public URLs. Changing `VITE_API_BASE_URL` requires a new Netlify build/deploy.

### Troubleshooting a blank/white Netlify page

The frontend must call the Render API, not Netlify's own `/api` path. If `VITE_API_BASE_URL` is missing from the Netlify build, `/api/...` can be rewritten to the SPA's `index.html`; the app then receives HTML instead of JSON and may fail while rendering. The root `netlify.toml` sets the API URL to `https://procureflow-ai-dl2o.onrender.com/api`; if using another backend, update that value and trigger **Clear cache and deploy site** (or a new deploy). Verify the Netlify deploy used the commit containing this configuration. The app now shows an API configuration/error notice rather than crashing on HTML responses, and a React render error fallback avoids a completely blank page.

The root `render.yaml` is now backend-only for an optional Render Blueprint; it no longer creates a Render-hosted frontend. For the split deployment above, create the Render Web Service and Netlify site separately.

Production deployment changes in this project:
- The frontend reads `VITE_API_BASE_URL`; local Vite development proxies `/api` to port 8010. Netlify settings and SPA routing are in `netlify.toml`.
- The backend reads `PORT`, `DATABASE_PATH`, `SECRET_KEY`, `CORS_ORIGINS`, and admin bootstrap settings from the environment. The Render start command binds to Render's assigned `$PORT`.
- `.python-version` pins the Render Python runtime to 3.13.14; commit it to GitHub so Render does not select a newer default interpreter.
- Production mode will not create the demo users. It creates only the configured admin on an empty database. Existing local/demo databases can still authenticate legacy SHA-256 password hashes, which are upgraded to salted PBKDF2 hashes at successful login.
- The API allows only configured CORS origins instead of wildcard origins.

Free-tier limitations:
- Treat all database content as disposable. Use demo data only; don't enter real procurement, supplier, employee, or confidential business information.
- Free web services can spin down after inactivity, leading to a delay on the first request while the API starts.
- No persistent disk is attached. For durable records and production workloads, use a paid persistent disk or migrate to managed PostgreSQL. SQLite on an ephemeral filesystem is not a production database.
- The frontend static site remains available while the API may be starting up from idle.
- Local procurement master data and contract PDFs are still bundled with the application and are not editable through production management workflows.

## Key files
- `backend/app/main.py` — FastAPI app, policy engine, auth, and admin workflows
- `backend/config/procurement_policy_v5.json` — versioned policy configuration
- `backend/procurement.db` — SQLite persistence for users, policy history, and audit logs
- `netlify.toml` — Netlify frontend build settings and SPA route rewrite
- `render.yaml` — optional Render Blueprint for the backend API only
- `frontend/src/App.tsx` — dashboard, intake UI, and admin console
- `master_data/` — item, supplier, PO, and plant data
- `DECISIONS.md` — policy assumptions and silent decisions
