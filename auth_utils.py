from datetime import timedelta, datetime
from uuid import UUID
from typing import Optional
import os

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session

# ajuste esses imports conforme sua estrutura
from database import SessionLocal
from models import User

# =========================
# Config JWT
# =========================
JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_SUPER_SECRET")  # use env var em prod
JWT_ALG = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 dias

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# =========================
# DB Session helper
# =========================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================
# Password hashing
# =========================
# --- Opção A: Passlib (recomendado)
# from passlib.context import CryptContext
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# def hash_password(password: str) -> str:
#     return pwd_context.hash(password)
# def verify_password(plain_password: str, password_hash: str) -> bool:
#     return pwd_context.verify(plain_password, password_hash)

# --- Opção B: bcrypt (se não usa passlib)
import bcrypt
def hash_password(password: str) -> str:
    if isinstance(password, str):
        password = password.encode("utf-8")
    return bcrypt.hashpw(password, bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password: str, password_hash: str) -> bool:
    if isinstance(plain_password, str):
        plain_password = plain_password.encode("utf-8")
    if isinstance(password_hash, str):
        password_hash = password_hash.encode("utf-8")
    try:
        return bcrypt.checkpw(plain_password, password_hash)
    except ValueError:
        return False

# =========================
# Token helpers
# =========================
def create_access_token(user_id, email: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = {
        "sub": str(user_id),   # UUID como string
        "email": email,
        "exp": expire,
        "iat": datetime.utcnow(),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
    return token

# =========================
# Current user
# =========================
def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    credentials_exc = HTTPException(status_code=401, detail="Não autorizado")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except JWTError:
        raise credentials_exc

    sub = payload.get("sub")
    if not sub:
        raise credentials_exc

    user = None
    # Primeiro tenta UUID
    try:
        user_uuid = UUID(str(sub))
        user = db.query(User).filter(User.id == user_uuid).first()
    except ValueError:
        # Compat: se algum token antigo tiver sub numérico
        try:
            user_int = int(str(sub))
            user = db.query(User).filter(User.id == user_int).first()
        except ValueError:
            pass

    if not user:
        raise credentials_exc

    return user
