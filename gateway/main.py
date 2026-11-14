"""Gateway (port 8000) : le site que voient les clients.

Elle assemble chaque page à partir des services. Chaque morceau de page est
indépendant : si un service ne répond pas, seul ce qui dépend de lui est
remplacé par un message, le reste continue de fonctionner.
"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

import jwt
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend import ApiError, Backend, ServiceDown


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variable d'environnement {name} manquante")
    return value


JWT_SECRET = _required("JWT_SECRET")
APP_NAME = os.getenv("APP_NAME", "Comptoir")
CURRENCY = os.getenv("CURRENCY", "€")
CURRENCY_DECIMALS = int(os.getenv("CURRENCY_DECIMALS", "2"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"  # « true » derrière HTTPS
SESSION_SECONDS = int(os.getenv("TOKEN_HOURS", "8")) * 3600
try:
    LOCAL_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "UTC"))
except Exception:
    LOCAL_TZ = timezone.utc

backend = Backend({
    "users": os.getenv("USERS_SERVICE_URL", "http://users-service:8001"),
    "products": os.getenv("PRODUCTS_SERVICE_URL", "http://products-service:8002"),
    "orders": os.getenv("ORDERS_SERVICE_URL", "http://orders-service:8003"),
    "cart": os.getenv("CART_SERVICE_URL", "http://cart-service:8004"),
    "notifications": os.getenv("NOTIFICATIONS_SERVICE_URL", "http://notifications-service:8005"),
}, _required("INTERNAL_TOKEN"))

SERVICE_INFO = {  # nom affiché, ce qui s'arrête quand le service est en panne
    "users": ("Comptes", "Connexion, inscription et modification du profil. "
                         "Les personnes déjà connectées le restent."),
    "products": ("Catalogue", "Le catalogue affiche sa dernière version connue. "
                              "Les nouvelles commandes attendent son retour."),
    "orders": ("Commandes", "Passage de commande et historique. Catalogue et panier restent utilisables."),
    "cart": ("Panier", "Ajout au panier et commande. Le catalogue reste consultable."),
    "notifications": ("Notifications", "Les messages sont mis en attente et arrivent au retour du service."),
}
DOWN_MESSAGES = {
    "users": "Le service des comptes ne répond pas pour le moment. Réessayez dans un instant.",
    "products": "Le catalogue ne répond pas pour le moment. Réessayez dans un instant.",
    "orders": "Le service des commandes ne répond pas pour le moment. Votre panier est conservé.",
    "cart": "Le panier ne répond pas pour le moment. Réessayez dans un instant.",
    "notifications": "Les notifications ne répondent pas pour le moment.",
}
STATUS_LABELS = {"confirmed": "Confirmée", "shipped": "Expédiée", "delivered": "Livrée",
                 "cancelled": "Annulée"}
NEXT_STATUS = {"confirmed": [("shipped", "Marquer expédiée")],
               "shipped": [("delivered", "Marquer livrée")]}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await backend.start()
    yield
    await backend.close()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

try:
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app).expose(app, include_in_schema=False)
except ImportError:
    pass


# ---------- Filtres d'affichage ----------
def money(value) -> str:
    text = f"{float(value or 0):,.{CURRENCY_DECIMALS}f}".replace(",", "\u202f").replace(".", ",")
    return f"{text}\u00a0{CURRENCY}"


def when(value, fmt: str = "%d/%m/%Y à %H:%M") -> str:
    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime(fmt)


def clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, LOCAL_TZ).strftime("%H:%M")


templates.env.filters.update(money=money, when=when, clock=clock,
                             status=lambda s: STATUS_LABELS.get(s, s))
templates.env.globals.update(app_name=APP_NAME)


# ---------- Session ----------
class NeedLogin(Exception):
    pass


class Forbidden(Exception):
    pass


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    request.state.user = request.state.token = None
    token = request.cookies.get("session")
    if token:
        try:  # vérification locale : la session survit à une panne du service users
            claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.state.user = {"id": int(claims["sub"]), "name": claims.get("name", ""),
                                  "email": claims.get("email", ""), "role": claims.get("role")}
            request.state.token = token
        except (jwt.PyJWTError, KeyError, ValueError):
            pass
    if request.method == "POST":  # un autre site ne peut pas soumettre nos formulaires
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Origine refusée"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def need_user(request: Request) -> tuple[dict, str]:
    if not request.state.user:
        raise NeedLogin()
    return request.state.user, request.state.token


def need_admin(request: Request) -> tuple[dict, str]:
    user, token = need_user(request)
    if user["role"] != "admin":
        raise Forbidden()
    return user, token


def set_session(response, token: str) -> None:
    response.set_cookie("session", token, max_age=SESSION_SECONDS, httponly=True,
                        samesite="lax", secure=COOKIE_SECURE, path="/")


def safe_next(target: str | None) -> str:
    """N'accepte qu'un chemin interne, pour ne jamais rediriger vers un autre site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def redirect(url: str, message: str | None = None, level: str = "ok") -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        response.set_cookie("flash", quote(f"{level}|{message}"), max_age=30, httponly=True,
                            samesite="lax", secure=COOKIE_SECURE)
    return response


def back(request: Request, message: str, level: str = "error") -> RedirectResponse:
    referer = urlparse(request.headers.get("referer", ""))
    target = safe_next(referer.path + (f"?{referer.query}" if referer.query else ""))
    return redirect(target, message, level)


async def page(request: Request, template: str, status_code: int = 200, **context):
    """Rend une page. Les compteurs de l'en-tête sont facultatifs : en cas de panne, ils disparaissent."""
    user, token = request.state.user, request.state.token
    cart_count = unread = None
    if user:
        cart, notif = await asyncio.gather(
            backend.optional("cart", "GET", "/cart/count", token=token, timeout=2),
            backend.optional("notifications", "GET", "/notifications/unread-count", token=token,
                             timeout=2))
        cart_count = cart["count"] if cart else None
        unread = notif["count"] if notif else None
    flash = None
    if raw := request.cookies.get("flash"):
        level, _, message = unquote(raw).partition("|")
        flash = {"level": level, "message": message}
    context.update(user=user, cart_count=cart_count, unread=unread, flash=flash,
                   path=request.url.path)
    response = templates.TemplateResponse(request, template, context, status_code=status_code)
    if flash:
        response.delete_cookie("flash")
    return response


# ---------- Pannes et refus : toujours une réponse lisible ----------
@app.exception_handler(NeedLogin)
async def on_need_login(request: Request, exc: NeedLogin):
    target = request.url.path if request.method == "GET" else urlparse(
        request.headers.get("referer", "")).path or "/"
    return redirect(f"/connexion?next={quote(target)}", "Connectez-vous pour continuer.", "info")


@app.exception_handler(Forbidden)
async def on_forbidden(request: Request, exc: Forbidden):
    return await page(request, "error.html", 403, title="Accès réservé",
                      message="Cette page est réservée aux administrateurs.")


@app.exception_handler(ServiceDown)
async def on_service_down(request: Request, exc: ServiceDown):
    message = DOWN_MESSAGES.get(exc.service, "Un service ne répond pas pour le moment.")
    if request.method == "POST":
        return back(request, message)
    return await page(request, "error.html", 503, title="Momentanément indisponible",
                      message=message, show_status=True)


@app.exception_handler(ApiError)
async def on_api_error(request: Request, exc: ApiError):
    if exc.status == 401:
        response = redirect("/connexion", "Votre session a expiré. Reconnectez-vous.", "info")
        response.delete_cookie("session")
        return response
    if request.method == "POST":
        return back(request, exc.message)
    title = "Page introuvable" if exc.status == 404 else "Demande refusée"
    return await page(request, "error.html", exc.status, title=title, message=exc.message)


@app.exception_handler(404)
@app.exception_handler(RequestValidationError)
async def on_not_found(request: Request, exc):
    return await page(request, "error.html", 404, title="Page introuvable",
                      message="Cette page n'existe pas ou a été déplacée.")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}  # ne dépend d'aucun service : la gateway reste « vivante » seule


# ---------- Outils ----------
def to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return default


def decorate(product: dict) -> dict:
    """Ajoute de quoi dessiner la vignette d'un produit sans photo."""
    seed = product.get("category") or product.get("name") or ""
    product["tone"] = sum((i + 1) * ord(ch) for i, ch in enumerate(seed)) % 5
    product["initial"] = (product.get("name") or "?")[0].upper()
    return product


async def catalog(params: dict | None = None):
    """Catalogue avec secours : renvoie (produits, réponse_de_secours?, heure) ou (None, …) si rien."""
    try:
        cached = await backend.cached_get("products", "/products", params or {})
    except (ServiceDown, ApiError):
        return None, False, 0.0
    return [decorate(dict(p)) for p in cached.data], cached.stale, cached.fetched_at


async def categories() -> list[str]:
    try:
        return (await backend.cached_get("products", "/categories", ttl=60)).data
    except (ServiceDown, ApiError):
        return []


# ---------- Boutique (public) ----------
@app.get("/")
async def home(request: Request):
    (products, stale, at), cats = await asyncio.gather(catalog(), categories())
    featured = sorted(products or [], key=lambda p: p["id"], reverse=True)[:8]
    return await page(request, "index.html", products=featured, catalog_down=products is None,
                      stale=stale, stale_at=at, categories=cats)


@app.get("/catalogue")
async def catalogue(request: Request, q: str = "", categorie: str = ""):
    params = {k: v for k, v in (("q", q.strip()), ("category", categorie)) if v}
    (products, stale, at), cats = await asyncio.gather(catalog(params), categories())
    return await page(request, "catalog.html", products=products or [],
                      catalog_down=products is None, stale=stale, stale_at=at, categories=cats,
                      q=q, category=categorie)


@app.get("/produits/{product_id}")
async def product_detail(request: Request, product_id: int):
    cached = await backend.cached_get("products", f"/products/{product_id}")
    return await page(request, "product_detail.html", product=decorate(dict(cached.data)),
                      stale=cached.stale, stale_at=cached.fetched_at)


@app.get("/etat")
async def status_page(request: Request):
    checks = await backend.health()
    for check in checks:
        check["label"], check["impact"] = SERVICE_INFO[check["name"]]
    return await page(request, "status.html", checks=checks,
                      all_up=all(c["up"] for c in checks), checked_at=datetime.now(LOCAL_TZ))


# ---------- Connexion et inscription ----------
@app.get("/connexion")
async def login_form(request: Request, next: str = "/"):
    if request.state.user:
        return redirect(safe_next(next))
    return await page(request, "login.html", next=safe_next(next), email="", error=None)


@app.post("/connexion")
async def login(request: Request):
    form = await request.form()
    email, target = str(form.get("email", "")).strip(), safe_next(form.get("next"))
    try:
        session = await backend.call("users", "POST", "/auth/login",
                                     json={"email": email, "password": str(form.get("password", ""))})
    except ApiError as exc:
        return await page(request, "login.html", exc.status, next=target, email=email,
                          error=exc.message)
    except ServiceDown:
        return await page(request, "login.html", 503, next=target, email=email,
                          error="La connexion est momentanément indisponible. "
                                "Le catalogue reste consultable, réessayez dans un instant.")
    response = redirect(target, f"Bonjour {session['user']['name']} !")
    set_session(response, session["access_token"])
    return response


@app.get("/inscription")
async def register_form(request: Request, next: str = "/"):
    if request.state.user:
        return redirect(safe_next(next))
    return await page(request, "register.html", next=safe_next(next), values={}, error=None)


@app.post("/inscription")
async def register(request: Request):
    form = await request.form()
    values = {"name": str(form.get("name", "")).strip(), "email": str(form.get("email", "")).strip()}
    password, target = str(form.get("password", "")), safe_next(form.get("next"))
    error = None
    if len(password) < 8:
        error = "Le mot de passe doit compter au moins 8 caractères."
    elif password != form.get("password_confirm"):
        error = "Les deux mots de passe ne sont pas identiques."
    status_code = 400
    if not error:
        try:
            session = await backend.call("users", "POST", "/auth/register",
                                         json={**values, "password": password})
        except ApiError as exc:
            error, status_code = exc.message, exc.status
            if exc.status == 422:
                error = "Vérifiez votre nom (2 caractères au moins) et votre adresse e-mail."
        except ServiceDown:
            error, status_code = ("L'inscription est momentanément indisponible. "
                                  "Réessayez dans un instant."), 503
    if error:
        return await page(request, "register.html", status_code, next=target, values=values,
                          error=error)
    await backend.optional("notifications", "POST", "/internal/notifications", internal=True,
                           timeout=2, json={"user_id": session["user"]["id"],
                                            "title": f"Bienvenue chez {APP_NAME}",
                                            "body": "Votre compte est créé. Bonne visite !",
                                            "link": "/catalogue",
                                            "dedup_key": f"welcome-{session['user']['id']}"})
    response = redirect(target, "Votre compte est créé.")
    set_session(response, session["access_token"])
    return response


@app.post("/deconnexion")
async def logout(request: Request):
    response = redirect("/", "Vous êtes déconnecté.", "info")
    response.delete_cookie("session")
    return response


# ---------- Mon compte ----------
@app.get("/compte")
async def account(request: Request):
    user, token = need_user(request)
    try:
        profile, users_down = await backend.call("users", "GET", "/users/me", token=token), False
    except ServiceDown:
        profile, users_down = dict(user), True  # on affiche ce que contient la session
    return await page(request, "account.html", profile=profile, users_down=users_down)


@app.post("/compte")
async def account_update(request: Request):
    _, token = need_user(request)
    form = await request.form()
    body = {k: (str(form.get(k, "")).strip() or None) for k in ("name", "phone", "address", "city")}
    session = await backend.call("users", "PUT", "/users/me", token=token, json=body)
    response = redirect("/compte", "Profil enregistré.")
    set_session(response, session["access_token"])
    return response


@app.post("/compte/mot-de-passe")
async def account_password(request: Request):
    _, token = need_user(request)
    form = await request.form()
    new = str(form.get("new_password", ""))
    if len(new) < 8:
        return redirect("/compte", "Le nouveau mot de passe doit compter au moins 8 caractères.", "error")
    if new != form.get("new_password_confirm"):
        return redirect("/compte", "Les deux mots de passe ne sont pas identiques.", "error")
    await backend.call("users", "POST", "/users/me/password", token=token,
                       json={"current_password": str(form.get("current_password", "")),
                             "new_password": new})
    return redirect("/compte", "Mot de passe modifié.")


# ---------- Notifications ----------
@app.get("/notifications")
async def notifications(request: Request):
    _, token = need_user(request)
    items = await backend.call("notifications", "GET", "/notifications", token=token)
    return await page(request, "notifications.html", items=items)


@app.post("/notifications/lire")
async def notifications_read(request: Request):
    _, token = need_user(request)
    await backend.call("notifications", "POST", "/notifications/read-all", token=token)
    return redirect("/notifications")


# ---------- Panier ----------
async def cart_lines(token: str, fresh: bool = False):
    """Lignes du panier avec prix et stock du catalogue ; à défaut, la copie gardée par le panier."""
    cart = await backend.call("cart", "GET", "/cart", token=token)
    current, catalog_down = {}, False
    if cart["items"]:
        ids = ",".join(str(i["product_id"]) for i in cart["items"])
        try:
            if fresh:
                found = await backend.call("products", "GET", "/products", params={"ids": ids})
            else:
                found = (await backend.cached_get("products", "/products", {"ids": ids}, ttl=5)).data
            current = {p["id"]: p for p in found}
        except (ServiceDown, ApiError):
            catalog_down = True
    lines = []
    for item in cart["items"]:
        product = current.get(item["product_id"])
        price = product["price"] if product else item["unit_price"]
        stock = product["stock"] if product else (None if catalog_down else 0)
        lines.append({"product_id": item["product_id"], "quantity": item["quantity"],
                      "name": product["name"] if product else item["product_name"],
                      "unit_price": price, "subtotal": round(price * item["quantity"], 2),
                      "stock": stock, "short": stock is not None and stock < item["quantity"]})
    return lines, round(sum(l["subtotal"] for l in lines), 2), catalog_down


@app.get("/panier")
async def cart_page(request: Request):
    _, token = need_user(request)
    lines, total, catalog_down = await cart_lines(token)
    return await page(request, "cart.html", lines=lines, total=total, catalog_down=catalog_down,
                      blocked=catalog_down or any(l["short"] for l in lines))


@app.post("/panier/ajouter")
async def cart_add(request: Request):
    _, token = need_user(request)
    form = await request.form()
    product_id = to_int(form.get("product_id"))
    quantity = max(1, min(99, to_int(form.get("quantity"), 1)))
    product = (await backend.cached_get("products", f"/products/{product_id}")).data
    if not product.get("is_active") or product["stock"] < 1:
        return back(request, f"« {product['name']} » n'est plus disponible.")
    await backend.call("cart", "POST", "/cart/items", token=token,
                       json={"product_id": product_id, "quantity": quantity,
                             "product_name": product["name"], "unit_price": product["price"]})
    return back(request, f"« {product['name']} » ajouté au panier.", "ok")


@app.post("/panier/modifier")
async def cart_update(request: Request):
    _, token = need_user(request)
    form = await request.form()
    quantity = max(0, min(99, to_int(form.get("quantity"), 1)))
    await backend.call("cart", "PUT", f"/cart/items/{to_int(form.get('product_id'))}", token=token,
                       json={"quantity": quantity})
    return redirect("/panier")


@app.post("/panier/supprimer")
async def cart_remove(request: Request):
    _, token = need_user(request)
    form = await request.form()
    await backend.call("cart", "DELETE", f"/cart/items/{to_int(form.get('product_id'))}", token=token)
    return redirect("/panier", "Article retiré du panier.", "info")


# ---------- Commande ----------
@app.get("/commander")
async def checkout_form(request: Request):
    user, token = need_user(request)
    try:
        lines, total, catalog_down = await cart_lines(token, fresh=True)
    except ServiceDown as exc:
        return redirect("/panier", DOWN_MESSAGES[exc.service], "error")
    if not lines:
        return redirect("/panier")
    if catalog_down or any(l["short"] for l in lines):
        return redirect("/panier")
    # Le profil préremplit l'adresse ; sans lui, le formulaire reste simplement vide.
    profile = await backend.optional("users", "GET", "/users/me", token=token, default={}, timeout=2)
    values = {"shipping_name": profile.get("name") or user["name"],
              "shipping_phone": profile.get("phone") or "",
              "shipping_address": profile.get("address") or "",
              "shipping_city": profile.get("city") or ""}
    return await page(request, "checkout.html", lines=lines, total=total, values=values,
                      ref=uuid.uuid4().hex)


@app.post("/commander")
async def checkout(request: Request):
    _, token = need_user(request)
    form = await request.form()
    cart = await backend.call("cart", "GET", "/cart", token=token)
    if not cart["items"]:
        return redirect("/panier")
    body = {k: str(form.get(k, "")).strip() for k in
            ("ref", "shipping_name", "shipping_phone", "shipping_address", "shipping_city")}
    body["items"] = [{"product_id": i["product_id"], "quantity": i["quantity"]}
                     for i in cart["items"]]
    try:
        order = await backend.call("orders", "POST", "/orders", token=token, json=body, timeout=12)
    except ApiError as exc:
        if exc.status == 401:
            raise
        if exc.status == 422:
            return redirect("/commander", "Complétez tous les champs de livraison.", "error")
        message = exc.message
        if isinstance(exc.detail, dict) and exc.detail.get("problems"):
            message += " " + " ; ".join(f"{p['name']} : {p['available']} en stock"
                                        for p in exc.detail["problems"])
        return redirect("/panier", message, "error")
    backend.forget("products")  # le stock vient de changer
    await backend.optional("cart", "DELETE", "/cart", token=token)
    return redirect(f"/commandes/{order['id']}", "Commande enregistrée. Merci !")


@app.get("/commandes")
async def orders_page(request: Request):
    _, token = need_user(request)
    orders = await backend.call("orders", "GET", "/orders", token=token)
    return await page(request, "orders.html", orders=orders)


@app.get("/commandes/{order_id}")
async def order_detail(request: Request, order_id: int):
    user, token = need_user(request)
    order = await backend.call("orders", "GET", f"/orders/{order_id}", token=token)
    return await page(request, "order_detail.html", order=order,
                      can_cancel=order["status"] == "confirmed",
                      admin_actions=NEXT_STATUS.get(order["status"], []) if user["role"] == "admin" else [])


@app.post("/commandes/{order_id}/annuler")
async def order_cancel(request: Request, order_id: int):
    _, token = need_user(request)
    await backend.call("orders", "POST", f"/orders/{order_id}/cancel", token=token)
    backend.forget("products")
    return redirect(f"/commandes/{order_id}", "Commande annulée.", "info")


# ---------- Administration ----------
@app.get("/admin")
async def admin_home(request: Request):
    _, token = need_admin(request)
    stats, products, users, recent = await asyncio.gather(
        backend.optional("orders", "GET", "/admin/stats", token=token),
        backend.optional("products", "GET", "/products", params={"include_inactive": "true"}),
        backend.optional("users", "GET", "/users", token=token),
        backend.optional("orders", "GET", "/admin/orders", token=token, params={"status": "confirmed"}))
    low_stock = sorted((p for p in products or [] if p["stock"] <= 3 and p["is_active"]),
                       key=lambda p: p["stock"])
    return await page(request, "admin/dashboard.html", stats=stats, products=products, users=users,
                      low_stock=low_stock, to_ship=recent)


@app.get("/admin/produits")
async def admin_products(request: Request):
    need_admin(request)
    products = await backend.call("products", "GET", "/products", params={"include_inactive": "true"})
    return await page(request, "admin/products.html", products=products)


def product_body(form) -> dict:
    return {"name": str(form.get("name", "")).strip(),
            "description": str(form.get("description", "")).strip() or None,
            "price": to_float(form.get("price"), -1), "stock": to_int(form.get("stock"), -1),
            "category": str(form.get("category", "")).strip() or None,
            "image_url": str(form.get("image_url", "")).strip() or None,
            "is_active": form.get("is_active") == "on"}


async def save_product(request: Request, product_id: int | None):
    _, token = need_admin(request)
    body = product_body(await request.form())
    try:
        if product_id is None:
            await backend.call("products", "POST", "/products", token=token, json=body)
        else:
            await backend.call("products", "PUT", f"/products/{product_id}", token=token, json=body)
    except ApiError as exc:
        if exc.status in (401, 404):
            raise
        return await page(request, "admin/product_form.html", 400, product=body,
                          product_id=product_id, categories=await categories(),
                          error="Vérifiez le nom, le prix et le stock (nombres positifs) et, "
                                "si elle est renseignée, l'adresse de l'image (http… ou https…).")
    backend.forget("products")
    return redirect("/admin/produits", "Produit enregistré.")


@app.get("/admin/produits/nouveau")
async def admin_product_new(request: Request):
    need_admin(request)
    return await page(request, "admin/product_form.html", product={"is_active": True, "stock": 0},
                      product_id=None, categories=await categories(), error=None)


@app.post("/admin/produits/nouveau")
async def admin_product_create(request: Request):
    return await save_product(request, None)


@app.get("/admin/produits/{product_id}")
async def admin_product_edit(request: Request, product_id: int):
    need_admin(request)
    product = await backend.call("products", "GET", f"/products/{product_id}")
    return await page(request, "admin/product_form.html", product=product, product_id=product_id,
                      categories=await categories(), error=None)


@app.post("/admin/produits/{product_id}")
async def admin_product_update(request: Request, product_id: int):
    return await save_product(request, product_id)


@app.post("/admin/produits/{product_id}/supprimer")
async def admin_product_delete(request: Request, product_id: int):
    _, token = need_admin(request)
    await backend.call("products", "DELETE", f"/products/{product_id}", token=token)
    backend.forget("products")
    return redirect("/admin/produits", "Produit supprimé.", "info")


@app.get("/admin/commandes")
async def admin_orders(request: Request, statut: str = ""):
    _, token = need_admin(request)
    orders = await backend.call("orders", "GET", "/admin/orders", token=token,
                                params={"status": statut} if statut else None)
    return await page(request, "admin/orders.html", orders=orders, statut=statut,
                      statuses=STATUS_LABELS, next_status=NEXT_STATUS)


@app.post("/admin/commandes/{order_id}/statut")
async def admin_order_status(request: Request, order_id: int):
    _, token = need_admin(request)
    form = await request.form()
    new_status = str(form.get("status", ""))
    await backend.call("orders", "PUT", f"/admin/orders/{order_id}/status", token=token,
                       json={"status": new_status})
    if new_status == "cancelled":
        backend.forget("products")
    return back(request, f"Commande n° {order_id} : {STATUS_LABELS.get(new_status, new_status).lower()}.", "ok")


@app.get("/admin/utilisateurs")
async def admin_users(request: Request):
    _, token = need_admin(request)
    users = await backend.call("users", "GET", "/users", token=token)
    return await page(request, "admin/users.html", users=users)


@app.post("/admin/utilisateurs/{user_id}")
async def admin_user_update(request: Request, user_id: int):
    _, token = need_admin(request)
    form = await request.form()
    body = {}
    if form.get("role") in ("customer", "admin"):
        body["role"] = form.get("role")
    if form.get("is_active") in ("true", "false"):
        body["is_active"] = form.get("is_active") == "true"
    await backend.call("users", "PUT", f"/users/{user_id}", token=token, json=body)
    return redirect("/admin/utilisateurs", "Compte mis à jour (effectif à sa prochaine connexion).")
