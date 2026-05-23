"""NetShare Pro - End-to-end backend API tests.
Covers: auth (register/login/me), earnings (start/status/stop), settings,
payouts, referral, premium upgrade, admin overview.
"""
import os
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://passive-share.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@netsharepro.com"
ADMIN_PASSWORD = "NetShare2026!Admin"


# ---------- Fixtures ----------
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(session):
    r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"Admin login failed: {r.text}"
    data = r.json()
    assert data["user"]["role"] == "admin"
    assert data["user"]["is_premium"] is True
    return data["access_token"]


@pytest.fixture(scope="session")
def user_creds():
    email = f"test_{uuid.uuid4().hex[:10]}@netsharepro.com"
    return {"email": email, "password": "demo12345", "name": "Test User"}


@pytest.fixture(scope="session")
def user_token(session, user_creds):
    r = session.post(f"{API}/auth/register", json=user_creds)
    assert r.status_code == 201, f"Register failed: {r.text}"
    data = r.json()
    assert "access_token" in data
    assert data["user"]["referral_code"]
    assert data["user"]["role"] == "user"
    assert data["user"]["is_premium"] is False
    return data["access_token"]


def auth_h(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------- Auth ----------
class TestAuth:
    def test_root(self, session):
        r = session.get(f"{API}/")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_register_duplicate(self, session, user_creds, user_token):
        # user already registered above
        r = session.post(f"{API}/auth/register", json=user_creds)
        assert r.status_code == 409

    def test_login_admin(self, admin_token):
        assert admin_token

    def test_login_wrong_password(self, session):
        r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert r.status_code == 401

    def test_me_with_token(self, session, user_token):
        r = session.get(f"{API}/auth/me", headers=auth_h(user_token))
        assert r.status_code == 200
        u = r.json()
        assert u["role"] == "user"
        assert u["referral_code"]

    def test_me_without_token(self, session):
        r = session.get(f"{API}/auth/me")
        assert r.status_code == 401


# ---------- Earnings ----------
class TestEarnings:
    def test_start_status_stop_flow(self, session, user_token):
        # bump intensity to 'strong' so earnings register at 4-decimal precision in short test
        session.put(f"{API}/settings", headers=auth_h(user_token), json={"intensity": "strong"})
        # start
        r = session.post(f"{API}/earnings/start", headers=auth_h(user_token))
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["is_sharing"] is True
        assert s["rate_per_hour"] > 0
        # wait long enough for >0.0001 EUR at 4-decimal rounding
        time.sleep(8)
        # status
        r2 = session.get(f"{API}/earnings/status", headers=auth_h(user_token))
        assert r2.status_code == 200
        s2 = r2.json()
        assert s2["is_sharing"] is True
        assert s2["active_seconds_today"] >= 2
        # stop & settle
        r3 = session.post(f"{API}/earnings/stop", headers=auth_h(user_token))
        assert r3.status_code == 200
        s3 = r3.json()
        assert s3["is_sharing"] is False
        # After settling, balance & today_earned must reflect run
        assert s3["today_earned"] > 0
        assert s3["balance"] > 0

    def test_history(self, session, user_token):
        r = session.get(f"{API}/earnings/history", headers=auth_h(user_token))
        assert r.status_code == 200
        assert "items" in r.json()


# ---------- Settings ----------
class TestSettings:
    def test_get_settings(self, session, user_token):
        r = session.get(f"{API}/settings", headers=auth_h(user_token))
        assert r.status_code == 200
        body = r.json()
        assert "settings" in body
        assert body["settings"]["intensity"] in ("light", "normal", "strong")

    def test_update_settings(self, session, user_token):
        payload = {
            "intensity": "strong",
            "battery_limit": 30,
            "temp_limit": 50,
            "pause_on_gaming": False,
            "language": "en",
            "payout_method": "paypal",
            "payout_address": "test@paypal.com",
        }
        r = session.put(f"{API}/settings", headers=auth_h(user_token), json=payload)
        assert r.status_code == 200
        # verify via GET
        r2 = session.get(f"{API}/settings", headers=auth_h(user_token))
        body = r2.json()
        assert body["settings"]["intensity"] == "strong"
        assert body["settings"]["battery_limit"] == 30
        assert body["settings"]["temp_limit"] == 50
        assert body["settings"]["pause_on_gaming"] is False
        assert body["language"] == "en"
        assert body["payout_method"] == "paypal"
        assert body["payout_address"] == "test@paypal.com"


# ---------- Payouts ----------
class TestPayouts:
    def test_payout_below_min(self, session, user_token):
        r = session.post(
            f"{API}/payout/request",
            headers=auth_h(user_token),
            json={"amount": 2.0, "method": "paypal", "address": "x@y.com"},
        )
        assert r.status_code == 400
        assert "5" in r.json()["detail"]

    def test_payout_insufficient(self, session, user_token):
        r = session.post(
            f"{API}/payout/request",
            headers=auth_h(user_token),
            json={"amount": 100.0, "method": "paypal", "address": "x@y.com"},
        )
        assert r.status_code == 400
        assert "Insufficient" in r.json()["detail"]

    def test_payout_invalid_method(self, session, user_token):
        r = session.post(
            f"{API}/payout/request",
            headers=auth_h(user_token),
            json={"amount": 10.0, "method": "btc", "address": "x"},
        )
        assert r.status_code == 400

    def test_payout_history_empty(self, session, user_token):
        r = session.get(f"{API}/payout/history", headers=auth_h(user_token))
        assert r.status_code == 200
        assert "items" in r.json()


# ---------- Referral ----------
class TestReferral:
    def test_referral_info(self, session, user_token):
        r = session.get(f"{API}/referral", headers=auth_h(user_token))
        assert r.status_code == 200
        body = r.json()
        assert body["code"]
        assert body["referred_count"] == 0
        assert body["bonus_per_referral"] == 1.5


# ---------- Premium ----------
class TestPremium:
    def test_upgrade(self, session, user_token):
        r = session.post(f"{API}/premium/upgrade", headers=auth_h(user_token))
        assert r.status_code == 200
        assert r.json()["is_premium"] is True
        # verify via /me
        me = session.get(f"{API}/auth/me", headers=auth_h(user_token)).json()
        assert me["is_premium"] is True


# ---------- Admin ----------
class TestAdmin:
    def test_admin_overview(self, session, admin_token):
        r = session.get(f"{API}/admin/overview", headers=auth_h(admin_token))
        assert r.status_code == 200
        body = r.json()
        for k in (
            "total_users",
            "active_users_now",
            "premium_users",
            "platform_commission_total",
            "platform_commission_today",
            "gross_traffic_total",
            "payouts_by_status",
        ):
            assert k in body

    def test_admin_overview_forbidden_for_user(self, session, user_token):
        r = session.get(f"{API}/admin/overview", headers=auth_h(user_token))
        assert r.status_code == 403

    def test_admin_users_list(self, session, admin_token):
        r = session.get(f"{API}/admin/users", headers=auth_h(admin_token))
        assert r.status_code == 200
        assert "items" in r.json()
