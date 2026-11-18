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

## Deploy to Render

The repository includes a Render Blueprint in `render.yaml` for two services: a FastAPI API and a React static site. The API uses a mounted disk for the SQLite database so users, audit records, approvals, and policy history survive redeployments. A persistent disk requires a paid Render web-service plan; the Blueprint uses the Starter plan. Do not remove the disk or change `DATABASE_PATH` unless you have migrated/backed up the database.

1. Push this project to a GitHub repository and ensure `render.yaml` is at the repository root.
2. In Render, choose **New → Blueprint**, connect the repository and branch, and apply the Blueprint.
3. When prompted, enter `ADMIN_PASSWORD` as a new, unique password of at least 12 characters. Render generates `SECRET_KEY`; keep it secret and do not replace it with a committed value.
4. Wait for both services to finish building. The API health check is `/api/health`; API docs are available at `/docs` on the API service.
5. Open the static-site URL and sign in to the Admin console with username `admin` and the `ADMIN_PASSWORD` you provided.

The Blueprint preconfigures the API and frontend URLs using the service names `corvo-procurement-api` and `corvo-procurement-web`. If Render requires different names because those names are unavailable, update `VITE_API_BASE_URL` on the static site to `https://<your-api-service>.onrender.com/api` and `CORS_ORIGINS` on the API to `https://<your-static-site>.onrender.com`, then redeploy both services. `CORS_ORIGINS` must be the exact frontend origin (no trailing slash).

Production deployment changes in this project:
- The frontend reads `VITE_API_BASE_URL`; local Vite development proxies `/api` to port 8010.
- The backend reads `PORT`, `DATABASE_PATH`, `SECRET_KEY`, `CORS_ORIGINS`, and admin bootstrap settings from the environment. The Render start command binds to Render's assigned `$PORT`.
- Production mode will not create the demo users. It creates only the configured admin on an empty database. Existing local/demo databases can still authenticate legacy SHA-256 password hashes, which are upgraded to salted PBKDF2 hashes at successful login.
- The API allows only configured CORS origins instead of wildcard origins.

SQLite on one persistent disk is intended for a small, single-instance demonstration. For a production multi-instance service, migrate to managed PostgreSQL before scaling out; local procurement masters and contract PDFs are still bundled with the app and are not an editable production data-management workflow.

## Key files
- `backend/app/main.py` — FastAPI app, policy engine, auth, and admin workflows
- `backend/config/procurement_policy_v5.json` — versioned policy configuration
- `backend/procurement.db` — SQLite persistence for users, policy history, and audit logs
- `render.yaml` — Render Blueprint for the API, persistent disk, and static frontend
- `frontend/src/App.tsx` — dashboard, intake UI, and admin console
- `master_data/` — item, supplier, PO, and plant data
- `DECISIONS.md` — policy assumptions and silent decisions
