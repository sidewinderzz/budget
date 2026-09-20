from datetime import date

import httpx
import pytest

from app import config
from app.repositories import accounts as accounts_repo
from app.repositories import settings as settings_repo
from app.services import digest

TODAY = date(2026, 7, 30)


def _mock_client(handler):
    return httpx.MockTransport(handler)


def test_skips_when_no_digest_email_set(db, user_id):
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    db.commit()
    sent = digest.send_digest(db, user_id, TODAY, transport=_mock_client(lambda r: httpx.Response(200)))
    assert sent is False


def test_raises_when_api_key_missing(db, user_id, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", None)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()
    with pytest.raises(digest.DigestError):
        digest.send_digest(db, user_id, TODAY, transport=_mock_client(lambda r: httpx.Response(200)))


def test_sends_correct_resend_payload(db, user_id, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    monkeypatch.setattr(config, "DIGEST_FROM_EMAIL", "budget@example.com")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()

    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    sent = digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))

    assert sent is True
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["from"] == "budget@example.com"
    assert captured["body"]["to"] == ["alice@example.com"]
    assert "$1,000.00" in captured["body"]["text"]
    assert "Safe to spend" in captured["body"]["text"]


def test_digest_leads_with_what_happened_not_the_standing_balance(db, user_id, monkeypatch):
    """The digest used to open with a figure that could sit byte-identical for days,
    which read as noise. Activity comes first now."""
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))
    text = captured["body"]["text"]

    assert text.startswith("WHAT HAPPENED")
    assert text.index("WHAT HAPPENED") < text.index("WHERE YOU STAND")
    # Nothing in the ledger for this user, and it says so rather than staying silent.
    assert "Nothing recorded in the last 7 days." in text


def test_digest_summarizes_recent_spending(db, user_id, monkeypatch):
    from app.repositories import transactions as transactions_repo

    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    transactions_repo.create_transaction(
        db, user_id, "2026-07-28", 1274, "spending", category="gas", memo="ALLSUP"
    )
    transactions_repo.create_transaction(
        db, user_id, "2026-07-29", 806, "spending", category="food", memo="SONIC"
    )
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))
    text = captured["body"]["text"]

    assert "You spent $20.80 across 2 purchases." in text


def test_digest_subject_reports_movement_when_safe_to_spend_changed(db, user_id, monkeypatch):
    from app.repositories import snapshots as snapshots_repo

    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    snapshots_repo.upsert_snapshot(db, user_id, "2026-07-29", {
        "cash_on_hand_cents": 110000, "total_debt_cents": 0, "net_position_cents": 110000,
        "reserved_cash_cents": 0, "safe_to_spend_cents": 110000,
        "forecast_position_cents": 110000, "protected_floor_cents": 0, "window_days": 30,
    })
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))
    # Yesterday 1100.00 -> today 1000.00
    assert captured["body"]["subject"] == "Budget: down $100.00 — $1,000.00 safe to spend"


def test_digest_subject_omits_movement_when_unchanged(db, user_id, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))
    assert captured["body"]["subject"] == "Budget: $1,000.00 safe to spend"


def test_raises_on_non_2xx_response(db, user_id, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()

    def handler(request):
        return httpx.Response(422, text="Invalid `from` field")

    with pytest.raises(digest.DigestError):
        digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))


def test_digest_includes_next_due_payment(db, user_id, monkeypatch):
    from app.repositories import obligations as obligations_repo

    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    obligations_repo.create_obligation(db, user_id, "Rent", "housing", 90000, "2026-08-01")
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))
    assert "Rent" in captured["body"]["text"]
    assert "$900.00" in captured["body"]["text"]


def test_digest_sends_theme_matched_html_alongside_text(db, user_id, monkeypatch):
    """The HTML body mirrors the plain-text content (same sections, same figures)
    and carries the app's own dark palette so it reads as the same product."""
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))

    html = captured["body"]["html"]
    text = captured["body"]["text"]
    assert html.startswith("<!doctype html>")
    assert "$1,000.00" in html
    for heading in ("What happened", "Coming up", "Where you stand"):
        assert heading in html
        assert heading.upper() in text
    # Same dark ground / accent-muted tokens app.css uses -- inlined, since email
    # clients don't reliably honor stylesheets.
    assert "#161826" in html and "#232532" in html
    assert 'name="color-scheme" content="dark"' in html


def test_digest_html_escapes_user_supplied_names(db, user_id, monkeypatch):
    from app.repositories import obligations as obligations_repo

    monkeypatch.setattr(config, "RESEND_API_KEY", "test-key")
    accounts_repo.create_account(db, user_id, "Checking", "checking", 100000)
    settings_repo.upsert(db, user_id, "digest_email", "alice@example.com")
    obligations_repo.create_obligation(
        db, user_id, "<b>Rent</b>", "housing", 90000, "2026-08-01"
    )
    db.commit()

    captured = {}

    def handler(request):
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "abc"})

    digest.send_digest(db, user_id, TODAY, transport=_mock_client(handler))

    assert "<b>Rent</b>" not in captured["body"]["html"]
    assert "&lt;b&gt;Rent&lt;/b&gt;" in captured["body"]["html"]
    assert "<b>Rent</b>" in captured["body"]["text"]
