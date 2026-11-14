"""Service Orders (port 8003) : commandes et suivi.

Deux choix rendent ce service robuste aux pannes des autres :
- chaque commande garde une copie du nom et du prix des articles, donc
  l'historique reste lisible quand le catalogue est en panne ;
- les messages vers les autres services (notification, remise en stock)
  passent par une boîte d'envoi en base, rejouée jusqu'à ce qu'ils arrivent.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String,
                        func)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, relationship

from common import (Base, INTERNAL_TOKEN, SessionLocal, admin_user, current_user, get_db,
                    init_db, install_common, log, utcnow)

TARGETS = {
    "products": os.getenv("PRODUCTS_SERVICE_URL", "http://products-service:8002"),
    "notifications": os.getenv("NOTIFICATIONS_SERVICE_URL", "http://notifications-service:8005"),
}
INTERNAL_HEADERS = {"X-Internal-Token": INTERNAL_TOKEN}
STATUS_LABELS = {"confirmed": "confirmée", "shipped": "expédiée", "delivered": "livrée",
                 "cancelled": "annulée"}
TRANSITIONS = {"confirmed": {"shipped", "cancelled"}, "shipped": {"delivered", "cancelled"},
               "delivered": set(), "cancelled": set()}
OUTBOX_MAX_ATTEMPTS = 200


class Order(Base):
    __tablename__ = "customer_orders"  # l'ancienne table « orders » (v1) n'est pas touchée
    id = Column(Integer, primary_key=True, index=True)
    ref = Column(String, unique=True, nullable=False)  # clé d'idempotence et de réservation
    user_id = Column(Integer, nullable=False, index=True)
    user_name = Column(String, nullable=False)
    user_email = Column(String, nullable=False)
    status = Column(String, nullable=False, default="confirmed", index=True)
    total = Column(Numeric(12, 2), nullable=False)
    shipping_name = Column(String, nullable=False)
    shipping_phone = Column(String, nullable=False)
    shipping_address = Column(String, nullable=False)
    shipping_city = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    items = relationship("OrderItem", cascade="all, delete-orphan", lazy="selectin")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("customer_orders.id"), nullable=False, index=True)
    product_id = Column(Integer, nullable=False)
    product_name = Column(String, nullable=False)   # copie figée au moment de l'achat
    unit_price = Column(Numeric(12, 2), nullable=False)
    quantity = Column(Integer, nullable=False)


class Outbox(Base):
    __tablename__ = "outbox"
    id = Column(Integer, primary_key=True)
    target = Column(String, nullable=False)
    path = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    done = Column(Boolean, nullable=False, default=False, index=True)
    next_try = Column(DateTime(timezone=True), nullable=False, default=utcnow)


def enqueue(db: Session, target: str, path: str, payload: dict) -> None:
    db.add(Outbox(target=target, path=path, payload=payload))


def notify(db: Session, order: Order, title: str, body: str, event: str) -> None:
    enqueue(db, "notifications", "/internal/notifications", {
        "user_id": order.user_id, "title": title, "body": body,
        "link": f"/commandes/{order.id}", "dedup_key": f"order-{order.id}-{event}"})


def process_outbox() -> int:
    """Envoie les messages en attente. Un échec repousse le prochain essai (5 s, 10 s… 5 min max)."""
    sent = 0
    with SessionLocal() as db:
        events = (db.query(Outbox)
                  .filter(Outbox.done.is_(False), Outbox.next_try <= utcnow(),
                          Outbox.attempts < OUTBOX_MAX_ATTEMPTS)
                  .order_by(Outbox.id).limit(20).with_for_update(skip_locked=True).all())
        for event in events:
            try:
                r = httpx.post(TARGETS[event.target] + event.path, json=event.payload,
                               headers=INTERNAL_HEADERS, timeout=5)
                delivered = r.status_code < 500
                if 400 <= r.status_code < 500:
                    log.error("Message %s refusé (%s) : abandonné", event.id, r.status_code)
            except httpx.HTTPError:
                delivered = False
            if delivered:
                event.done = True
                sent += 1
            else:
                event.attempts += 1
                event.next_try = utcnow() + timedelta(seconds=min(300, 5 * 2 ** min(event.attempts, 6)))
        db.commit()
    return sent


async def outbox_worker() -> None:
    while True:
        try:
            await asyncio.to_thread(process_outbox)
        except Exception as exc:
            log.warning("Boîte d'envoi : %s", exc)
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker = asyncio.create_task(outbox_worker())
    yield
    worker.cancel()


app = FastAPI(title="Orders Service", lifespan=lifespan)
install_common(app, "orders")


# ---------- Schémas ----------
class LineIn(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=999)


class OrderIn(BaseModel):
    ref: str = Field(min_length=16, max_length=64)
    items: list[LineIn] = Field(min_length=1, max_length=100)
    shipping_name: str = Field(min_length=2, max_length=80)
    shipping_phone: str = Field(min_length=6, max_length=30)
    shipping_address: str = Field(min_length=4, max_length=200)
    shipping_city: str = Field(min_length=2, max_length=80)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(shipped|delivered|cancelled)$")


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int
    product_name: str
    unit_price: float
    quantity: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    user_name: str
    user_email: str
    status: str
    total: float
    shipping_name: str
    shipping_phone: str
    shipping_address: str
    shipping_city: str
    created_at: datetime
    updated_at: datetime | None = None
    items: list[ItemOut]


def _release_now(ref: str) -> None:
    try:
        httpx.post(TARGETS["products"] + "/internal/stock/release", json={"ref": ref},
                   headers=INTERNAL_HEADERS, timeout=3)
    except httpx.HTTPError:
        log.error("Stock de la réservation %s à libérer manuellement", ref)


# ---------- Côté client ----------
@app.post("/orders", response_model=OrderOut, status_code=201)
def create_order(body: OrderIn, user: dict = Depends(current_user), db: Session = Depends(get_db)):
    def existing():
        return db.query(Order).filter(Order.ref == body.ref, Order.user_id == user["id"]).first()

    if order := existing():  # formulaire envoyé deux fois : on renvoie la même commande
        return order

    unavailable = HTTPException(503, "Le catalogue ne répond pas : impossible de vérifier le stock. "
                                     "Votre panier est conservé, réessayez dans un instant.")
    payload = {"ref": body.ref, "items": [i.model_dump() for i in body.items]}
    try:
        r = httpx.post(TARGETS["products"] + "/internal/stock/reserve", json=payload,
                       headers=INTERNAL_HEADERS, timeout=6)
    except httpx.HTTPError:
        # La réservation a peut-être abouti sans que la réponse revienne : on programme sa libération.
        enqueue(db, "products", "/internal/stock/release", {"ref": body.ref})
        db.commit()
        raise unavailable
    if r.status_code == 409:
        raise HTTPException(409, r.json().get("detail"))
    if r.status_code >= 400:
        if r.status_code >= 500:
            enqueue(db, "products", "/internal/stock/release", {"ref": body.ref})
            db.commit()
        raise unavailable

    lines = r.json()["items"]
    total = round(sum(l["unit_price"] * l["quantity"] for l in lines), 2)
    order = Order(ref=body.ref, user_id=user["id"], user_name=user["name"], user_email=user["email"],
                  total=total, shipping_name=body.shipping_name.strip(),
                  shipping_phone=body.shipping_phone.strip(),
                  shipping_address=body.shipping_address.strip(),
                  shipping_city=body.shipping_city.strip(),
                  items=[OrderItem(product_id=l["product_id"], product_name=l["name"],
                                   unit_price=l["unit_price"], quantity=l["quantity"]) for l in lines])
    db.add(order)
    try:
        db.flush()
        notify(db, order, f"Commande n° {order.id} confirmée",
               "Nous préparons votre colis. Paiement à la livraison.", "confirmed")
        db.commit()
    except IntegrityError:  # requête jumelle arrivée en même temps avec la même ref
        db.rollback()
        if order := existing():
            return order
        raise HTTPException(409, "Cette commande a déjà été enregistrée")
    except Exception:
        db.rollback()
        _release_now(body.ref)
        raise
    db.refresh(order)
    return order


@app.get("/orders", response_model=list[OrderOut])
def my_orders(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    return (db.query(Order).filter(Order.user_id == user["id"])
            .order_by(Order.id.desc()).limit(200).all())


def _visible_order(order_id: int, user: dict, db: Session) -> Order:
    order = db.get(Order, order_id)
    if not order or (order.user_id != user["id"] and user["role"] != "admin"):
        raise HTTPException(404, "Commande introuvable")
    return order


@app.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, user: dict = Depends(current_user), db: Session = Depends(get_db)):
    return _visible_order(order_id, user, db)


def _set_status(db: Session, order: Order, new_status: str) -> Order:
    if new_status not in TRANSITIONS[order.status]:
        raise HTTPException(409, f"Une commande {STATUS_LABELS[order.status]} ne peut pas "
                                 f"passer à « {STATUS_LABELS[new_status]} »")
    order.status = new_status
    if new_status == "cancelled":
        enqueue(db, "products", "/internal/stock/release", {"ref": order.ref})
    notify(db, order, f"Commande n° {order.id} {STATUS_LABELS[new_status]}",
           {"shipped": "Votre colis est en route.", "delivered": "Votre colis a été remis. Merci !",
            "cancelled": "La commande est annulée, les articles sont remis en stock."}[new_status],
           new_status)
    db.commit()
    db.refresh(order)
    return order


@app.post("/orders/{order_id}/cancel", response_model=OrderOut)
def cancel_order(order_id: int, user: dict = Depends(current_user), db: Session = Depends(get_db)):
    order = _visible_order(order_id, user, db)
    if order.status != "confirmed" and user["role"] != "admin":
        raise HTTPException(409, "Cette commande est déjà partie : elle ne peut plus être annulée")
    return _set_status(db, order, "cancelled")


# ---------- Administration ----------
@app.get("/admin/orders", response_model=list[OrderOut])
def all_orders(status: str | None = None, _: dict = Depends(admin_user),
               db: Session = Depends(get_db)):
    query = db.query(Order)
    if status:
        query = query.filter(Order.status == status)
    return query.order_by(Order.id.desc()).limit(500).all()


@app.put("/admin/orders/{order_id}/status", response_model=OrderOut)
def update_status(order_id: int, body: StatusIn, _: dict = Depends(admin_user),
                  db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Commande introuvable")
    return _set_status(db, order, body.status)


@app.get("/admin/stats")
def stats(_: dict = Depends(admin_user), db: Session = Depends(get_db)):
    by_status = dict(db.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    revenue = (db.query(func.coalesce(func.sum(Order.total), 0))
               .filter(Order.status != "cancelled").scalar())
    pending_messages = db.query(func.count(Outbox.id)).filter(Outbox.done.is_(False)).scalar()
    return {"by_status": by_status, "orders": sum(by_status.values()),
            "revenue": float(revenue), "pending_messages": pending_messages}
