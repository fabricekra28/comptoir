"""Appels de la gateway vers les services, conçus pour encaisser les pannes.

- Délais courts : un service lent ne bloque pas la page.
- Disjoncteur par service : après plusieurs échecs, on cesse de l'appeler
  pendant quelques secondes au lieu de faire attendre chaque visiteur.
- Cache de secours : les lectures du catalogue gardent leur dernière réponse
  valide et la resservent si le service ne répond plus.
"""
import asyncio
import time
from dataclasses import dataclass

import httpx

FAILURES_TO_OPEN = 3      # échecs consécutifs avant d'ouvrir le disjoncteur
OPEN_SECONDS = 15         # durée pendant laquelle on n'appelle plus le service
STALE_MAX_SECONDS = 3600  # âge maximal d'une réponse de secours
CACHE_MAX_ENTRIES = 1000


class ServiceDown(Exception):
    def __init__(self, service: str):
        super().__init__(f"service {service} indisponible")
        self.service = service


class ApiError(Exception):
    """Le service a répondu, mais par un refus (4xx)."""

    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status, self.detail = status, detail

    @property
    def message(self) -> str:
        d = self.detail
        if isinstance(d, str):
            return d
        if isinstance(d, dict):
            return str(d.get("message", "Demande refusée"))
        if isinstance(d, list):  # erreurs de validation
            return "Certains champs sont invalides : " + ", ".join(
                str(e.get("loc", ["?"])[-1]) for e in d if isinstance(e, dict))
        return "Demande refusée"


@dataclass
class Cached:
    data: object
    stale: bool = False       # True : réponse de secours, le service ne répond pas
    fetched_at: float = 0.0


class Breaker:
    def __init__(self):
        self.failures = 0
        self.opened_at = 0.0

    @property
    def state(self) -> str:
        if self.failures < FAILURES_TO_OPEN:
            return "closed"
        return "open" if time.monotonic() - self.opened_at < OPEN_SECONDS else "half-open"

    def allow(self) -> bool:
        return self.state != "open"  # « half-open » laisse passer un appel d'essai

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= FAILURES_TO_OPEN:
            self.opened_at = time.monotonic()


class Backend:
    def __init__(self, urls: dict[str, str], internal_token: str):
        self.urls = urls
        self.internal_token = internal_token
        self.breakers = {name: Breaker() for name in urls}
        self.cache: dict[tuple, list] = {}  # clé -> [heure, données, à_rafraîchir]
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    async def call(self, service: str, method: str, path: str, *, token: str | None = None,
                   internal: bool = False, json=None, params=None, timeout: float | None = None):
        breaker = self.breakers[service]
        if not breaker.allow():
            raise ServiceDown(service)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if internal:
            headers["X-Internal-Token"] = self.internal_token
        attempts = 2 if method == "GET" else 1  # une lecture peut être retentée sans risque
        for attempt in range(attempts):
            try:
                r = await self.client.request(method, self.urls[service] + path, json=json,
                                              params=params, headers=headers,
                                              **({"timeout": timeout} if timeout else {}))
            except httpx.HTTPError:
                if attempt + 1 < attempts:
                    continue
                breaker.failure()
                raise ServiceDown(service)
            if r.status_code >= 500:
                if r.status_code == 503 and "application/json" in r.headers.get("content-type", ""):
                    detail = r.json().get("detail")
                    if detail:  # refus explicite d'un service vivant (ex. catalogue injoignable)
                        breaker.success()
                        raise ApiError(503, detail)
                breaker.failure()
                raise ServiceDown(service)
            breaker.success()
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                raise ApiError(r.status_code, detail)
            return r.json() if r.content else None

    async def cached_get(self, service: str, path: str, params: dict | None = None,
                         ttl: float = 15.0) -> Cached:
        """Lecture publique avec cache : fraîche si possible, de secours sinon."""
        key = (service, path, tuple(sorted((params or {}).items())))
        now = time.time()
        hit = self.cache.get(key)
        if hit and not hit[2] and now - hit[0] < ttl:
            return Cached(hit[1], False, hit[0])
        try:
            data = await self.call(service, "GET", path, params=params)
        except ServiceDown:
            if hit and now - hit[0] < STALE_MAX_SECONDS:
                return Cached(hit[1], True, hit[0])
            raise
        if len(self.cache) >= CACHE_MAX_ENTRIES:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = [now, data, False]
        return Cached(data, False, now)

    def forget(self, service: str) -> None:
        """À appeler après une écriture : la prochaine lecture ira chercher des données neuves.

        Les anciennes ne sont pas effacées : elles restent la copie de secours si le service tombe.
        """
        for key, entry in self.cache.items():
            if key[0] == service:
                entry[2] = True

    async def optional(self, service: str, method: str, path: str, default=None, **kwargs):
        """Pour les éléments secondaires d'une page : en cas de panne, on s'en passe."""
        try:
            return await self.call(service, method, path, **kwargs)
        except (ServiceDown, ApiError):
            return default

    async def health(self) -> list[dict]:
        """État de chaque service, mesuré en direct (sans passer par le disjoncteur)."""

        async def probe(name: str, url: str) -> dict:
            started = time.monotonic()
            try:
                r = await self.client.get(url + "/health", timeout=2.0)
                up = r.status_code == 200
            except httpx.HTTPError:
                up = False
            if up:
                self.breakers[name].success()
            return {"name": name, "up": up, "breaker": self.breakers[name].state,
                    "ms": round((time.monotonic() - started) * 1000)}

        return list(await asyncio.gather(*(probe(n, u) for n, u in self.urls.items())))
