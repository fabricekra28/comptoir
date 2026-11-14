"""Service Products (port 8002) : catalogue, stock et réservations de stock."""
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, Numeric, String, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common import (Base, SessionLocal, add_missing_columns, admin_user, get_db, init_db,
                    install_common, internal_only, log)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    price = Column(Numeric(12, 2), nullable=False, default=0)
    stock = Column(Integer, nullable=False, default=0, server_default="0")
    category = Column(String, nullable=True, index=True)
    image_url = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class StockReservation(Base):
    """Trace de chaque réservation, pour que réserver et libérer soient rejouables sans risque."""
    __tablename__ = "stock_reservations"
    ref = Column(String, primary_key=True)
    items = Column(JSON, nullable=False)
    released = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


DEMO_PRODUCTS = [
    ("Cafetière à piston 1 L", "Verre borosilicate et inox, pour huit tasses.", 34.90, 25, "Cuisine"),
    ("Planche à découper en hêtre", "Bois massif, 40 × 28 cm, rigole à jus.", 27.50, 40, "Cuisine"),
    ("Lot de 4 bols en grès", "Émail mat, passent au lave-vaisselle.", 42.00, 18, "Cuisine"),
    ("Lampe de bureau articulée", "LED 8 W, trois températures de lumière.", 59.00, 12, "Maison"),
    ("Plaid en laine 130 × 180", "Laine mérinos, tissage chevron.", 79.00, 9, "Maison"),
    ("Bougie cire de soja", "Figue et cèdre, 45 heures de combustion.", 19.90, 60, "Maison"),
    ("Carnet pointillé A5", "192 pages, papier 100 g, reliure cousue.", 14.50, 80, "Papeterie"),
    ("Stylo plume laiton", "Plume moyenne, livré avec convertisseur.", 48.00, 15, "Papeterie"),
    ("Sac à dos 20 L", "Toile enduite, compartiment ordinateur 15 pouces.", 89.00, 14, "Bagagerie"),
    ("Gourde isotherme 750 ml", "Inox double paroi, 24 h au frais.", 29.90, 3, "Bagagerie"),
    ("Enceinte Bluetooth compacte", "12 heures d'autonomie, résiste aux éclaboussures.", 69.00, 20, "Audio"),
    ("Casque filaire studio", "Fermé, câble détachable de 3 m.", 119.00, 0, "Audio"),
]


def seed_demo() -> None:
    if os.getenv("SEED_DEMO", "false").lower() != "true":
        return
    with SessionLocal() as db:
        if db.query(Product).first():
            return
        db.add_all(Product(name=n, description=d, price=p, stock=s, category=c)
                   for n, d, p, s, c in DEMO_PRODUCTS)
        db.commit()
        log.info("Catalogue de démonstration créé (%s produits)", len(DEMO_PRODUCTS))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    add_missing_columns("products", {
        "stock": "INTEGER NOT NULL DEFAULT 0", "category": "VARCHAR", "image_url": "VARCHAR",
        "is_active": "BOOLEAN NOT NULL DEFAULT true",
        "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"})
    seed_demo()
    yield


app = FastAPI(title="Products Service", lifespan=lifespan)
install_common(app, "products")


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    price: float = Field(ge=0, le=10_000_000)
    stock: int = Field(default=0, ge=0, le=1_000_000)
    category: str | None = Field(default=None, max_length=60)
    image_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    is_active: bool = True


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None
    price: float
    stock: int
    category: str | None = None
    image_url: str | None = None
    is_active: bool
    created_at: datetime | None = None


# ---------- Catalogue (public) ----------
@app.get("/products", response_model=list[ProductOut])
def list_products(q: str | None = None, category: str | None = None,
                  ids: str | None = Query(default=None, description="ex. 1,2,3"),
                  include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(Product)
    if not include_inactive:
        query = query.filter(Product.is_active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Product.name.ilike(like), Product.description.ilike(like)))
    if category:
        query = query.filter(Product.category == category)
    if ids:
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        query = query.filter(Product.id.in_(wanted))
    return query.order_by(Product.name).limit(500).all()


@app.get("/categories", response_model=list[str])
def list_categories(db: Session = Depends(get_db)):
    rows = (db.query(Product.category).filter(Product.is_active.is_(True),
            Product.category.isnot(None), Product.category != "").distinct().all())
    return sorted(r[0] for r in rows)


@app.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produit introuvable")
    return product


# ---------- Gestion (administrateurs) ----------
def _apply(product: Product, body: ProductIn) -> None:
    for field, value in body.model_dump().items():
        setattr(product, field, value.strip() if isinstance(value, str) else value)
    product.category = product.category or None
    product.image_url = product.image_url or None


@app.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: ProductIn, _: dict = Depends(admin_user), db: Session = Depends(get_db)):
    product = Product()
    _apply(product, body)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.put("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, body: ProductIn, _: dict = Depends(admin_user),
                   db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produit introuvable")
    _apply(product, body)
    db.commit()
    db.refresh(product)
    return product


@app.delete("/products/{product_id}")
def delete_product(product_id: int, _: dict = Depends(admin_user), db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Produit introuvable")
    db.delete(product)
    db.commit()
    return {"message": "Produit supprimé"}


# ---------- Stock (appels internes du service orders) ----------
class ReserveLine(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=999)


class ReserveIn(BaseModel):
    ref: str = Field(min_length=8, max_length=64)
    items: list[ReserveLine] = Field(min_length=1, max_length=100)


class ReleaseIn(BaseModel):
    ref: str = Field(min_length=8, max_length=64)


@app.post("/internal/stock/reserve", dependencies=[Depends(internal_only)])
def reserve_stock(body: ReserveIn, db: Session = Depends(get_db)):
    """Vérifie et décrémente le stock en une seule transaction. Rejouable : même ref, même réponse."""
    existing = db.get(StockReservation, body.ref)
    if existing:
        if existing.released:
            raise HTTPException(409, {"message": "Cette tentative a expiré. Recommencez la commande.",
                                      "problems": []})
        return {"ref": body.ref, "items": existing.items}

    wanted: dict[int, int] = {}
    for line in body.items:
        wanted[line.product_id] = wanted.get(line.product_id, 0) + line.quantity
    # Verrouillage dans l'ordre des identifiants pour éviter les interblocages.
    products = (db.query(Product).filter(Product.id.in_(wanted)).order_by(Product.id)
                .with_for_update().all())
    by_id = {p.id: p for p in products}
    problems = []
    for pid, qty in wanted.items():
        p = by_id.get(pid)
        if not p or not p.is_active:
            problems.append({"product_id": pid, "name": p.name if p else f"Produit n° {pid}",
                             "available": 0})
        elif p.stock < qty:
            problems.append({"product_id": pid, "name": p.name, "available": p.stock})
    if problems:
        db.rollback()
        raise HTTPException(409, {"message": "Certains articles ne sont plus disponibles "
                                             "dans la quantité demandée.", "problems": problems})
    items = []
    for pid, qty in wanted.items():
        p = by_id[pid]
        p.stock -= qty
        items.append({"product_id": pid, "name": p.name, "unit_price": float(p.price),
                      "quantity": qty})
    db.add(StockReservation(ref=body.ref, items=items))
    try:
        db.commit()
    except IntegrityError:  # même ref réservée au même instant par une requête jumelle
        db.rollback()
        twin = db.get(StockReservation, body.ref)
        if twin and not twin.released:
            return {"ref": body.ref, "items": twin.items}
        raise HTTPException(409, {"message": "Cette tentative a expiré. Recommencez la commande.",
                                  "problems": []})
    return {"ref": body.ref, "items": items}


@app.post("/internal/stock/release", dependencies=[Depends(internal_only)])
def release_stock(body: ReleaseIn, db: Session = Depends(get_db)):
    """Remet le stock en rayon (commande annulée ou échouée). Rejouable sans double remise."""
    reservation = (db.query(StockReservation).filter(StockReservation.ref == body.ref)
                   .with_for_update().first())
    if not reservation:
        # Libération arrivée sans réservation : on la retient pour refuser une réservation tardive.
        db.add(StockReservation(ref=body.ref, items=[], released=True))
        db.commit()
        return {"ref": body.ref, "released": True}
    if not reservation.released:
        ids = [i["product_id"] for i in reservation.items]
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(ids))
                    .order_by(Product.id).with_for_update().all()}
        for item in reservation.items:
            if item["product_id"] in products:  # produit supprimé entre-temps : rien à remettre
                products[item["product_id"]].stock += item["quantity"]
        reservation.released = True
        db.commit()
    return {"ref": body.ref, "released": True}
