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

This is not yet a production deployment or a complete implementation of the assignment. In particular, persistence currently uses SQLite rather than PostgreSQL/SQLAlchemy/Alembic; policy changes do not yet have a draft/review/effective-date approval lifecycle; and full chat/PR creation, buyer, sourcing, and contract-management workflows are still outstanding. Demo credentials and the current authentication implementation are for local evaluation only and must be replaced before deployment.

## Key files
- `backend/app/main.py` — FastAPI app, policy engine, auth, and admin workflows
- `backend/config/procurement_policy_v5.json` — versioned policy configuration
- `backend/procurement.db` — SQLite persistence for users, policy history, and audit logs
- `frontend/src/App.tsx` — dashboard, intake UI, and admin console
- `master_data/` — item, supplier, PO, and plant data
- `DECISIONS.md` — policy assumptions and silent decisions
