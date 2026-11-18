"""Socle commun aux services : base de données, authentification, santé.

Ce fichier est volontairement identique dans users/, products/, orders/,
cart/ et notifications/ : chaque service reste un dépôt autonome, sans
bibliothèque partagée à publier.
"""
import logging
import os
import time
from datetime import datetime, timezone

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("service")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variable d'environnement {name} manquante")
    return value


DATABASE_URL = _required("DATABASE_URL")
JWT_SECRET = _required("JWT_SECRET")          # partagé : chaque service vérifie les jetons seul
INTERNAL_TOKEN = _required("INTERNAL_TOKEN")  # appels de service à service

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite renvoie des dates sans fuseau : on les considère en UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def init_db(retries: int = 30, delay: float = 2.0) -> None:
    """Crée les tables, en patientant si la base n'est pas encore prête."""
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except Exception as exc:  # base pas encore joignable
            log.warning("Base indisponible (%s/%s) : %s", attempt, retries, exc)
            time.sleep(delay)
    raise RuntimeError("Base de données injoignable")


def add_missing_columns(table: str, columns: dict[str, str]) -> None:
    """Ajoute les colonnes absentes d'une table créée par une ancienne version."""
    existing = {c["name"] for c in inspect(engine).get_columns(table)}
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                log.info("Migration : ajout de %s.%s", table, name)
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """Vérifie le jeton localement : aucun appel au service users n'est nécessaire."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise")
    try:
        claims = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Session expirée ou invalide")
    return {"id": int(claims["sub"]), "name": claims.get("name", ""),
            "email": claims.get("email", ""), "role": claims.get("role", "customer")}


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs")
    return user


def internal_only(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Appel interne uniquement")


def install_common(app: FastAPI, name: str) -> None:
    """Ajoute /live (processus vivant), /health (base joignable), /metrics, et les traces."""
    from tracing import setup_tracing
    setup_tracing(app, name, engine)

    @app.get("/live", include_in_schema=False)
    def live():
        return {"service": name, "status": "ok"}

    @app.get("/health", include_in_schema=False)
    def health():
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"service": name, "status": "down", "database": "down"}, 503)
        return {"service": name, "status": "ok", "database": "ok"}

    try:
        from prometheus_fastapi_instrumentator import Instrumentator
        Instrumentator().instrument(app).expose(app, include_in_schema=False)
    except ImportError:
        pass
