"""Service Users (port 8001) : comptes, connexion, rôles.

Le service émet des jetons signés (JWT). Les autres services les vérifient
eux-mêmes : une personne déjà connectée continue donc d'utiliser la boutique
même si ce service tombe.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common import (Base, JWT_SECRET, SessionLocal, add_missing_columns, admin_user, aware,
                    current_user, get_db, init_db, install_common, internal_only, log, utcnow)

TOKEN_HOURS = int(os.getenv("TOKEN_HOURS", "8"))
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 10


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=True)  # vide pour les comptes d'avant la v2
    role = Column(String, nullable=False, default="customer", server_default="customer")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    city = Column(String, nullable=True)
    failed_logins = Column(Integer, nullable=False, default=0, server_default="0")
    locked_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def make_token(user: User) -> str:
    now = utcnow()
    return jwt.encode({"sub": str(user.id), "name": user.name, "email": user.email,
                       "role": user.role, "iat": now, "exp": now + timedelta(hours=TOKEN_HOURS)},
                      JWT_SECRET, algorithm="HS256")


def seed_admin() -> None:
    """Crée le premier administrateur à partir de ADMIN_EMAIL / ADMIN_PASSWORD."""
    email, password = os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PASSWORD")
    if not email or not password:
        return
    with SessionLocal() as db:
        if db.query(User).filter(User.role == "admin").first():
            return
        user = db.query(User).filter(User.email == email.lower()).first()
        if user:
            user.role, user.password_hash = "admin", hash_password(password)
        else:
            db.add(User(name=os.getenv("ADMIN_NAME", "Administrateur"), email=email.lower(),
                        password_hash=hash_password(password), role="admin"))
        try:
            db.commit()
        except IntegrityError:  # un autre réplica vient de le créer
            db.rollback()
            return
        log.info("Compte administrateur créé : %s", email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    add_missing_columns("users", {
        "password_hash": "VARCHAR", "role": "VARCHAR NOT NULL DEFAULT 'customer'",
        "is_active": "BOOLEAN NOT NULL DEFAULT true", "phone": "VARCHAR", "address": "VARCHAR",
        "city": "VARCHAR", "failed_logins": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TIMESTAMP WITH TIME ZONE",
        "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"})
    seed_admin()
    yield


app = FastAPI(title="Users Service", lifespan=lifespan)
install_common(app, "users")


# ---------- Schémas ----------
class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class LoginIn(BaseModel):
    email: str
    password: str


class ProfileIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


class AdminUserIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(customer|admin)$")
    is_active: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    created_at: datetime | None = None


class SessionOut(BaseModel):
    access_token: str
    user: UserOut


# ---------- Inscription et connexion ----------
@app.post("/auth/register", response_model=SessionOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.lower()
    user = db.query(User).filter(User.email == email).first()
    if user and user.password_hash:
        raise HTTPException(409, "Un compte existe déjà avec cette adresse e-mail")
    if user:  # compte créé avant la v2, sans mot de passe : la personne le récupère
        user.name, user.password_hash = body.name.strip(), hash_password(body.password)
    else:
        user = User(name=body.name.strip(), email=email, password_hash=hash_password(body.password))
        db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Un compte existe déjà avec cette adresse e-mail")
    db.refresh(user)
    return {"access_token": make_token(user), "user": user}


@app.post("/auth/login", response_model=SessionOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user:
        raise HTTPException(401, "E-mail ou mot de passe incorrect")
    locked = aware(user.locked_until)
    if locked and locked > utcnow():
        raise HTTPException(429, f"Trop de tentatives. Réessayez dans {LOCK_MINUTES} minutes.")
    if not check_password(body.password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until, user.failed_logins = utcnow() + timedelta(minutes=LOCK_MINUTES), 0
        db.commit()
        raise HTTPException(401, "E-mail ou mot de passe incorrect")
    if not user.is_active:
        raise HTTPException(403, "Ce compte a été désactivé")
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    db.refresh(user)
    return {"access_token": make_token(user), "user": user}


# ---------- Mon compte ----------
def _me(db: Session, claims: dict) -> User:
    user = db.get(User, claims["id"])
    if not user or not user.is_active:
        raise HTTPException(401, "Compte introuvable ou désactivé")
    return user


@app.get("/users/me", response_model=UserOut)
def get_me(claims: dict = Depends(current_user), db: Session = Depends(get_db)):
    return _me(db, claims)


@app.put("/users/me", response_model=SessionOut)
def update_me(body: ProfileIn, claims: dict = Depends(current_user), db: Session = Depends(get_db)):
    user = _me(db, claims)
    user.name, user.phone = body.name.strip(), body.phone
    user.address, user.city = body.address, body.city
    db.commit()
    db.refresh(user)
    return {"access_token": make_token(user), "user": user}  # nouveau jeton : le nom a pu changer


@app.post("/users/me/password")
def change_password(body: PasswordIn, claims: dict = Depends(current_user),
                    db: Session = Depends(get_db)):
    user = _me(db, claims)
    if not check_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Le mot de passe actuel est incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"message": "Mot de passe modifié"}


# ---------- Administration ----------
@app.get("/users", response_model=list[UserOut])
def list_users(_: dict = Depends(admin_user), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id).all()


@app.put("/users/{user_id}", response_model=UserOut)
def admin_update_user(user_id: int, body: AdminUserIn, admin: dict = Depends(admin_user),
                      db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    if user.id == admin["id"]:
        raise HTTPException(400, "Vous ne pouvez pas modifier votre propre rôle ou statut")
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    db.commit()
    db.refresh(user)
    return user


@app.get("/internal/users/{user_id}", response_model=UserOut, dependencies=[Depends(internal_only)])
def internal_get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Utilisateur introuvable")
    return user
