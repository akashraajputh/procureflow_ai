# Corvo Procurement Automation Platform

This workspace contains a working prototype of the Corvo Engineering procurement automation platform for the Full-Stack Intern Assignment.

## Included
- FastAPI backend with policy-driven PR evaluation engine
- React + Vite frontend dashboard and PR intake UI
- Policy version configuration for procurement rules
- CSV validation and supplier recommendation logic
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
npm install
npm run dev -- --host 0.0.0.0 --port 4173
```

Then open:
- Frontend: http://localhost:4173
- API: http://localhost:8010/docs

## Admin access

The platform includes a lightweight enterprise layer backed by SQLite:
- Default admin credentials: `admin / admin123`
- Demo role accounts: `procurement_head / proc123`, `plant_manager / plant123`, `buyer / buyer123`
- Approval routing is determined by decision value and business role, and admin users can approve all pending items
- The app stores authenticated sessions in the browser and persists data locally in `backend/procurement.db`

## Key files
- `backend/app/main.py` — FastAPI app, policy engine, auth, and admin workflows
- `backend/config/procurement_policy_v5.json` — versioned policy configuration
- `backend/procurement.db` — SQLite persistence for users, policy history, and audit logs
- `frontend/src/App.tsx` — dashboard, intake UI, and admin console
- `master_data/` — item, supplier, PO, and plant data
- `DECISIONS.md` — policy assumptions and silent decisions
