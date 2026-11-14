"""Service Cart (port 8004) : panier enregistré par compte, retrouvé d'un appareil à l'autre."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common import Base, current_user, get_db, init_db, install_common

MAX_QUANTITY = 99


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    product_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    # Copie du nom et du prix à l'ajout : le panier reste lisible si le catalogue est en panne.
    # Le prix facturé est toujours celui du catalogue au moment de la commande.
    product_name = Column(String, nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Cart Service", lifespan=lifespan)
install_common(app, "cart")


class AddIn(BaseModel):
    product_id: int
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY)
    product_name: str = Field(min_length=1, max_length=120)
    unit_price: float = Field(ge=0)


class QuantityIn(BaseModel):
    quantity: int = Field(ge=0, le=MAX_QUANTITY)


def _cart(db: Session, user_id: int) -> dict:
    items = db.query(CartItem).filter(CartItem.user_id == user_id).order_by(CartItem.id).all()
    return {"count": sum(i.quantity for i in items),
            "items": [{"product_id": i.product_id, "quantity": i.quantity,
                       "product_name": i.product_name, "unit_price": float(i.unit_price)}
                      for i in items]}


def _item(db: Session, user_id: int, product_id: int) -> CartItem | None:
    return (db.query(CartItem).filter(CartItem.user_id == user_id,
                                      CartItem.product_id == product_id).first())


@app.get("/cart")
def get_cart(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    return _cart(db, user["id"])


@app.get("/cart/count")
def cart_count(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    total = (db.query(func.coalesce(func.sum(CartItem.quantity), 0))
             .filter(CartItem.user_id == user["id"]).scalar())
    return {"count": int(total)}


@app.post("/cart/items")
def add_item(body: AddIn, user: dict = Depends(current_user), db: Session = Depends(get_db)):
    for _ in range(2):  # second passage si deux ajouts simultanés créent la même ligne
        item = _item(db, user["id"], body.product_id)
        if item:
            item.quantity = min(MAX_QUANTITY, item.quantity + body.quantity)
            item.product_name, item.unit_price = body.product_name, body.unit_price
        else:
            db.add(CartItem(user_id=user["id"], product_id=body.product_id, quantity=body.quantity,
                            product_name=body.product_name, unit_price=body.unit_price))
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
    return _cart(db, user["id"])


@app.put("/cart/items/{product_id}")
def set_quantity(product_id: int, body: QuantityIn, user: dict = Depends(current_user),
                 db: Session = Depends(get_db)):
    item = _item(db, user["id"], product_id)
    if item:
        if body.quantity == 0:
            db.delete(item)
        else:
            item.quantity = body.quantity
        db.commit()
    return _cart(db, user["id"])


@app.delete("/cart/items/{product_id}")
def remove_item(product_id: int, user: dict = Depends(current_user), db: Session = Depends(get_db)):
    item = _item(db, user["id"], product_id)
    if item:
        db.delete(item)
        db.commit()
    return _cart(db, user["id"])


@app.delete("/cart")
def clear_cart(user: dict = Depends(current_user), db: Session = Depends(get_db)):
    db.query(CartItem).filter(CartItem.user_id == user["id"]).delete()
    db.commit()
    return {"count": 0, "items": []}
