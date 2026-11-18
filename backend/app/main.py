from __future__ import annotations

import json
import csv
import io
import logging
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
import pandas as pd
from pypdf import PdfReader
from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
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
logger = logging.getLogger(__name__)


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


def load_pdf_only_contracts() -> tuple[pd.DataFrame, pd.DataFrame]:
    register_rows = []
    line_rows = []
    known_contracts = set(CONTRACT_REGISTER['contract_id'].astype(str))
    for pdf_path in sorted((CONTRACTS_DIR / 'pdf').glob('*.pdf')):
        try:
            text = '\n'.join(page.extract_text() or '' for page in PdfReader(pdf_path).pages)
        except Exception as exc:
            logger.warning('Could not extract contract PDF %s: %s', pdf_path.name, exc)
            continue

        contract_match = re.search(r'Contract No\.\s*(RC-[A-Z0-9-]+)', text, re.IGNORECASE)
        if not contract_match:
            logger.warning('Skipping contract PDF without a contract number: %s', pdf_path.name)
            continue
        contract_id = contract_match.group(1).upper()
        if contract_id in known_contracts:
            continue

        supplier_match = re.search(r'Supplier ID\s+(S\d+)', text, re.IGNORECASE)
        term_match = re.search(
            r'valid from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})',
            text,
            re.IGNORECASE,
        )
        ceiling_match = re.search(r'not exceed INR\s+([\d,]+(?:\.\d+)?)', text, re.IGNORECASE)
        if not (supplier_match and term_match and ceiling_match):
            logger.warning('Skipping contract PDF with incomplete terms: %s', pdf_path.name)
            continue

        supplier_id = supplier_match.group(1).upper()
        supplier = get_supplier_by_id(supplier_id)
        if supplier is None:
            logger.warning('Skipping %s because supplier %s is absent from supplier master', contract_id, supplier_id)
            continue
        ceiling = float(ceiling_match.group(1).replace(',', ''))
        register_rows.append({
            'contract_id': contract_id,
            'supplier_id': supplier_id,
            'supplier_name': supplier.get('name'),
            'start_date': term_match.group(1),
            'end_date': term_match.group(2),
            'value_cap': ceiling,
            'consumed_value': 0.0,
            'source': pdf_path.name,
        })

        schedule = re.compile(
            r'^\s*\d+\s+(ITM-\d+)\s+(.+?)\s+(NOS|KG|PAIR|MTR|LTR|BRL|BKT|CYL|MON|JOB|QTR)\s+([\d,]+(?:\.\d+)?)\s*$',
            re.IGNORECASE | re.MULTILINE,
        )
        for line_match in schedule.finditer(text):
            line_rows.append({
                'contract_id': contract_id,
                'item_code': line_match.group(1).upper(),
                'description': line_match.group(2).strip(),
                'uom': line_match.group(3).upper(),
                'rate': float(line_match.group(4).replace(',', '')),
                'source': pdf_path.name,
            })
        if not any(row['contract_id'] == contract_id for row in line_rows):
            logger.warning('No readable schedule lines found in contract PDF %s', pdf_path.name)
            register_rows.pop()
            continue
        known_contracts.add(contract_id)
    if register_rows:
        logger.info('Loaded %s PDF-only contracts with %s item rates', len(register_rows), len(line_rows))
    return pd.DataFrame(register_rows), pd.DataFrame(line_rows)


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
    requirement_value = float(requirement.get('requirement_value', 0) or 0)
    if route in {'RFP', 'EMERGENCY_PO'} or requirement_value >= 500000:
        return 'procurement_head'
    if requirement_value >= 250000:
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


PDF_CONTRACT_REGISTER, PDF_CONTRACT_LINES = load_pdf_only_contracts()
if not PDF_CONTRACT_REGISTER.empty:
    CONTRACT_REGISTER = pd.concat([CONTRACT_REGISTER, PDF_CONTRACT_REGISTER], ignore_index=True)
    CONTRACT_LINES = pd.concat([CONTRACT_LINES, PDF_CONTRACT_LINES], ignore_index=True)


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
                'source': contract.get('source', 'contract_register.csv'),
            })
    return contracts


@lru_cache(maxsize=2048)
def _get_preferred_supplier_rank(item_code: str, as_of_iso: str) -> tuple:
    as_of = datetime.fromisoformat(as_of_iso)
    target = norm(item_code)
    window_start = as_of - timedelta(days=548)
    candidates = []
    for _, supplier in SUPPLIER_MASTER.iterrows():
        if norm(supplier.get('status')) != 'active':
            continue
        supplier_id = str(supplier.get('supplier_id'))
        po_rows = PO_HISTORY.loc[PO_HISTORY['item_code'].str.lower() == target].copy()
        po_rows = po_rows[po_rows['supplier_id'].astype(str).str.lower() == supplier_id.lower()]
        po_rows['_parsed_date'] = pd.to_datetime(po_rows['po_date'], errors='coerce')
        po_rows = po_rows[
            (po_rows['_parsed_date'] <= pd.Timestamp(as_of))
            & (po_rows['_parsed_date'] >= pd.Timestamp(window_start))
        ]
        po_rows = po_rows.sort_values('po_date', ascending=False)
        latest_po = po_rows.iloc[0].to_dict() if not po_rows.empty else None
        supplier_contracts = CONTRACT_REGISTER.loc[
            CONTRACT_REGISTER['supplier_id'].astype(str).str.lower() == supplier_id.lower()
        ]
        contract_history = []
        for _, contract in supplier_contracts.iterrows():
            start = to_date(contract.get('start_date'))
            if start is None or start > as_of:
                continue
            relevant = CONTRACT_LINES.loc[
                (CONTRACT_LINES['contract_id'].astype(str) == str(contract['contract_id']))
                & (CONTRACT_LINES['item_code'].astype(str).str.lower() == target)
            ]
            for _, contract_line in relevant.iterrows():
                contract_history.append({
                    'contract_id': contract['contract_id'],
                    'rate': float(contract_line['rate']),
                    'start_date': start,
                })
        latest_contract = max(contract_history, key=lambda contract: contract['start_date']) if contract_history else None
        if latest_po is None and latest_contract is None:
            continue
        latest_po_date = to_date(latest_po['po_date']) if latest_po else None
        days = (as_of - latest_po_date).days if latest_po_date else 3650
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
            'reference_price': float(latest_po.get('unit_price', 0) or 0) if latest_po is not None else float(latest_contract['rate']),
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


def _resolve_policy_value(value: Any, policy: dict) -> Any:
    if isinstance(value, dict) and 'limit' in value:
        return policy.get('limits', {}).get(value['limit'])
    return value


def _condition_matches(condition: dict, context: dict, policy: dict) -> bool:
    if 'all' in condition:
        return all(_condition_matches(part, context, policy) for part in condition['all'])
    if 'any' in condition:
        return any(_condition_matches(part, context, policy) for part in condition['any'])
    if 'not' in condition:
        return not _condition_matches(condition['not'], context, policy)
    field = condition.get('field')
    if not field or field not in context:
        return False
    actual = context[field]
    expected = _resolve_policy_value(condition.get('value'), policy)
    operator = condition.get('operator', 'eq')
    if operator == 'eq':
        return actual == expected
    if operator == 'ne':
        return actual != expected
    if operator == 'lt':
        return actual < expected
    if operator == 'lte':
        return actual <= expected
    if operator == 'gt':
        return actual > expected
    if operator == 'gte':
        return actual >= expected
    if operator == 'in':
        return actual in expected
    if operator == 'contains':
        return expected in actual
    return False


def _most_recent_po_price(item_code: str, as_of: datetime, policy: dict) -> Optional[float]:
    days = int(policy.get('history_windows', {}).get('recent_purchase_days', 365))
    po_rows = PO_HISTORY.loc[PO_HISTORY['item_code'].astype(str).str.lower() == norm(item_code)].copy()
    po_rows['_parsed_date'] = pd.to_datetime(po_rows['po_date'], errors='coerce')
    po_rows = po_rows[
        (po_rows['_parsed_date'] <= pd.Timestamp(as_of))
        & (po_rows['_parsed_date'] >= pd.Timestamp(as_of - timedelta(days=days)))
    ].sort_values('_parsed_date', ascending=False)
    if po_rows.empty:
        return None
    return float(po_rows.iloc[0]['unit_price'])


def evaluate_requirement(requirement: dict, policy: Optional[dict] = None) -> Dict[str, Any]:
    active_policy = policy or POLICY
    req_date = to_date(requirement.get('created_at')) or datetime.utcnow()
    item = item_match(requirement.get('item_code'), requirement.get('description'))
    quantity = float(requirement.get('quantity', 0) or 0)
    estimated = float(requirement.get('estimated_unit_price', 0) or 0)
    if item is None:
        value = quantity * estimated
        item_code = None
        applicable_contracts = []
        usable_contract = None
        unit_price = estimated
        history = []
        preferred = []
    else:
        item_code = str(item['item_code'])
        applicable_contracts = sorted(
            get_contracts_for_item(item_code, req_date),
            key=lambda contract: (contract['rate'], contract['contract_id']),
        )
        usable_contract = next(
            (
                contract for contract in applicable_contracts
                if contract['consumed_value'] + quantity * contract['rate'] <= contract['value_cap']
            ),
            None,
        )
        if usable_contract is not None:
            unit_price = usable_contract['rate']
        elif applicable_contracts:
            unit_price = applicable_contracts[0]['rate']
        else:
            unit_price = _most_recent_po_price(item_code, req_date, active_policy)
            if unit_price is None:
                unit_price = float(item.get('standard_price', 0) or 0)
        value = quantity * unit_price
        history = get_active_supplier_history(item_code, req_date)
        recent_days = int(active_policy.get('history_windows', {}).get('recent_purchase_days', 365))
        recent_history = [
            row for row in history
            if 0 <= (req_date - row['po_date']).days <= recent_days
        ]
        preferred = get_preferred_supplier_rank(item_code, req_date)

    context = {
        'item_missing': item is None,
        'item_type': str(item.get('item_type', '')).lower() if item is not None else '',
        'item_category': str(item.get('category', '')) if item is not None else '',
        'plant': str(requirement.get('plant', '')).upper(),
        'value': value,
        'quantity': quantity,
        'emergency': str(requirement.get('urgency', '')).strip().lower() == 'emergency',
        'contract_available': usable_contract is not None,
        'has_applicable_contract': bool(applicable_contracts),
        'has_recent_active_supplier': bool(recent_history) if item is not None else False,
    }

    selected_rule = None
    for rule in active_policy.get('rule_priority', []):
        condition = rule.get('when')
        if condition is None or _condition_matches(condition, context, active_policy):
            selected_rule = rule
            break
    if selected_rule is None:
        return {
            'decision': 'SOURCING_REQUEST',
            'route': 'NOT_PROCESSED',
            'supplier': None,
            'clause': None,
            'contract': None,
            'requirement_value': value,
            'unit_price': unit_price,
            'policy_version': active_policy.get('version'),
            'explanation': 'No active policy rule matched this requirement; manual review is required.',
            'preferred_supplier': preferred,
            'suggestions': [],
        }

    route = selected_rule['route']
    supplier_strategy = selected_rule.get('supplier', 'none')
    if supplier_strategy == 'contract_supplier' and usable_contract:
        supplier_id = usable_contract['supplier_id']
        suggestions = []
        contract = usable_contract
    elif supplier_strategy == 'first_preferred_supplier':
        supplier_id = preferred[0]['supplier_id'] if preferred else None
        suggestions = []
        contract = None
    else:
        supplier_id = None
        contract = None
        match = re.fullmatch(r'suggest_(\d+)', supplier_strategy)
        suggestions = (
            get_supplier_suggestions(item_code, req_date, int(match.group(1)))
            if match and item_code else []
        )
    if item is None:
        suggestions = []
    explanation = (
        f"Policy v{active_policy.get('version', 'unknown')} clause {selected_rule.get('clause', 'n/a')} "
        f"selected route {route} at requirement value INR {value:,.2f}."
    )
    if contract:
        explanation += f" Contract {contract['contract_id']} supplies at INR {unit_price:,.2f} per unit."
    elif item is not None:
        explanation += f" Unit price INR {unit_price:,.2f} was selected by the policy valuation order."
    return {
        'decision': selected_rule['decision'],
        'route': route,
        'supplier': supplier_id,
        'clause': selected_rule.get('clause'),
        'contract': contract,
        'requirement_value': value,
        'unit_price': unit_price,
        'policy_version': active_policy.get('version'),
        'item_match': item,
        'explanation': explanation,
        'preferred_supplier': preferred,
        'suggestions': suggestions,
    }


def classify_requirement_route(requirement: dict) -> str:
    return evaluate_requirement(requirement)['route']


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


DECISION_EXPORT_COLUMNS = [
    'pr_no', 'pr_line', 'resolved_item_code', 'group_id', 'po_id', 'decision',
    'route', 'supplier_id', 'unit_price', 'group_value_inr',
    'suggested_supplier_ids', 'policy_version', 'reason',
]
REQUIRED_BATCH_COLUMNS = {
    'pr_no', 'pr_line', 'created_at', 'requester', 'department', 'plant',
    'item_code', 'description', 'quantity', 'uom', 'estimated_unit_price',
    'need_by_date', 'urgency',
}


def _csv_number(value: Any, *, allow_empty: bool = False) -> Optional[float]:
    if value is None or str(value).strip() == '':
        return None if allow_empty else 0.0
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def process_batch_csv(content: bytes, policy: Optional[dict] = None) -> Dict[str, Any]:
    try:
        frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ValueError(f'Could not parse CSV: {exc}') from exc
    missing_columns = sorted(REQUIRED_BATCH_COLUMNS - set(frame.columns))
    if missing_columns:
        raise ValueError(f'Missing required columns: {missing_columns}')

    raw_rows = frame.to_dict(orient='records')
    duplicate_keys: set[tuple[str, str]] = set()
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in raw_rows:
        key = (str(row.get('pr_no', '')).strip(), str(row.get('pr_line', '')).strip())
        counts[key] += 1
    duplicate_keys = {key for key, count in counts.items() if count > 1}

    records = []
    for index, row in enumerate(raw_rows, start=2):
        row_errors = []
        pr_no = str(row.get('pr_no', '')).strip()
        pr_line = str(row.get('pr_line', '')).strip()
        created_at = to_date(row.get('created_at'))
        need_by = to_date(row.get('need_by_date'))
        quantity = _csv_number(row.get('quantity'))
        estimated = _csv_number(row.get('estimated_unit_price'), allow_empty=True)
        if not pr_no or not pr_line:
            row_errors.append('pr_no and pr_line are required')
        if (pr_no, pr_line) in duplicate_keys:
            row_errors.append('duplicate pr_no and pr_line')
        if created_at is None:
            row_errors.append('created_at is invalid')
        if need_by is None:
            row_errors.append('need_by_date is invalid')
        if quantity is None or quantity <= 0:
            row_errors.append('quantity must be a positive number')
        if estimated is None and str(row.get('estimated_unit_price', '')).strip():
            row_errors.append('estimated_unit_price must be numeric')
        plant = str(row.get('plant', '')).strip().upper()
        if plant not in set(POLICY.get('plants', [])):
            row_errors.append(f'plant {plant or "(blank)"} is not configured in the active policy')
        if not str(row.get('description', '')).strip() and not str(row.get('item_code', '')).strip():
            row_errors.append('item_code or description is required')
        item = item_match(row.get('item_code'), row.get('description'))
        if item is None and estimated is None:
            row_errors.append('estimated_unit_price is required for a new item')
        records.append({
            'source_row': index,
            'input': row,
            'errors': row_errors,
            'created_at': created_at,
            'need_by': need_by,
            'quantity': quantity,
            'estimated_unit_price': estimated or 0.0,
            'plant': plant,
            'item': item,
        })

    valid_records = [record for record in records if not record['errors']]
    valid_records.sort(key=lambda record: (
        record['created_at'],
        str(record['input'].get('pr_no', '')),
        int(record['input'].get('pr_line') or 0),
    ))
    groups: list[dict] = []
    for record in valid_records:
        item_code = record['item'].get('item_code') if record['item'] else None
        matching_group = None
        if item_code:
            for group in groups:
                first = group['first']
                if group['item_code'] != item_code or first['plant'] != record['plant']:
                    continue
                day_delta = (record['created_at'].date() - first['created_at'].date()).days
                need_delta = abs((record['need_by'].date() - first['need_by'].date()).days)
                if 0 <= day_delta <= 14 and need_delta <= 7:
                    matching_group = group
                    break
        if matching_group is None:
            matching_group = {
                'first': record,
                'item_code': item_code,
                'records': [],
            }
            groups.append(matching_group)
        matching_group['records'].append(record)

    output_by_source_row: dict[int, dict] = {}
    po_groups: list[dict] = []
    for group_number, group in enumerate(groups, start=1):
        members = group['records']
        first_row = group['first']['input']
        aggregate = dict(first_row)
        aggregate['item_code'] = group['item_code'] or first_row.get('item_code')
        aggregate['quantity'] = sum(record['quantity'] or 0 for record in members)
        aggregate['created_at'] = group['first']['created_at'].isoformat(sep=' ')
        aggregate['need_by_date'] = group['first']['need_by'].strftime('%Y-%m-%d')
        aggregate['urgency'] = (
            'Emergency'
            if any(str(record['input'].get('urgency', '')).strip().lower() == 'emergency' for record in members)
            else first_row.get('urgency', 'Normal')
        )
        evaluated = evaluate_requirement(aggregate, policy=policy)
        group_id = f'GRP-{group_number:04d}'
        supplier_id = evaluated.get('supplier')
        decision = evaluated['decision']
        route = evaluated['route']
        reason = evaluated['explanation']
        if route in {'CONTRACT_PO', 'EMERGENCY_PO', 'SPOT_PO'} and not supplier_id:
            decision = 'NOT_PROCESSED'
            route = 'NOT_PROCESSED'
            reason += ' No eligible active supplier was available, so no PO was generated.'

        po_id = None
        if decision == 'PO':
            eligible = next(
                (
                    candidate for candidate in po_groups
                    if candidate['supplier_id'] == supplier_id
                    and candidate['plant'] == group['first']['plant']
                    and candidate['route'] == route
                    and (group['first']['created_at'] - candidate['first_created_at']).total_seconds() <= 24 * 60 * 60
                ),
                None,
            )
            if eligible is None:
                eligible = {
                    'po_id': f'PO-{len(po_groups) + 1:04d}',
                    'supplier_id': supplier_id,
                    'plant': group['first']['plant'],
                    'route': route,
                    'first_created_at': group['first']['created_at'],
                }
                po_groups.append(eligible)
            po_id = eligible['po_id']

        suggested_ids = ';'.join(
            str(suggestion['supplier_id'])
            for suggestion in evaluated.get('suggestions', [])
            if suggestion.get('supplier_id')
        )
        if decision == 'NOT_PROCESSED':
            suggested_ids = ''
        for record in members:
            original = record['input']
            output_by_source_row[record['source_row']] = {
                'pr_no': original.get('pr_no', ''),
                'pr_line': original.get('pr_line', ''),
                'resolved_item_code': group['item_code'] or '',
                'group_id': group_id,
                'po_id': po_id or '',
                'decision': decision,
                'route': route,
                'supplier_id': supplier_id or '',
                'unit_price': evaluated.get('unit_price') or '',
                'group_value_inr': evaluated.get('requirement_value', 0),
                'suggested_supplier_ids': suggested_ids,
                'policy_version': evaluated.get('policy_version') or (policy or POLICY).get('version', ''),
                'reason': reason,
            }
        try:
            record_decision_audit(
                pr_no=str(first_row.get('pr_no', '')),
                item_code=group['item_code'],
                route=route,
                decision=decision,
                clause=str(evaluated.get('clause') or ''),
                detail=reason,
                created_by='batch',
                required_role=determine_approval_role(
                    {**aggregate, 'requirement_value': evaluated.get('requirement_value', 0)},
                    route,
                    decision,
                ),
            )
        except sqlite3.Error:
            logger.exception('Could not persist batch decision audit for group %s', group_id)
            raise

    for record in records:
        if record['source_row'] in output_by_source_row:
            continue
        original = record['input']
        output_by_source_row[record['source_row']] = {
            'pr_no': original.get('pr_no', ''),
            'pr_line': original.get('pr_line', ''),
            'resolved_item_code': record['item'].get('item_code', '') if record['item'] else '',
            'group_id': '',
            'po_id': '',
            'decision': 'NOT_PROCESSED',
            'route': 'NOT_PROCESSED',
            'supplier_id': '',
            'unit_price': '',
            'group_value_inr': '',
            'suggested_supplier_ids': '',
            'policy_version': (policy or POLICY).get('version', ''),
            'reason': '; '.join(record['errors']),
        }

    results = [output_by_source_row[index] for index in range(2, len(raw_rows) + 2)]
    export_buffer = io.StringIO(newline='')
    writer = csv.DictWriter(export_buffer, fieldnames=DECISION_EXPORT_COLUMNS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(results)
    return {
        'rows_processed': len(raw_rows),
        'processed_rows': sum(row['decision'] != 'NOT_PROCESSED' for row in results),
        'not_processed_rows': sum(row['decision'] == 'NOT_PROCESSED' for row in results),
        'groups_created': len(groups),
        'purchase_orders_created': len(po_groups),
        'results': results,
        'decision_csv': export_buffer.getvalue(),
    }


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
        result = evaluate_requirement(row.to_dict())
        totals[result['decision']] += 1
        per_route[result['route']] += 1
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
    required_role = determine_approval_role(
        {**payload.model_dump(), 'requirement_value': result['requirement_value']},
        result['route'],
        result['decision'],
    )
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
    as_of_date = to_date(as_of)
    if as_of_date is None:
        raise HTTPException(status_code=400, detail='as_of must be a valid date.')
    return get_preferred_supplier_rank(item_code, as_of_date)


@app.get('/api/export/supplier-rankings')
def export_supplier_rankings(as_of: str) -> Response:
    as_of_date = to_date(as_of)
    if as_of_date is None:
        raise HTTPException(status_code=400, detail='as_of must be a valid date.')
    buffer = io.StringIO(newline='')
    columns = ['item_code', 'rank', 'supplier_id', 'reference_price']
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator='\n')
    writer.writeheader()
    for item_code in ITEM_MASTER['item_code'].astype(str).unique():
        for supplier in get_preferred_supplier_rank(item_code, as_of_date)[:3]:
            writer.writerow({
                'item_code': item_code,
                'rank': supplier['rank'],
                'supplier_id': supplier['supplier_id'],
                'reference_price': supplier['reference_price'],
            })
    return Response(
        content=buffer.getvalue(),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename="supplier-rankings.csv"'},
    )


@app.post('/api/process-batch')
async def process_batch(file: UploadFile = File(...), current_user: dict = Depends(require_admin)) -> Dict[str, Any]:
    del current_user
    if not file.filename or not file.filename.lower().endswith('.csv'):
        raise HTTPException(status_code=400, detail='Only CSV files are accepted.')
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail='Uploaded CSV is empty.')
    try:
        return process_batch_csv(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        evaluation = evaluate_requirement(row.to_dict())
        results.append({
            'pr_no': row.get('pr_no'),
            'item_code': row.get('item_code'),
            'plant': row.get('plant'),
            'route': evaluation['route'],
            'decision': evaluation['decision'],
            'value': evaluation['requirement_value'],
            'clause': evaluation['clause'],
        })
    return results


@app.get('/')
def root() -> Dict[str, str]:
    return {'message': 'Corvo Procurement Automation Platform'}
