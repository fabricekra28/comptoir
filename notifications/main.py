"""Service Notifications (port 8005) : messages affichés dans l'espace client.

Service secondaire par nature : s'il tombe, les commandes continuent et les
messages en attente (boîte d'envoi du service orders) arrivent à son retour.
"""
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common import Base, current_user, get_db, init_db, install_common, internal_only


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=False)
    body = Column(String, nullable=False, default="")
    link = Column(String, nullable=True)
    dedup_key = Column(String, unique=True, nullable=True)  # un même événement n'est affiché qu'une fois
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Notifications Service", lifespan=lifespan)
install_common(app, "notifications")


class NotificationIn(BaseModel):
    user_id: int
    title: str = Field(min_length=1, max_length=140)
    body: str = Field(default="", max_length=500)
    link: str | None = Field(default=None, max_length=200, pattern="^/")
    dedup_key: str | None = Field(default=None, max_length=100)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    body: str
    link: str | None = None
    is_read: bool
    created_at: datetime


@app.post("/internal/notifications", status_code=201, dependencies=[Depends(internal_only)])
def create_notification(body: NotificationIn, db: Session = Depends(get_db)):
    db.add(Notification(**body.model_dump()))
    try:
        db.commit()
    except IntegrityError:  # déjà reçue (message rejoué) : rien à faire
        db.rollback()
    return {"status": "ok"}


@app.get("/notifications", response_model=list[NotificationOut])
def list_notifications(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    return (db.query(Notification).filter(Notification.user_id == user["id"])
            .order_by(Notification.id.desc()).limit(100).all())


@app.get("/notifications/unread-count")
def unread_count(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    count = (db.query(func.count(Notification.id))
             .filter(Notification.user_id == user["id"], Notification.is_read.is_(False)).scalar())
    return {"count": count}


@app.post("/notifications/read-all")
def read_all(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    (db.query(Notification).filter(Notification.user_id == user["id"],
                                   Notification.is_read.is_(False)).update({"is_read": True}))
    db.commit()
    return {"count": 0}
