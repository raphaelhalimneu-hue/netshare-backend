"""NetShare Pro Backend - One-Tap Edition
FastAPI + MongoDB with JWT auth, simulated bandwidth earnings, payouts, referrals.
"""
from fastapi import FastAPI, APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Annotated
import uuid
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta
import random

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT config
JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGORITHM = os.environ.get('JWT_ALGORITHM', 'HS256')
JWT_EXPIRE_MINUTES = int(os.environ.get('JWT_EXPIRE_MINUTES', '10080'))
ADMIN_EMAIL = os.environ['ADMIN_EMAIL']
ADMIN_PASSWORD = os.environ['ADMIN_PASSWORD']

app = FastAPI(title="NetShare Pro API")
api = APIRouter(prefix="/api")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)


# ============================================================
# MODELS
# ============================================================
class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    role: str = "user"
    is_premium: bool = False
    referral_code: str
    balance: float = 0.0
    total_earned: float = 0.0
    total_gb_shared: float = 0.0
    is_sharing: bool = False
    payout_method: Optional[str] = None
    payout_address: Optional[str] = None
    language: str = "de"
    created_at: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class SettingsUpdate(BaseModel):
    intensity: Optional[str] = None  # "light" | "normal" | "strong"
    battery_limit: Optional[int] = None  # 0-100
    temp_limit: Optional[int] = None  # 30-60
    pause_on_gaming: Optional[bool] = None
    language: Optional[str] = None
    payout_method: Optional[str] = None  # "paypal" | "bank" | "crypto"
    payout_address: Optional[str] = None


class PayoutRequest(BaseModel):
    amount: float
    method: str  # paypal | bank | crypto
    address: str


class EarningsStatus(BaseModel):
    is_sharing: bool
    today_earned: float
    today_gb: float
    balance: float
    total_earned: float
    progress_to_payout: float  # 0..1 toward 5€
    active_seconds_today: int
    rate_per_hour: float


# ============================================================
# HELPERS
# ============================================================
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')


def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode('utf-8'), h.encode('utf-8'))
    except Exception:
        return False


def create_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def gen_referral_code() -> str:
    return uuid.uuid4().hex[:8].upper()


def user_to_public(u: dict) -> UserPublic:
    return UserPublic(
        id=u['id'],
        email=u['email'],
        name=u.get('name'),
        role=u.get('role', 'user'),
        is_premium=u.get('is_premium', False),
        referral_code=u.get('referral_code', ''),
        balance=round(u.get('balance', 0.0), 4),
        total_earned=round(u.get('total_earned', 0.0), 4),
        total_gb_shared=round(u.get('total_gb_shared', 0.0), 4),
        is_sharing=u.get('is_sharing', False),
        payout_method=u.get('payout_method'),
        payout_address=u.get('payout_address'),
        language=u.get('language', 'de'),
        created_at=u.get('created_at', datetime.now(timezone.utc).isoformat()),
    )


async def get_current_user(token: Annotated[Optional[str], Depends(oauth2_scheme)]) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def require_admin(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# Simulated earnings rates (EUR per hour, GB per hour)
INTENSITY_RATES = {
    "light":  {"eur_per_hour": 0.025, "gb_per_hour": 0.08},
    "normal": {"eur_per_hour": 0.055, "gb_per_hour": 0.20},
    "strong": {"eur_per_hour": 0.095, "gb_per_hour": 0.35},
}
PREMIUM_MULTIPLIER = 1.0  # premium = comfort (no earnings boost). Equal earnings rate.
PLATFORM_FEE_FREE = 0.30   # 30% platform cut for free users
PLATFORM_FEE_PREMIUM = 0.10  # 10% for premium
PREMIUM_PRICE_EUR = 1.99   # monthly price
PREMIUM_MAX_DEVICES = 5
FREE_MAX_DEVICES = 1
REWARD_PER_AD_EUR = 0.05   # rewarded video credit
REWARD_DAILY_CAP = 1.50    # max 1.50€/day from ad rewards (= 30 ads)


async def _compute_live_earnings(user: dict) -> tuple[float, float, int]:
    """Compute the unrealized earnings since session_started_at.
    Returns (eur, gb, seconds)."""
    if not user.get('is_sharing'):
        return 0.0, 0.0, 0
    started = user.get('session_started_at')
    if not started:
        return 0.0, 0.0, 0
    start_dt = datetime.fromisoformat(started)
    now = datetime.now(timezone.utc)
    elapsed = (now - start_dt).total_seconds()
    if elapsed <= 0:
        return 0.0, 0.0, 0

    settings = user.get('settings', {})
    intensity = settings.get('intensity', 'normal')
    rates = INTENSITY_RATES.get(intensity, INTENSITY_RATES['normal'])
    mult = PREMIUM_MULTIPLIER if user.get('is_premium') else 1.0
    hours = elapsed / 3600.0
    # Add some realistic noise (deterministic per minute)
    noise = 0.9 + ((int(elapsed) % 60) / 600.0)
    gross_eur = rates['eur_per_hour'] * hours * mult * noise
    fee = PLATFORM_FEE_PREMIUM if user.get('is_premium') else PLATFORM_FEE_FREE
    net_eur = gross_eur * (1 - fee)
    platform_cut = gross_eur * fee  # used by admin aggregation
    gb = rates['gb_per_hour'] * hours * mult * noise
    return net_eur, gb, int(elapsed), platform_cut if False else net_eur  # keep tuple shape


async def get_live_status(user: dict) -> EarningsStatus:
    """Compute live status without persisting."""
    today = datetime.now(timezone.utc).date().isoformat()
    today_doc = await db.daily_stats.find_one({"user_id": user['id'], "date": today}, {"_id": 0}) or {}
    today_earned = today_doc.get('earned', 0.0)
    today_gb = today_doc.get('gb', 0.0)
    active_seconds = today_doc.get('active_seconds', 0)

    # Add unrealized session earnings
    if user.get('is_sharing') and user.get('session_started_at'):
        start_dt = datetime.fromisoformat(user['session_started_at'])
        elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
        settings = user.get('settings', {})
        intensity = settings.get('intensity', 'normal')
        rates = INTENSITY_RATES.get(intensity, INTENSITY_RATES['normal'])
        mult = PREMIUM_MULTIPLIER if user.get('is_premium') else 1.0
        hours = elapsed / 3600.0
        gross = rates['eur_per_hour'] * hours * mult
        fee = PLATFORM_FEE_PREMIUM if user.get('is_premium') else PLATFORM_FEE_FREE
        live_net = gross * (1 - fee)
        live_gb = rates['gb_per_hour'] * hours * mult
        today_earned += live_net
        today_gb += live_gb
        active_seconds += int(elapsed)

    settings = user.get('settings', {})
    intensity = settings.get('intensity', 'normal')
    rates = INTENSITY_RATES.get(intensity, INTENSITY_RATES['normal'])
    mult = PREMIUM_MULTIPLIER if user.get('is_premium') else 1.0
    fee = PLATFORM_FEE_PREMIUM if user.get('is_premium') else PLATFORM_FEE_FREE
    rate_per_hour = rates['eur_per_hour'] * mult * (1 - fee)

    balance = user.get('balance', 0.0) + (today_earned - today_doc.get('earned', 0.0))
    # The unsettled part is already counted as live; balance shown reflects pending too
    progress = min(1.0, balance / 5.0) if balance > 0 else 0.0

    return EarningsStatus(
        is_sharing=user.get('is_sharing', False),
        today_earned=round(today_earned, 4),
        today_gb=round(today_gb, 4),
        balance=round(balance, 4),
        total_earned=round(user.get('total_earned', 0.0) + (today_earned - today_doc.get('earned', 0.0)), 4),
        progress_to_payout=round(progress, 4),
        active_seconds_today=active_seconds,
        rate_per_hour=round(rate_per_hour, 4),
    )


async def settle_session(user: dict) -> dict:
    """Persist live earnings to daily_stats and user balance. Stops the session."""
    if not user.get('is_sharing') or not user.get('session_started_at'):
        return user
    start_dt = datetime.fromisoformat(user['session_started_at'])
    elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds()
    if elapsed <= 0:
        elapsed = 0

    settings = user.get('settings', {})
    intensity = settings.get('intensity', 'normal')
    rates = INTENSITY_RATES.get(intensity, INTENSITY_RATES['normal'])
    mult = PREMIUM_MULTIPLIER if user.get('is_premium') else 1.0
    hours = elapsed / 3600.0
    gross_eur = rates['eur_per_hour'] * hours * mult
    fee_pct = PLATFORM_FEE_PREMIUM if user.get('is_premium') else PLATFORM_FEE_FREE
    net_eur = gross_eur * (1 - fee_pct)
    fee_eur = gross_eur * fee_pct
    gb = rates['gb_per_hour'] * hours * mult

    today = datetime.now(timezone.utc).date().isoformat()
    # Upsert daily_stats
    await db.daily_stats.update_one(
        {"user_id": user['id'], "date": today},
        {
            "$inc": {
                "earned": net_eur,
                "gb": gb,
                "active_seconds": int(elapsed),
                "platform_fee": fee_eur,
            },
            "$setOnInsert": {"user_id": user['id'], "date": today},
        },
        upsert=True,
    )
    # Update user
    await db.users.update_one(
        {"id": user['id']},
        {
            "$inc": {
                "balance": net_eur,
                "total_earned": net_eur,
                "total_gb_shared": gb,
            },
            "$set": {
                "is_sharing": False,
                "session_started_at": None,
                "last_settled_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    # Track platform earnings (admin)
    await db.platform_earnings.update_one(
        {"date": today},
        {"$inc": {"total_fee": fee_eur, "total_gross": gross_eur, "active_users": 0}},
        upsert=True,
    )
    return await db.users.find_one({"id": user['id']}, {"_id": 0})


# ============================================================
# STARTUP - create admin + indexes
# ============================================================
@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.users.create_index("referral_code", unique=True, sparse=True)
    await db.daily_stats.create_index([("user_id", 1), ("date", 1)], unique=True)
    await db.platform_earnings.create_index("date", unique=True)

    # Seed admin user
    admin = await db.users.find_one({"email": ADMIN_EMAIL})
    if not admin:
        admin_doc = {
            "id": str(uuid.uuid4()),
            "email": ADMIN_EMAIL,
            "password_hash": hash_password(ADMIN_PASSWORD),
            "name": "Platform Admin",
            "role": "admin",
            "is_premium": True,
            "referral_code": "ADMIN001",
            "balance": 0.0,
            "total_earned": 0.0,
            "total_gb_shared": 0.0,
            "is_sharing": False,
            "session_started_at": None,
            "settings": {
                "intensity": "normal",
                "battery_limit": 20,
                "temp_limit": 45,
                "pause_on_gaming": True,
            },
            "language": "de",
            "payout_method": None,
            "payout_address": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await db.users.insert_one(admin_doc)
            logging.info(f"Admin seeded: {ADMIN_EMAIL}")
        except DuplicateKeyError:
            pass


@app.on_event("shutdown")
async def shutdown():
    client.close()


# ============================================================
# AUTH ROUTES
# ============================================================
@api.post("/auth/register", response_model=TokenResponse, status_code=201)
async def register(payload: UserRegister):
    email = payload.email.lower()
    referred_by = None
    user_doc = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": payload.name,
        "role": "user",
        "is_premium": False,
        "referral_code": gen_referral_code(),
        "balance": 0.0,
        "total_earned": 0.0,
        "total_gb_shared": 0.0,
        "is_sharing": False,
        "session_started_at": None,
        "settings": {
            "intensity": "normal",
            "battery_limit": 20,
            "temp_limit": 45,
            "pause_on_gaming": True,
        },
        "language": "de",
        "payout_method": None,
        "payout_address": None,
        "referred_by": referred_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.users.insert_one(user_doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Email already registered")

    token = create_token(user_doc['id'], user_doc['role'])
    return TokenResponse(access_token=token, user=user_to_public(user_doc))


@api.post("/auth/login", response_model=TokenResponse)
async def login(payload: UserLogin):
    user = await db.users.find_one({"email": payload.email.lower()}, {"_id": 0})
    if not user or not verify_password(payload.password, user['password_hash']):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(user['id'], user.get('role', 'user'))
    return TokenResponse(access_token=token, user=user_to_public(user))


@api.get("/auth/me", response_model=UserPublic)
async def me(user: Annotated[dict, Depends(get_current_user)]):
    return user_to_public(user)


# ============================================================
# EARNINGS ROUTES
# ============================================================
@api.post("/earnings/start", response_model=EarningsStatus)
async def start_earning(user: Annotated[dict, Depends(get_current_user)]):
    if user.get('is_sharing'):
        return await get_live_status(user)
    await db.users.update_one(
        {"id": user['id']},
        {"$set": {"is_sharing": True, "session_started_at": datetime.now(timezone.utc).isoformat()}},
    )
    user = await db.users.find_one({"id": user['id']}, {"_id": 0})
    return await get_live_status(user)


@api.post("/earnings/stop", response_model=EarningsStatus)
async def stop_earning(user: Annotated[dict, Depends(get_current_user)]):
    user = await settle_session(user)
    return await get_live_status(user)


@api.get("/earnings/status", response_model=EarningsStatus)
async def earnings_status(user: Annotated[dict, Depends(get_current_user)]):
    return await get_live_status(user)


@api.get("/earnings/history")
async def earnings_history(user: Annotated[dict, Depends(get_current_user)], days: int = 30):
    cursor = db.daily_stats.find({"user_id": user['id']}, {"_id": 0}).sort("date", -1).limit(days)
    items = await cursor.to_list(length=days)
    return {"items": items}


# ============================================================
# SETTINGS
# ============================================================
@api.get("/settings")
async def get_settings(user: Annotated[dict, Depends(get_current_user)]):
    return {
        "settings": user.get('settings', {}),
        "language": user.get('language', 'de'),
        "payout_method": user.get('payout_method'),
        "payout_address": user.get('payout_address'),
    }


@api.put("/settings")
async def update_settings(payload: SettingsUpdate, user: Annotated[dict, Depends(get_current_user)]):
    update = {}
    settings = user.get('settings', {})
    if payload.intensity in ("light", "normal", "strong"):
        settings['intensity'] = payload.intensity
    if payload.battery_limit is not None and 0 <= payload.battery_limit <= 100:
        settings['battery_limit'] = payload.battery_limit
    if payload.temp_limit is not None and 30 <= payload.temp_limit <= 60:
        settings['temp_limit'] = payload.temp_limit
    if payload.pause_on_gaming is not None:
        settings['pause_on_gaming'] = payload.pause_on_gaming
    update['settings'] = settings
    if payload.language in ("de", "en"):
        update['language'] = payload.language
    if payload.payout_method in ("paypal", "bank", "crypto"):
        update['payout_method'] = payload.payout_method
    if payload.payout_address is not None:
        update['payout_address'] = payload.payout_address
    await db.users.update_one({"id": user['id']}, {"$set": update})
    return {"ok": True, "settings": settings}


# ============================================================
# PAYOUTS
# ============================================================
@api.post("/payout/request")
async def request_payout(payload: PayoutRequest, user: Annotated[dict, Depends(get_current_user)]):
    if payload.amount < 5.0:
        raise HTTPException(status_code=400, detail="Minimum payout is 5€")
    if payload.method not in ("paypal", "bank", "crypto"):
        raise HTTPException(status_code=400, detail="Invalid payout method")
    if not payload.address.strip():
        raise HTTPException(status_code=400, detail="Address required")

    # Settle live session first
    fresh = await db.users.find_one({"id": user['id']}, {"_id": 0})
    if fresh.get('is_sharing'):
        fresh = await settle_session(fresh)

    if fresh.get('balance', 0.0) < payload.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    payout_id = str(uuid.uuid4())
    payout = {
        "id": payout_id,
        "user_id": user['id'],
        "amount": payload.amount,
        "method": payload.method,
        "address": payload.address,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.payouts.insert_one(payout)
    await db.users.update_one(
        {"id": user['id']},
        {"$inc": {"balance": -payload.amount}},
    )
    payout.pop('_id', None)
    return payout


@api.get("/payout/history")
async def payout_history(user: Annotated[dict, Depends(get_current_user)]):
    cursor = db.payouts.find({"user_id": user['id']}, {"_id": 0}).sort("created_at", -1).limit(50)
    items = await cursor.to_list(length=50)
    return {"items": items}


# ============================================================
# REFERRAL
# ============================================================
@api.get("/referral")
async def referral_info(user: Annotated[dict, Depends(get_current_user)]):
    count = await db.users.count_documents({"referred_by": user['referral_code']})
    return {
        "code": user.get('referral_code'),
        "referred_count": count,
        "bonus_per_referral": 1.50,  # 1.50€ per referral
        "total_bonus": round(count * 1.50, 2),
    }


# ============================================================
# PREMIUM (mock upgrade)
# ============================================================
@api.post("/premium/upgrade")
async def upgrade_premium(user: Annotated[dict, Depends(get_current_user)]):
    # MOCKED upgrade flow (no real Stripe yet)
    await db.users.update_one(
        {"id": user['id']},
        {"$set": {"is_premium": True, "premium_since": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True, "is_premium": True}


# ============================================================
# REWARDED ADS (User earns by watching opt-in video ads)
# ============================================================
class RewardClaim(BaseModel):
    ad_unit: str = "rewarded_video"
    network: str = "admob"


@api.post("/earnings/reward")
async def claim_reward(payload: RewardClaim, user: Annotated[dict, Depends(get_current_user)]):
    """Credit the user with REWARD_PER_AD_EUR for watching a rewarded video ad.
    Server-side rate limited to REWARD_DAILY_CAP per day to prevent abuse."""
    today = datetime.now(timezone.utc).date().isoformat()
    stat = await db.daily_stats.find_one({"user_id": user['id'], "date": today}, {"_id": 0}) or {}
    today_rewards = stat.get('ad_rewards', 0.0)
    if today_rewards >= REWARD_DAILY_CAP:
        raise HTTPException(status_code=429, detail="Daily reward cap reached. Come back tomorrow!")

    amount = REWARD_PER_AD_EUR
    await db.daily_stats.update_one(
        {"user_id": user['id'], "date": today},
        {
            "$inc": {"earned": amount, "ad_rewards": amount, "ads_watched": 1},
            "$setOnInsert": {"user_id": user['id'], "date": today},
        },
        upsert=True,
    )
    await db.users.update_one(
        {"id": user['id']},
        {"$inc": {"balance": amount, "total_earned": amount}},
    )
    await db.ad_events.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user['id'],
        "amount": amount,
        "network": payload.network,
        "ad_unit": payload.ad_unit,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    fresh = await db.users.find_one({"id": user['id']}, {"_id": 0})
    return {
        "ok": True,
        "credited": amount,
        "today_reward_total": round(today_rewards + amount, 4),
        "daily_cap": REWARD_DAILY_CAP,
        "balance": round(fresh.get('balance', 0.0), 4),
    }


@api.get("/earnings/reward/status")
async def reward_status(user: Annotated[dict, Depends(get_current_user)]):
    today = datetime.now(timezone.utc).date().isoformat()
    stat = await db.daily_stats.find_one({"user_id": user['id'], "date": today}, {"_id": 0}) or {}
    return {
        "today_rewards": round(stat.get('ad_rewards', 0.0), 4),
        "ads_watched_today": stat.get('ads_watched', 0),
        "daily_cap": REWARD_DAILY_CAP,
        "reward_per_ad": REWARD_PER_AD_EUR,
        "remaining_today": round(max(0.0, REWARD_DAILY_CAP - stat.get('ad_rewards', 0.0)), 4),
    }


@api.get("/premium/info")
async def premium_info():
    return {
        "price_eur": PREMIUM_PRICE_EUR,
        "max_devices_premium": PREMIUM_MAX_DEVICES,
        "max_devices_free": FREE_MAX_DEVICES,
        "platform_fee_free_pct": int(PLATFORM_FEE_FREE * 100),
        "platform_fee_premium_pct": int(PLATFORM_FEE_PREMIUM * 100),
        "benefits": [
            "ad_free",
            "multi_device_up_to_5",
            "priority_payout",
            "lower_platform_fee",
            "detailed_statistics",
        ],
    }


# ============================================================
# ADMIN
# ============================================================
@api.get("/admin/overview")
async def admin_overview(admin: Annotated[dict, Depends(require_admin)]):
    total_users = await db.users.count_documents({"role": "user"})
    active_users = await db.users.count_documents({"is_sharing": True})
    premium_users = await db.users.count_documents({"is_premium": True, "role": "user"})

    # Aggregate platform earnings (commission)
    pipeline = [
        {"$group": {"_id": None, "total_fee": {"$sum": "$total_fee"}, "total_gross": {"$sum": "$total_gross"}}}
    ]
    agg = await db.platform_earnings.aggregate(pipeline).to_list(length=1)
    total_fee = round(agg[0]['total_fee'], 4) if agg else 0.0
    total_gross = round(agg[0]['total_gross'], 4) if agg else 0.0

    # Today
    today = datetime.now(timezone.utc).date().isoformat()
    today_doc = await db.platform_earnings.find_one({"date": today}, {"_id": 0}) or {}

    # Total user earnings paid out
    payouts_agg = await db.payouts.aggregate([
        {"$group": {"_id": "$status", "total": {"$sum": "$amount"}, "count": {"$sum": 1}}}
    ]).to_list(length=10)

    return {
        "total_users": total_users,
        "active_users_now": active_users,
        "premium_users": premium_users,
        "platform_commission_total": total_fee,
        "platform_commission_today": round(today_doc.get('total_fee', 0.0), 4),
        "gross_traffic_total": total_gross,
        "payouts_by_status": payouts_agg,
    }


@api.get("/admin/users")
async def admin_users(admin: Annotated[dict, Depends(require_admin)], limit: int = 100):
    cursor = db.users.find({"role": "user"}, {"_id": 0, "password_hash": 0}).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    return {"items": items}


# ============================================================
# ROOT
# ============================================================
@api.get("/")
async def root():
    return {"app": "NetShare Pro", "version": "1.0.0", "status": "ok"}


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
