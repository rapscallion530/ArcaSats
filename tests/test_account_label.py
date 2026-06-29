"""Account label is now a KYC-status dropdown (KYC / Non-KYC); legacy free-text labels move to the
note (split_kyc_label + migration 0007)."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account
from app.services import accounts as acc


def test_split_kyc_label_normalizes_and_relocates():
    assert acc.split_kyc_label("KYC", "") == ("KYC", "")
    assert acc.split_kyc_label("kyc", "n") == ("KYC", "n")
    assert acc.split_kyc_label("non-KYC", "") == ("non-KYC", "")
    assert acc.split_kyc_label("non kyc", "") == ("non-KYC", "")
    assert acc.split_kyc_label("", "keep") == ("", "keep")
    # custom free text -> non-KYC, original preserved in the note
    lbl, note = acc.split_kyc_label("Coinbase main", "")
    assert lbl == "non-KYC" and "Label: Coinbase main" in note
    assert acc.split_kyc_label("Cold storage", "existing") == ("non-KYC", "existing\nLabel: Cold storage")


def test_create_account_accepts_dropdown_value(session):
    a = acc.create_account(session, name="NonKycAcct", label_kind="non-KYC")
    assert a.label_kind == "non-KYC"


def test_add_form_renders_kyc_dropdown(client):
    html = client.get("/partials/add-account-form").text
    assert "KYC Status" in html and "Label (optional)" not in html
    assert 'name="label_kind"' in html and "<option value=\"non-KYC\"" in html


def test_edit_form_preselects_current_kyc_status(client):
    client.post("/accounts", data={"name": "EditKyc", "label_kind": "KYC"})
    with SessionLocal() as s:
        aid = s.scalar(select(Account.id).where(Account.name == "EditKyc"))
    html = client.get(f"/accounts/{aid}/edit-form").text
    assert "KYC Status" in html
    assert '<option value="KYC" selected>' in html       # current value preselected
