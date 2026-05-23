import os
import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr

from jose import JWTError, jwt
from passlib.hash import pbkdf2_sha256

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-please")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 h

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_sharing = Column(Boolean, default=False)
    sharing_started_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
# Tabellen beim Import erstellen (funktioniert überall: Render, Termux, Tests)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="MyApp Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> UserDB:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token ungültig oder abgelaufen",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if sub is None:
            raise credentials_exception
        user_id = int(sub)
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(UserDB).filter(UserDB.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str
    password: str
    username: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------
@app.post("/api/auth/register", response_model=TokenResponse)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(UserDB).filter(UserDB.email == req.email).first():
        raise HTTPException(status_code=400, detail="Email bereits registriert")
    if db.query(UserDB).filter(UserDB.username == req.username).first():
        raise HTTPException(status_code=400, detail="Username bereits vergeben")

    user = UserDB(
        email=req.email,
        username=req.username,
        hashed_password=pbkdf2_sha256.hash(req.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(access_token=token)


@app.post("/api/auth/login", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.email == form.username).first()
    if not user or not pbkdf2_sha256.verify(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Falsche Zugangsdaten")

    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(access_token=token)


# ---------------------------------------------------------------------------
# Routes – Dashboard (JWT geschützt)
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard(user: UserDB = Depends(get_current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "is_sharing": user.is_sharing,
        "sharing_started_at": user.sharing_started_at.isoformat() if user.sharing_started_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# ---------------------------------------------------------------------------
# Routes – Sharing
# ---------------------------------------------------------------------------
@app.post("/api/sharing/start")
def sharing_start(user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.is_sharing:
        raise HTTPException(status_code=400, detail="Sharing läuft bereits")
    user.is_sharing = True
    user.sharing_started_at = datetime.datetime.utcnow()
    db.commit()
    return {"status": "sharing_started", "started_at": user.sharing_started_at.isoformat()}


@app.post("/api/sharing/stop")
def sharing_stop(user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.is_sharing:
        raise HTTPException(status_code=400, detail="Sharing ist nicht aktiv")
    user.is_sharing = False
    user.sharing_started_at = None
    db.commit()
    return {"status": "sharing_stopped"}


# ---------------------------------------------------------------------------
# Health Check (Render braucht das)
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}
