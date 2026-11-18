from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BASE_DIR.parent
DATA_DIR = ROOT_DIR / 'master_data'
CONTRACTS_DIR = ROOT_DIR / 'contracts'
BATCH_DIR = ROOT_DIR / 'pr_batches'
POLICY_FILE = BASE_DIR / 'config' / 'procurement_policy_v5.json'
DB_PATH = BASE_DIR / 'procurement.db'
SECRET_KEY = 'corvo-local-dev-secret-change-in-production'

app = FastAPI(title='Corvo Procurement Automation API')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

ITEM_MASTER = pd.read_csv(DATA_DIR / 'item_master.csv')
SUPPLIER_MASTER = pd.read_csv(DATA_DIR / 'supplier_master.csv')
PO_HISTORY = pd.read_csv(DATA_DIR / 'po_history.csv')
CONTRACT_REGISTER = pd.read_csv(CONTRACTS_DIR / 'contract_register.csv')
CONTRACT_LINES = pd.read_csv(CONTRACTS_DIR / 'contract_lines.csv')
POLICY = json.loads(POLICY_FILE.read_text(encoding='utf-8'))
security = HTTPBearer(auto_error=False)


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS policy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                document TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                owner TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS decision_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pr_no TEXT,
                item_code TEXT,
                route TEXT,
                decision TEXT,
                clause TEXT,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT NOT NULL DEFAULT 'system'
            )
            '''
        )
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS approval_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pr_no TEXT,
                item_code TEXT,
                route TEXT,
                decision TEXT,
                clause TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                required_role TEXT NOT NULL DEFAULT 'buyer',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                requested_by TEXT NOT NULL DEFAULT 'workflow',
                approved_by TEXT,
                approved_at TEXT
            )
            '''
        )
        conn.commit()

        approval_columns = [row[1] for row in conn.execute('PRAGMA table_info(approval_queue)').fetchall()]
        if 'required_role' not in approval_columns:
            conn.execute('ALTER TABLE approval_queue ADD COLUMN required_role TEXT NOT NULL DEFAULT "buyer"')
            conn.execute('UPDATE approval_queue SET required_role = "buyer" WHERE required_role IS NULL')
        conn.commit()

    with get_db_connection() as conn:
        for username, email, password, role in [
            ('admin', 'admin@corvo.local', 'admin123', 'admin'),
            ('procurement_head', 'procurement.head@corvo.local', 'proc123', 'procurement_head'),
            ('plant_manager', 'plant.manager@corvo.local', 'plant123', 'plant_manager'),
            ('buyer', 'buyer@corvo.local', 'buyer123', 'buyer'),
        ]:
            existing = conn.execute('SELECT COUNT(*) FROM users WHERE username = ?', (username,)).fetchone()
            if existing[0] == 0:
                conn.execute(
                    'INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
                    (username, email, sha256(password.encode('utf-8')).hexdigest(), role),
                )

        policy_count = conn.execute('SELECT COUNT(*) FROM policy_history').fetchone()[0]
        if policy_count == 0:
            conn.execute(
                'INSERT INTO policy_history (version, document, effective_date, owner, payload) VALUES (?, ?, ?, ?, ?)',
                (
                    POLICY.get('version', '5.0'),
                    POLICY.get('document', 'CORVO-PROC-POL-04'),
                    POLICY.get('effective_date', '2026-04-01'),
                    POLICY.get('owner', 'Head of Procurement'),
                    json.dumps(POLICY),
                ),
            )
        conn.commit()


init_db()


def norm(value: Any) -> str:
    if value is None or pd.isna(value):
        return ''
    return str(value).strip().lower()


def to_date(value: Any) -> Optional[datetime]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%d-%m-%Y', '%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y %H:%M'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return pd.to_datetime(text).to_pydatetime()
    except Exception:
        return None


def create_token(user_id: int, username: str, role: str) -> str:
    payload = {'sub': username, 'role': role, 'user_id': user_id}
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail='Authentication required.')
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=['HS256'])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail='Invalid or expired token.') from exc
    return {'id': payload.get('user_id'), 'username': payload.get('sub'), 'role': payload.get('role', 'user')}


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='Admin access required.')
    return current_user


def determine_approval_role(requirement: Dict[str, Any], route: str, decision: str) -> str:
    requirement_value = float(requirement.get('estimated_unit_price', 0) or 0) * float(requirement.get('quantity', 0) or 0)
    if not requirement_value and route in {'EMERGENCY_PO', 'CONTRACT_PO', 'SPOT_PO'}:
        requirement_value = float(requirement.get('requirement_value', 0) or 0)
    if route in {'RFP', 'EMERGENCY_PO'} or requirement_value >= 500000:
        return 'procurement_head'
    if requirement_value >= 250000 or str(requirement.get('plant', '')).upper() in {'CHK1', 'PUN1'}:
        return 'plant_manager'
    return 'buyer'


def record_decision_audit(pr_no: Optional[str], item_code: Optional[str], route: str, decision: str, clause: str, detail: str, created_by: str = 'system', required_role: str = 'buyer') -> None:
    with get_db_connection() as conn:
        conn.execute(
            'INSERT INTO decision_audit (pr_no, item_code, route, decision, clause, detail, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (pr_no, item_code, route, decision, clause, detail, created_by),
        )
        conn.execute(
            'INSERT INTO approval_queue (pr_no, item_code, route, decision, clause, reason, status, required_role, requested_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (pr_no, item_code, route, decision, clause, detail, 'pending', required_role, created_by),
        )
        conn.commit()


class LoginInput(BaseModel):
    username: str
    password: str


class UserCreateInput(BaseModel):
    username: str
    email: str
    password: str
    role: str = 'user'


class PolicyVersionInput(BaseModel):
    version: str
    document: str = 'CORVO-PROC-POL-04'
    effective_date: str
    owner: str = 'Head of Procurement'
    payload: Dict[str, Any]


class ApprovalDecisionInput(BaseModel):
    status: str = 'approved'
    note: str = ''


def item_match(item_code: Optional[str], description: Optional[str]) -> Optional[dict]:
    code = norm(item_code)
    if code:
        row = ITEM_MASTER.loc[ITEM_MASTER['item_code'].str.lower() == code]
        if not row.empty:
            return row.iloc[0].to_dict()
    text = norm(description)
    if not text:
        return None
    best_score = 0.0
    best_row = None
    for _, row in ITEM_MASTER.iterrows():
        candidate = norm(row.get('description', ''))
        if not candidate:
            continue
        score = 0.0
        if text == candidate:
            score = 1.0
        else:
            overlap = len(set(text.split()) & set(candidate.split()))
            if overlap >= 2:
                score = max(score, 0.82)
            if text in candidate or candidate in text:
                score = max(score, 0.9)
        if text == norm(row.get('item_code')):
            score = 1.0
        if score > best_score:
            best_score = score
            best_row = row.to_dict()
    if best_score >= 0.8:
        return best_row
    return None


def get_supplier_by_id(supplier_id: Optional[str]) -> Optional[dict]:
    if not supplier_id:
        return None
    row = SUPPLIER_MASTER.loc[SUPPLIER_MASTER['supplier_id'].str.lower() == str(supplier_id).strip().lower()]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


@lru_cache(maxsize=2048)
def _get_active_supplier_history(item_code: str, as_of_iso: str) -> tuple:
    as_of = datetime.fromisoformat(as_of_iso)
    if not item_code:
        return tuple()
    result = []
    target = norm(item_code)
    for _, row in PO_HISTORY.iterrows():
        if norm(row.get('item_code')) != target:
            continue
        po_date = to_date(row.get('po_date'))
        if po_date is None or po_date > as_of:
            continue
        supplier = get_supplier_by_id(row.get('supplier_id'))
        if supplier and norm(supplier.get('status')) == 'active':
            result.append({
                'supplier_id': row.get('supplier_id'),
                'po_date': po_date,
                'unit_price': float(row.get('unit_price', 0) or 0),
                'quantity': float(row.get('quantity', 0) or 0),
                'plant': row.get('plant'),
            })
    return tuple(result)


def get_active_supplier_history(item_code: str, as_of: datetime | str) -> List[dict]:
    as_of_iso = as_of.isoformat() if not isinstance(as_of, str) else as_of
    return list(_get_active_supplier_history(item_code, as_of_iso))


def get_contracts_for_item(item_code: str, as_of: datetime) -> List[dict]:
    contracts = []
    target = norm(item_code)
    for _, row in CONTRACT_LINES.iterrows():
        if norm(row.get('item_code')) != target:
            continue
        contract_id = str(row.get('contract_id'))
        register_row = CONTRACT_REGISTER.loc[CONTRACT_REGISTER['contract_id'] == contract_id]
        if register_row.empty:
            continue
        contract = register_row.iloc[0].to_dict()
        supplier = get_supplier_by_id(contract.get('supplier_id'))
        if supplier is None:
            continue
        if norm(supplier.get('status')) != 'active':
            continue
        start = to_date(contract.get('start_date'))
        end = to_date(contract.get('end_date'))
        if start is None or end is None:
            continue
        if start <= as_of <= end:
            contracts.append({
                'contract_id': contract_id,
                'supplier_id': contract.get('supplier_id'),
                'supplier_name': contract.get('supplier_name'),
                'rate': float(row.get('rate', 0) or 0),
                'consumed_value': float(contract.get('consumed_value', 0) or 0),
                'value_cap': float(contract.get('value_cap', 0) or 0),
            })
    return contracts


@lru_cache(maxsize=2048)
def _get_preferred_supplier_rank(item_code: str, as_of_iso: str) -> tuple:
    as_of = datetime.fromisoformat(as_of_iso)
    target = norm(item_code)
    candidates = []
    for _, supplier in SUPPLIER_MASTER.iterrows():
        if norm(supplier.get('status')) != 'active':
            continue
        supplier_id = str(supplier.get('supplier_id'))
        po_rows = PO_HISTORY.loc[PO_HISTORY['item_code'].str.lower() == target].copy()
        po_rows = po_rows[po_rows['supplier_id'].astype(str).str.lower() == supplier_id.lower()]
        po_rows = po_rows[pd.to_datetime(po_rows['po_date']) <= pd.Timestamp(as_of)]
        po_rows = po_rows.sort_values('po_date', ascending=False)
        latest_po = po_rows.iloc[0].to_dict() if not po_rows.empty else None
        latest_contract = None
        for _, contract in CONTRACT_REGISTER.iterrows():
            if str(contract.get('supplier_id')).lower() != supplier_id.lower():
                continue
            if to_date(contract.get('start_date')) and to_date(contract.get('start_date')) <= as_of:
                relevant = CONTRACT_LINES.loc[CONTRACT_LINES['contract_id'] == contract['contract_id']]
                relevant = relevant[relevant['item_code'].str.lower() == target]
                if not relevant.empty:
                    latest_contract = {'contract_id': contract['contract_id'], 'rate': float(relevant.iloc[0]['rate'])}
                    break
        if latest_po is None and latest_contract is None:
            continue
        days = (as_of - to_date(latest_po['po_date'])).days if latest_po else 3650
        if days <= 90:
            recency = 1.0
        elif days <= 365:
            recency = 0.5
        else:
            recency = 0.0
        score = (len(po_rows) * 36) + (recency * 28) + (1 if latest_contract else 0) * 18 + (float(supplier.get('on_time_pct', 0) or 0) * 0.22) + (float(supplier.get('quality_rating', 0) or 0) * 8)
        candidates.append({
            'supplier_id': supplier_id,
            'supplier_name': supplier.get('name'),
            'po_count': len(po_rows),
            'recency': recency,
            'contract': bool(latest_contract),
            'on_time_pct': float(supplier.get('on_time_pct', 0) or 0),
            'quality_rating': float(supplier.get('quality_rating', 0) or 0),
            'reference_price': float(latest_po.get('unit_price', 0) or 0) if latest_po is not None else float(latest_contract.get('rate', 0) or 0),
            'score': score,
        })
    ranked = sorted(candidates, key=lambda item: (-item['score'], -item['on_time_pct'], -item['quality_rating']))
    for idx, sup in enumerate(ranked, start=1):
        sup['rank'] = idx
    return tuple((item['supplier_id'], item['supplier_name'], item['po_count'], item['recency'], item['contract'], item['on_time_pct'], item['quality_rating'], item['reference_price'], item['score'], item['rank']) for item in ranked)


def get_preferred_supplier_rank(item_code: str, as_of: datetime | str) -> List[dict]:
    as_of_iso = as_of.isoformat() if not isinstance(as_of, str) else as_of
    ranked = _get_preferred_supplier_rank(item_code, as_of_iso)
    return [{
        'supplier_id': item[0],
        'supplier_name': item[1],
        'po_count': item[2],
        'recency': item[3],
        'contract': item[4],
        'on_time_pct': item[5],
        'quality_rating': item[6],
        'reference_price': item[7],
        'score': item[8],
        'rank': item[9],
    } for item in ranked]


def get_supplier_suggestions(item_code: str, as_of: datetime, required_count: int) -> List[dict]:
    preferred = get_preferred_supplier_rank(item_code, as_of)
    suggested = preferred[:required_count]
    if len(suggested) >= required_count:
        return suggested
    item_row = ITEM_MASTER.loc[ITEM_MASTER['item_code'].str.lower() == norm(item_code)]
    category = item_row.iloc[0].get('category') if not item_row.empty else None
    if not category:
        return suggested
    other = []
    for _, supplier in SUPPLIER_MASTER.iterrows():
        if norm(supplier.get('status')) != 'active':
            continue
        supplier_id = str(supplier.get('supplier_id'))
        if any(item['supplier_id'] == supplier_id for item in suggested):
            continue
        cats = [cat.strip() for cat in str(supplier.get('categories', '')).split(',')]
        if norm(category) in {norm(cat) for cat in cats}:
            other.append({
                'supplier_id': supplier_id,
                'supplier_name': supplier.get('name'),
                'on_time_pct': float(supplier.get('on_time_pct', 0) or 0),
                'quality_rating': float(supplier.get('quality_rating', 0) or 0),
            })
    for item in sorted(other, key=lambda x: (-x['on_time_pct'], -x['quality_rating'])):
        if len(suggested) >= required_count:
            break
        suggested.append({
            'supplier_id': item['supplier_id'],
            'supplier_name': item['supplier_name'],
            'score': None,
            'reference_price': None,
            'rank': None,
            'source': 'category_topping',
        })
    return suggested


def classify_requirement_route(requirement: dict) -> str:
    req_date = to_date(requirement.get('created_at')) or datetime.utcnow()
    item = item_match(requirement.get('item_code'), requirement.get('description'))
    quantity = float(requirement.get('quantity', 0) or 0)
    estimated = float(requirement.get('estimated_unit_price', 0) or 0)
    value = quantity * (estimated if estimated > 0 else (float(item.get('standard_price', 0) or 0) if item else 0))
    if item is None:
        return 'NEW_ITEM_SOURCING'
    if str(item.get('item_type')).lower() == 'capex':
        return 'CAPEX_SOURCING'
    for contract in get_contracts_for_item(str(item.get('item_code')), req_date):
        if contract['consumed_value'] + value <= contract['value_cap']:
            return 'CONTRACT_PO'
    history = get_active_supplier_history(str(item.get('item_code')), req_date)
    recent_history = [h for h in history if (req_date - h['po_date']).days <= 365]
    emergency = str(requirement.get('urgency', '')).lower() == 'emergency'
    if emergency and value >= float(POLICY['limits']['emergency_po_threshold']) and recent_history:
        return 'EMERGENCY_PO'
    if value < float(POLICY['limits']['spot_po_threshold']) and recent_history:
        return 'SPOT_PO'
    if value < float(POLICY['limits']['rfq_threshold']):
        return 'RFQ'
    return 'RFP'


def evaluate_requirement(requirement: dict) -> Dict[str, Any]:
    req_date = to_date(requirement.get('created_at')) or datetime.utcnow()
    item = item_match(requirement.get('item_code'), requirement.get('description'))
    quantity = float(requirement.get('quantity', 0) or 0)
    estimated = float(requirement.get('estimated_unit_price', 0) or 0)
    if item is not None:
        quantity = float(requirement.get('quantity', 0) or 0)
        if estimated <= 0:
            estimated = float(item.get('standard_price', 0) or 0)
        value = quantity * estimated
    else:
        value = quantity * estimated
    if item is None:
        return {
            'decision': 'SOURCING_REQUEST',
            'route': 'NEW_ITEM_SOURCING',
            'supplier': None,
            'clause': '1.2',
            'contract': None,
            'requirement_value': value,
            'explanation': 'No item master match was found, so the requirement is treated as a new item and sent for sourcing.',
            'preferred_supplier': [],
            'suggestions': [],
        }

    item_code = str(item['item_code'])
    if str(item.get('item_type')).lower() == 'capex':
        return {
            'decision': 'SOURCING_REQUEST',
            'route': 'CAPEX_SOURCING',
            'supplier': None,
            'clause': '5.1',
            'contract': None,
            'requirement_value': value,
            'explanation': 'The item is marked Capex, which always routes to sourcing prior to contract or value checks.',
            'preferred_supplier': get_preferred_supplier_rank(item_code, req_date)[:5],
            'suggestions': get_supplier_suggestions(item_code, req_date, 5),
        }

    applicable_contract = None
    for contract in sorted(get_contracts_for_item(item_code, req_date), key=lambda c: (c['rate'], c['contract_id'])):
        projected = contract['consumed_value'] + value
        if projected <= contract['value_cap']:
            applicable_contract = contract
            break

    if applicable_contract is not None:
        return {
            'decision': 'PO',
            'route': 'CONTRACT_PO',
            'supplier': applicable_contract['supplier_id'],
            'clause': '5.2',
            'contract': applicable_contract,
            'requirement_value': value,
            'explanation': f"A valid contract ({applicable_contract['contract_id']}) exists and retains enough ceiling for this requirement.",
            'preferred_supplier': get_preferred_supplier_rank(item_code, req_date)[:5],
            'suggestions': [],
        }

    history = get_active_supplier_history(item_code, req_date)
    recent_history = [h for h in history if (req_date - h['po_date']).days <= 365]
    emergency = str(requirement.get('urgency', '')).lower() == 'emergency'
    if emergency and value >= float(POLICY['limits']['emergency_po_threshold']) and recent_history:
        preferred = get_preferred_supplier_rank(item_code, req_date)
        return {
            'decision': 'PO',
            'route': 'EMERGENCY_PO',
            'supplier': preferred[0]['supplier_id'] if preferred else None,
            'clause': '5.3',
            'contract': None,
            'requirement_value': value,
            'explanation': 'The requirement is emergency and matches recent active purchasing history, so the first preferred supplier is used without a sourcing event.',
            'preferred_supplier': preferred[:5],
            'suggestions': [],
        }

    if value < float(POLICY['limits']['spot_po_threshold']) and recent_history:
        preferred = get_preferred_supplier_rank(item_code, req_date)
        return {
            'decision': 'PO',
            'route': 'SPOT_PO',
            'supplier': preferred[0]['supplier_id'] if preferred else None,
            'clause': '5.4',
            'contract': None,
            'requirement_value': value,
            'explanation': 'The value is below the material spot-PO threshold and there is recent active supplier history for the same item.',
            'preferred_supplier': preferred[:5],
            'suggestions': [],
        }

    if value < float(POLICY['limits']['rfq_threshold']):
        suggestions = get_supplier_suggestions(item_code, req_date, 3)
        return {
            'decision': 'SOURCING_REQUEST',
            'route': 'RFQ',
            'supplier': None,
            'clause': '5.5',
            'contract': None,
            'requirement_value': value,
            'explanation': 'The requirement falls below INR 10,00,000 and requires an RFQ sourcing event.',
            'preferred_supplier': get_preferred_supplier_rank(item_code, req_date)[:5],
            'suggestions': suggestions,
        }

    suggestions = get_supplier_suggestions(item_code, req_date, 5)
    return {
        'decision': 'SOURCING_REQUEST',
        'route': 'RFP',
        'supplier': None,
        'clause': '5.6',
        'contract': None,
        'requirement_value': value,
        'explanation': 'The requirement reaches the RFP threshold, so a formal request-for-proposal is required.',
        'preferred_supplier': get_preferred_supplier_rank(item_code, req_date)[:5],
        'suggestions': suggestions,
    }


class IntentionInput(BaseModel):
    request_text: str


class PurchaseRequirement(BaseModel):
    pr_no: str = Field(default='PR-LOCAL-001')
    pr_line: int = 1
    created_at: str = Field(default='2026-11-16 09:00')
    requester: str = 'Employee'
    department: str = 'Operations'
    plant: str = 'PUN1'
    item_code: Optional[str] = None
    description: str = ''
    quantity: float = 1
    uom: str = 'NOS'
    estimated_unit_price: float = 0
    need_by_date: str = '2026-11-30'
    urgency: str = 'Normal'


@app.get('/api/health')
def health() -> Dict[str, str]:
    return {'status': 'ok', 'service': 'corvo-procurement-api'}


@app.get('/api/policy')
def policy() -> Dict[str, Any]:
    with get_db_connection() as conn:
        row = conn.execute(
            'SELECT payload FROM policy_history ORDER BY id DESC LIMIT 1'
        ).fetchone()
    if row is not None:
        return json.loads(row['payload'])
    return POLICY


@app.post('/api/auth/login')
def login(payload: LoginInput) -> Dict[str, Any]:
    username = payload.username.strip()
    password_hash = sha256(payload.password.encode('utf-8')).hexdigest()
    with get_db_connection() as conn:
        user_row = conn.execute(
            'SELECT id, username, email, role FROM users WHERE (username = ? OR email = ?) AND password_hash = ?',
            (username, username, password_hash),
        ).fetchone()
    if user_row is None:
        raise HTTPException(status_code=401, detail='Invalid username or password.')
    token = create_token(user_row['id'], user_row['username'], user_row['role'])
    return {
        'token': token,
        'user': {
            'id': user_row['id'],
            'username': user_row['username'],
            'email': user_row['email'],
            'role': user_row['role'],
        },
    }


@app.get('/api/admin/dashboard')
def admin_dashboard(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    with get_db_connection() as conn:
        if current_user.get('role') == 'admin':
            users_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            policies_count = conn.execute('SELECT COUNT(*) FROM policy_history').fetchone()[0]
            audit_count = conn.execute('SELECT COUNT(*) FROM decision_audit').fetchone()[0]
        else:
            users_count = 0
            policies_count = 0
            audit_count = conn.execute(
                'SELECT COUNT(*) FROM approval_queue WHERE status = ? AND required_role = ?',
                ('pending', current_user.get('role')),
            ).fetchone()[0]
    return {
        'user': current_user,
        'totals': {
            'users': users_count,
            'policies': policies_count,
            'audit_rows': audit_count,
        },
        'active_policy': POLICY.get('version'),
    }


@app.get('/api/admin/users')
def list_users(current_user: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    del current_user
    with get_db_connection() as conn:
        rows = conn.execute(
            'SELECT id, username, email, role, created_at FROM users ORDER BY id ASC'
        ).fetchall()
    return [dict(row) for row in rows]


@app.post('/api/admin/users')
def create_user(payload: UserCreateInput, current_user: dict = Depends(require_admin)) -> Dict[str, Any]:
    del current_user
    username = payload.username.strip()
    email = payload.email.strip()
    if not username or not email or not payload.password:
        raise HTTPException(status_code=400, detail='Username, email, and password are required.')
    password_hash = sha256(payload.password.encode('utf-8')).hexdigest()
    with get_db_connection() as conn:
        try:
            cursor = conn.execute(
                'INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
                (username, email, password_hash, payload.role.strip() or 'user'),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail='User already exists for that username or email.') from exc
        user_id = cursor.lastrowid
    return {'id': user_id, 'username': username, 'email': email, 'role': payload.role.strip() or 'user'}


@app.get('/api/admin/policies')
def list_policies(current_user: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    del current_user
    with get_db_connection() as conn:
        rows = conn.execute(
            'SELECT id, version, document, effective_date, owner, payload, created_at FROM policy_history ORDER BY id DESC'
        ).fetchall()
    return [
        {
            **dict(row),
            'payload': json.loads(row['payload']),
        }
        for row in rows
    ]


@app.post('/api/admin/policies')
def create_policy(payload: PolicyVersionInput, current_user: dict = Depends(require_admin)) -> Dict[str, Any]:
    del current_user
    with get_db_connection() as conn:
        cursor = conn.execute(
            'INSERT INTO policy_history (version, document, effective_date, owner, payload) VALUES (?, ?, ?, ?, ?)',
            (payload.version, payload.document, payload.effective_date, payload.owner, json.dumps(payload.payload)),
        )
        conn.commit()
    global POLICY
    POLICY = payload.payload
    return {'id': cursor.lastrowid, 'version': payload.version, 'document': payload.document, 'effective_date': payload.effective_date}


@app.get('/api/admin/audit')
def list_audit(current_user: dict = Depends(require_admin)) -> List[Dict[str, Any]]:
    del current_user
    with get_db_connection() as conn:
        rows = conn.execute(
            'SELECT id, pr_no, item_code, route, decision, clause, detail, created_at, created_by FROM decision_audit ORDER BY id DESC LIMIT 50'
        ).fetchall()
    return [dict(row) for row in rows]


@app.get('/api/admin/approvals')
def list_approvals(current_user: dict = Depends(get_current_user)) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        if current_user.get('role') == 'admin':
            rows = conn.execute(
                'SELECT id, pr_no, item_code, route, decision, clause, reason, status, required_role, created_at, requested_by, approved_by, approved_at FROM approval_queue ORDER BY id DESC LIMIT 50'
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, pr_no, item_code, route, decision, clause, reason, status, required_role, created_at, requested_by, approved_by, approved_at FROM approval_queue WHERE required_role = ? ORDER BY id DESC LIMIT 50',
                (current_user.get('role'),),
            ).fetchall()
    return [dict(row) for row in rows]


@app.post('/api/admin/approvals/{approval_id}/decision')
def update_approval(approval_id: int, payload: ApprovalDecisionInput, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    status = payload.status.strip().lower()
    if status not in {'approved', 'rejected'}:
        raise HTTPException(status_code=400, detail='Status must be approved or rejected.')
    with get_db_connection() as conn:
        row = conn.execute(
            'SELECT id, status, required_role FROM approval_queue WHERE id = ?', (approval_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Approval request not found.')
        if current_user['role'] != 'admin' and current_user['role'] != row['required_role']:
            raise HTTPException(status_code=403, detail=f'Role {row["required_role"]} approval is required for this request.')
        if row['status'] != 'pending':
            raise HTTPException(status_code=400, detail='Approval request has already been processed.')
        conn.execute(
            'UPDATE approval_queue SET status = ?, approved_by = ?, approved_at = CURRENT_TIMESTAMP WHERE id = ?',
            (status, current_user['username'], approval_id),
        )
        conn.commit()
    return {'id': approval_id, 'status': status, 'updated_by': current_user['username'], 'note': payload.note}


@app.get('/api/summary')
def summary() -> Dict[str, Any]:
    batch = pd.read_csv(BATCH_DIR / 'sample_pr_batch.csv')
    totals = {'PO': 0, 'SOURCING_REQUEST': 0}
    per_route = defaultdict(int)
    for _, row in batch.iterrows():
        route = classify_requirement_route(row.to_dict())
        decision = 'PO' if route in {'CONTRACT_PO', 'EMERGENCY_PO', 'SPOT_PO'} else 'SOURCING_REQUEST'
        totals[decision] += 1
        per_route[route] += 1
    return {'total_lines': len(batch), 'decisions': totals, 'routes': dict(sorted(per_route.items()))}


@app.post('/api/analyze-intent')
def analyze_intent(payload: IntentionInput) -> Dict[str, Any]:
    text = payload.request_text or ''
    qty_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:nos|pc|pcs|kg|ltr|mtr|pair|bkt|job)?', text.lower())
    plant = 'PUN1'
    if 'chakan' in text.lower():
        plant = 'CHK1'
    description = text.strip()
    if qty_match:
        quantity = float(qty_match.group(1))
        description = re.sub(r'\d+(?:\.\d+)?\s*(?:nos|pc|pcs|kg|ltr|mtr|pair|bkt|job)?', '', text, flags=re.IGNORECASE).strip()
    else:
        quantity = 1.0
    matched = item_match(None, description)
    return {
        'request_text': text,
        'plant': plant,
        'quantity': quantity,
        'description': description,
        'item_match': matched,
        'needs_clarification': matched is None,
        'suggested_follow_up': 'Please confirm the item, quantity, plant, and exact need-by date before creating a PR.' if matched is None else 'Please confirm the final values before saving the PR.',
    }


@app.post('/api/evaluate-requirement')
def evaluate_requirement_endpoint(payload: PurchaseRequirement) -> Dict[str, Any]:
    result = evaluate_requirement(payload.model_dump())
    item = item_match(payload.item_code, payload.description)
    result['matched_item'] = item
    required_role = determine_approval_role(payload.model_dump(), result['route'], result['decision'])
    result['required_role'] = required_role
    record_decision_audit(
        pr_no=payload.pr_no,
        item_code=payload.item_code or (item.get('item_code') if item else None),
        route=result['route'],
        decision=result['decision'],
        clause=result['clause'],
        detail=result['explanation'],
        created_by='workflow',
        required_role=required_role,
    )
    return result


@app.get('/api/preferred-suppliers')
def preferred_suppliers(item_code: str, as_of: str = '2026-11-01') -> List[dict]:
    as_of_date = to_date(as_of) or datetime(2026, 11, 1)
    return get_preferred_supplier_rank(item_code, as_of_date)


@app.post('/api/upload-batch')
async def upload_batch(file: UploadFile = File(...)) -> Dict[str, Any]:
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail='Only CSV files are accepted.')
    content = await file.read()
    csv_path = ROOT_DIR / 'tmp_batch_upload.csv'
    csv_path.write_bytes(content)
    df = pd.read_csv(csv_path)
    required = {'pr_no', 'pr_line', 'created_at', 'requester', 'department', 'plant', 'quantity', 'uom', 'need_by_date', 'urgency'}
    missing = sorted(required - set(df.columns))
    if missing:
        raise HTTPException(status_code=400, detail=f'Missing required columns: {missing}')
    rows = []
    for idx, row in df.iterrows():
        errors = []
        qty = float(row.get('quantity', 0) or 0)
        if qty <= 0:
            errors.append('quantity must be positive')
        if not to_date(row.get('need_by_date')):
            errors.append('need_by_date is invalid')
        if not str(row.get('plant', '')).strip():
            errors.append('plant is required')
        item = item_match(row.get('item_code'), row.get('description'))
        if item is None and str(row.get('description') or '').strip():
            errors.append('description does not match the item master; new-item sourcing will be required')
        rows.append({'row': idx + 2, 'valid': not errors, 'errors': errors, 'item_code': item.get('item_code') if item else None})
    return {'rows_processed': len(rows), 'valid_rows': sum(1 for row in rows if row['valid']), 'results': rows}


@app.get('/api/sample-decisions')
def sample_decisions() -> List[Dict[str, Any]]:
    df = pd.read_csv(BATCH_DIR / 'sample_pr_batch.csv')
    results = []
    for _, row in df.head(15).iterrows():
        route = classify_requirement_route(row.to_dict())
        results.append({
            'pr_no': row.get('pr_no'),
            'item_code': row.get('item_code'),
            'plant': row.get('plant'),
            'route': route,
            'decision': 'PO' if route in {'CONTRACT_PO', 'EMERGENCY_PO', 'SPOT_PO'} else 'SOURCING_REQUEST',
            'value': float(row.get('quantity', 0) or 0) * float((row.get('estimated_unit_price') or 0) or 0),
            'clause': 'policy evaluation',
        })
    return results


@app.get('/')
def root() -> Dict[str, str]:
    return {'message': 'Corvo Procurement Automation Platform'}
