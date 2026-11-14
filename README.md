# Comptoir — boutique en ligne en microservices

Une boutique complète : catalogue, comptes clients, panier, commandes avec suivi,
messages, espace de gestion. Six services indépendants ; quand l'un tombe, seul ce
qui dépend de lui s'arrête et le reste continue.

## Démarrer

```bash
cp .env.example .env        # puis remplacez les trois secrets dans .env
docker compose up --build
```

Ouvrez http://localhost:8000.

- **Administrateur** : l'adresse et le mot de passe définis dans `.env`
  (`ADMIN_EMAIL`, `ADMIN_PASSWORD`). Le lien « Gestion » apparaît une fois connecté.
- **Client** : créez un compte depuis « Créer un compte ».
- Avec `SEED_DEMO=true`, le catalogue reçoit 12 produits d'exemple s'il est vide.

Pour voir la résistance aux pannes : `docker compose stop products-service`,
naviguez, puis `docker compose start products-service`. La page `/etat` montre
l'état de chaque service.

## Les services

| Dossier | Port | Rôle |
|---|---|---|
| `gateway/` | 8000 | Le site : pages, session, assemblage des données. Seul service exposé. |
| `users/` | 8001 | Comptes, mots de passe (bcrypt), connexion, rôles client / administrateur. |
| `products/` | 8002 | Catalogue, rayons, stock, réservations de stock. |
| `orders/` | 8003 | Commandes, suivi (confirmée, expédiée, livrée, annulée), chiffres de vente. |
| `cart/` | 8004 | Panier enregistré par compte. **Nouveau.** |
| `notifications/` | 8005 | Messages de suivi affichés dans l'espace client. **Nouveau.** |

Chaque service a sa propre base (`db/init.sql`) et ne lit jamais les tables d'un autre.
`common.py` est le même fichier copié dans les cinq services de données : si vous le
modifiez, recopiez-le partout.

## Ce qui se passe quand un service tombe

| Service en panne | Ce qui continue | Ce qui s'arrête |
|---|---|---|
| users | Tout pour les personnes déjà connectées, y compris commander. Catalogue pour tous. | Connexion, inscription, modification du profil. |
| products | Catalogue affiché depuis sa dernière copie (bandeau « mode dégradé »), panier, historique. | Nouvelles commandes, fiches jamais consultées. |
| orders | Catalogue, panier (conservé), compte. | Passage de commande, historique. |
| cart | Catalogue, historique des commandes, compte. | Panier, donc nouvelles commandes. |
| notifications | Tout. Les messages attendent et arrivent au retour du service. | Page « Messages ». |
| gateway | — | Le site (c'est le point d'entrée : faites-en tourner au moins deux, voir `k8s/`). |
| base PostgreSQL | — | Tout ce qui écrit. Voir « Limites ». |

Comment c'est obtenu :

- **Session vérifiée localement.** La connexion délivre un jeton signé (JWT, cookie
  `HttpOnly`). La gateway et chaque service le vérifient avec `JWT_SECRET`, sans
  rappeler `users`.
- **Délais courts et disjoncteur** (`gateway/backend.py`). Après 3 échecs, un service
  n'est plus appelé pendant 15 s : les pages répondent tout de suite au lieu d'attendre.
- **Copie de secours du catalogue.** La gateway garde la dernière réponse valide (1 h au
  plus) et la ressert si `products` ne répond plus.
- **Copies figées.** Une commande garde le nom et le prix des articles ; le panier aussi.
  L'historique reste lisible sans le catalogue.
- **Boîte d'envoi** (`orders/main.py`, table `outbox`). Notifications et remises en stock
  sont enregistrées avec la commande, puis envoyées et renvoyées jusqu'à réception.
- **Commande rejouable.** Chaque formulaire de commande porte une référence unique : un
  double clic ou un renvoi après panne ne crée ni doublon ni double débit de stock.
- **Stock exact.** La réservation verrouille les lignes en base : des commandes simultanées
  ne vendent jamais plus que le stock disponible.

## Depuis votre version 1

- La gateway n'est plus un CRUD générique : les anciens gabarits (`service_list.html`,
  `item_detail.html`, `create_form.html`, `edit_form.html`) sont supprimés.
- `start.sh` n'attend plus rien : la gateway démarre sans les autres services, et chaque
  service patiente lui-même jusqu'à ce que sa base réponde.
- **Base existante conservée.** Si `DATABASE_URL` pointe vers votre base actuelle, les
  tables `users` et `products` sont complétées au démarrage (colonnes ajoutées, données
  gardées). Vos produits existants reçoivent un stock de 0 : renseignez-le dans
  « Gestion > Produits ». Vos utilisateurs existants n'ont pas de mot de passe : ils
  le choisissent en passant par « Créer un compte » avec la même adresse.
- L'ancienne table `orders` n'est ni lue ni modifiée ; les commandes vivent dans
  `customer_orders` et `order_items`.
- Toutes les dépendances sont alignées sur celles de votre service users
  (FastAPI 0.143, Pydantic 2), image Python 3.12.

## Configuration

| Variable | Services | Rôle |
|---|---|---|
| `JWT_SECRET` | tous | Signe les sessions. Identique partout, long et aléatoire. |
| `INTERNAL_TOKEN` | tous | Autorise les appels entre services. |
| `DATABASE_URL` | tous sauf gateway | Base du service. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | users | Premier administrateur, créé s'il n'en existe aucun. |
| `SEED_DEMO` | products | `true` : produits d'exemple si le catalogue est vide. |
| `*_SERVICE_URL` | gateway, orders | Adresses des services. |
| `APP_NAME`, `CURRENCY`, `CURRENCY_DECIMALS`, `APP_TIMEZONE` | gateway | Nom, devise (ex. `FCFA` et `0`), fuseau horaire. |
| `COOKIE_SECURE` | gateway | `true` dès que le site est en HTTPS. |
| `TOKEN_HOURS` | users, gateway | Durée d'une session, 8 h par défaut. |

Chaque service expose `/live`, `/health` (base comprise) et `/metrics` (Prometheus) ;
la gateway expose `/healthz` et `/metrics`.

## Kubernetes

`k8s/` contient un ConfigMap, un Secret à remplir, et un Deployment + Service par
microservice (namespace `microservices`, sondes de vie et de disponibilité, deux
réplicas). Construisez et poussez les six images, remplacez `VOTRE_REGISTRE`, puis
`kubectl apply -f k8s/`. Branchez votre Ingress sur le service `gateway`, port 8000.
Ces manifestes n'ont pas été appliqués sur un cluster : relisez-les avant usage.

## Limites à connaître

- **Paiement** : à la livraison uniquement. Aucun paiement en ligne n'est intégré.
- **E-mails** : aucun envoi. Pas de « mot de passe oublié » ni de vérification d'adresse.
- **Désactivation d'un compte, changement de rôle** : effectifs à la prochaine connexion
  (8 h au plus), puisque les sessions sont vérifiées sans rappeler `users`.
- **Base de données** : `docker-compose.yml` lance un seul PostgreSQL pour les cinq bases.
  S'il tombe, tout s'arrête. En production, utilisez une instance par service ou un
  PostgreSQL répliqué, et sauvegardez-le.
- **HTTPS** : à assurer devant la gateway (Ingress, Caddy, Nginx…), avec `COOKIE_SECURE=true`.
- **Copie de secours et disjoncteur** : en mémoire, propres à chaque réplica de la gateway.
