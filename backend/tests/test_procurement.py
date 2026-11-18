import csv
import io
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import sys

import pandas as pd
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main


ROOT = Path(__file__).resolve().parents[2]
client = TestClient(main.app)


def requirement(**overrides):
    base = {
        'pr_no': 'PR-TEST-001',
        'pr_line': 1,
        'created_at': '2026-11-04 12:40',
        'requester': 'Test Requester',
        'department': 'Operations',
        'plant': 'PUN1',
        'item_code': 'ITM-1002',
        'description': 'MS sheet 3mm 1250x2500',
        'quantity': 15,
        'uom': 'NOS',
        'estimated_unit_price': 11873.67,
        'need_by_date': '2026-11-15',
        'urgency': 'Normal',
    }
    return {**base, **overrides}


def csv_bytes(rows):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=main.REQUIRED_BATCH_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def test_password_hash_is_salted_and_legacy_hashes_are_upgraded():
    stored_hash = main.hash_password('correct horse battery staple')
    assert stored_hash != main.hash_password('correct horse battery staple')
    assert main.verify_password('correct horse battery staple', stored_hash) == (True, False)
    assert main.verify_password('incorrect password', stored_hash) == (False, False)
    legacy_hash = sha256(b'legacy password').hexdigest()
    assert main.verify_password('legacy password', legacy_hash) == (True, True)


def test_worked_example_contract_uses_contract_rate():
    result = main.evaluate_requirement(requirement())
    assert result['route'] == 'CONTRACT_PO'
    assert result['supplier'] == 'S001'
    assert result['unit_price'] == 9168.71
    assert result['requirement_value'] == 137530.65
    assert result['contract']['contract_id'] == 'RC-2025-011'


def test_worked_example_emergency_at_or_below_threshold():
    result = main.evaluate_requirement(requirement(
        pr_no='PR-26-00059',
        created_at='2026-11-06 18:50',
        item_code='ITM-1026',
        description='Gearbox oil seal 45x62x8',
        quantity=2380,
        estimated_unit_price=0,
        urgency='Emergency',
    ))
    assert result['route'] == 'EMERGENCY_PO'
    assert result['supplier'] == 'S014'
    assert result['requirement_value'] == 253184.4


def test_worked_example_pdf_only_contract_is_loaded():
    result = main.evaluate_requirement(requirement(
        pr_no='PR-26-00045',
        created_at='2026-11-18 10:50',
        plant='CHK1',
        item_code='ITM-1037',
        description='Solenoid valve 5/2 1/4 inch',
        quantity=11,
        estimated_unit_price=0,
        need_by_date='2026-12-13',
    ))
    assert result['route'] == 'CONTRACT_PO'
    assert result['supplier'] == 'S021'
    assert result['contract']['contract_id'] == 'RC-2026-031'
    assert result['requirement_value'] == 26829.11


def test_new_item_is_not_suggested_or_clubbed():
    result = main.evaluate_requirement(requirement(
        item_code='',
        description='Uncatalogued industrial widget',
        quantity=2,
        estimated_unit_price=300,
    ))
    assert result['route'] == 'NEW_ITEM_SOURCING'
    assert result['requirement_value'] == 600
    assert result['suggestions'] == []


def test_inactive_suppliers_are_not_preferred():
    ranked = main.get_preferred_supplier_rank('ITM-1048', '2026-11-01')
    supplier_ids = {row['supplier_id'] for row in ranked}
    assert 'S031' not in supplier_ids
    assert all(
        main.get_supplier_by_id(supplier_id)['status'].lower() == 'active'
        for supplier_id in supplier_ids
    )


def test_preferred_supplier_history_obeys_548_day_window(monkeypatch):
    as_of = datetime(2026, 11, 1)
    monkeypatch.setattr(main, 'PO_HISTORY', pd.DataFrame([{
        'po_number': 'old',
        'po_date': '2025-05-01',
        'supplier_id': 'S002',
        'item_code': 'ITM-1002',
        'quantity': 1,
        'uom': 'NOS',
        'unit_price': 1,
        'plant': 'PUN1',
        'delivered_on_time': True,
    }]))
    monkeypatch.setattr(main, 'CONTRACT_REGISTER', main.CONTRACT_REGISTER.iloc[0:0].copy())
    monkeypatch.setattr(main, 'CONTRACT_LINES', main.CONTRACT_LINES.iloc[0:0].copy())
    main._get_preferred_supplier_rank.cache_clear()
    try:
        assert main.get_preferred_supplier_rank('ITM-1002', as_of) == []
    finally:
        main._get_preferred_supplier_rank.cache_clear()


def test_batch_clubs_lines_and_consolidates_po(monkeypatch):
    monkeypatch.setattr(main, 'record_decision_audit', lambda **kwargs: None)
    rows = [
        requirement(pr_no='PR-TEST-101', created_at='2026-11-02 10:00', quantity=5),
        requirement(pr_no='PR-TEST-102', created_at='2026-11-03 10:00', quantity=7),
    ]
    result = main.process_batch_csv(csv_bytes(rows))
    assert result['rows_processed'] == 2
    assert result['groups_created'] == 1
    assert result['results'][0]['group_id'] == result['results'][1]['group_id']
    assert result['results'][0]['group_value_inr'] == 12 * 9168.71
    assert result['results'][0]['po_id'] == result['results'][1]['po_id']
    assert result['results'][0]['decision'] == 'PO'


def test_invalid_and_duplicate_batch_lines_are_not_processed(monkeypatch):
    monkeypatch.setattr(main, 'record_decision_audit', lambda **kwargs: None)
    valid = requirement(pr_no='PR-DUP', pr_line=1)
    invalid = requirement(pr_no='PR-INVALID', pr_line=1, quantity=0)
    result = main.process_batch_csv(csv_bytes([valid, valid.copy(), invalid]))
    assert result['not_processed_rows'] == 3
    assert all(row['route'] == 'NOT_PROCESSED' for row in result['results'])
    assert 'duplicate pr_no and pr_line' in result['results'][0]['reason']
    assert 'quantity must be a positive number' in result['results'][2]['reason']


def test_sample_batch_processes_all_186_lines(monkeypatch):
    monkeypatch.setattr(main, 'record_decision_audit', lambda **kwargs: None)
    content = (ROOT / 'pr_batches' / 'sample_pr_batch.csv').read_bytes()
    result = main.process_batch_csv(content)
    assert result['rows_processed'] == 186
    assert len(result['results']) == 186
    assert result['decision_csv'].splitlines()[0] == ','.join(main.DECISION_EXPORT_COLUMNS)


def test_process_batch_api_returns_structured_csv(monkeypatch):
    monkeypatch.setattr(main, 'record_decision_audit', lambda **kwargs: None)
    login = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'})
    assert login.status_code == 200
    response = client.post(
        '/api/process-batch',
        headers={'Authorization': f"Bearer {login.json()['token']}"},
        files={'file': ('batch.csv', csv_bytes([requirement()]), 'text/csv')},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload['rows_processed'] == 1
    assert payload['results'][0]['route'] == 'CONTRACT_PO'
    assert payload['decision_csv'].splitlines()[0] == ','.join(main.DECISION_EXPORT_COLUMNS)


def test_policy_rule_configuration_controls_threshold():
    policy = deepcopy(main.POLICY)
    policy['limits']['spot_po_threshold'] = 1
    result = main.evaluate_requirement(requirement(
        item_code='ITM-1026',
        description='Gearbox oil seal 45x62x8',
        quantity=1,
        estimated_unit_price=0,
    ), policy=policy)
    assert result['route'] != 'SPOT_PO'
