# Chart Helm de Comptoir

Déploie la boutique sur Kubernetes : la gateway, les cinq services et PostgreSQL,
dans un namespace dédié.

## Installer sur k3s avec Argo CD

Argo CD déploie la chart depuis Git. Jenkins ne touche pas au cluster : il inscrit la
version des images dans `image-tag.yaml`, et Argo CD applique le changement.

Les quatre premières étapes se font une seule fois, depuis la racine du dépôt.

```bash
# 1. Activer Gateway API dans le Traefik de k3s (Traefik redémarre, une minute environ)
kubectl apply -f k3s/traefik-gateway-api.yaml
kubectl get gatewayclass          # doit afficher « traefik »

# 2. Le Secret de la boutique (jamais dans Git)
kubectl create namespace comptoir
kubectl -n comptoir create secret generic comptoir-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -base64 48)" \
  --from-literal=INTERNAL_TOKEN="$(openssl rand -base64 48)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=ADMIN_EMAIL="admin@exemple.com" \
  --from-literal=ADMIN_PASSWORD="un-mot-de-passe-solide"

# 3. Le certificat auto-signé
sh k3s/certificat-auto-signe.sh comptoir.test

# 4. Confier la boutique à Argo CD
kubectl apply -f argocd/application.yaml
```

Sur votre poste, ajoutez l'adresse du noeud k3s (`kubectl get nodes -o wide`) à `/etc/hosts` :

```
<adresse-du-noeud>  comptoir.test
```

La boutique répond alors sur https://comptoir.test. Le navigateur affiche un avertissement
de sécurité : c'est normal avec un certificat auto-signé, acceptez-le pour continuer.

Si `kubectl get gatewayclass` répond que ce type de ressource n'existe pas, votre version
de k3s n'inclut pas Gateway API. Installez-le puis reprenez l'étape 1 :

```bash
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
```

### Déployer une nouvelle version

Lancez le build Jenkins avec `DEPLOY` coché. À la fin, Jenkins pousse un commit
« deploy: images du build N » ; Argo CD le détecte (trois minutes au plus) et met à jour
les pods un par un. Pour suivre :

```bash
kubectl -n argocd get application comptoir
kubectl -n comptoir get pods
```

### Revenir en arrière

Annulez le commit de déploiement : Argo CD remet la version précédente.

```bash
git revert <commit-de-déploiement> && git push
```

## Installer sans Argo CD

```bash
helm upgrade --install comptoir helm/comptoir \
  --namespace comptoir --create-namespace \
  --set secrets.existingSecret=comptoir-secrets \
  --set image.tag=12 \
  --atomic --wait --timeout 10m
```

Sans `values-k3s.yaml`, la boutique répond sur `http://<adresse-d-un-noeud>:30080`
(accès de test, sans HTTPS).

## Ce que la chart crée

| Objet | Rôle |
|---|---|
| 6 Deployments et Services | gateway, users, products, orders, cart, notifications. Seule la gateway est exposée. |
| StatefulSet `postgres` | PostgreSQL avec un volume persistant et une base par service. |
| ConfigMap `comptoir-config` | Adresses des services, nom de la boutique, devise. |
| 9 NetworkPolicies | Tout est refusé par défaut ; seuls les échanges nécessaires sont ouverts. |
| 5 PodDisruptionBudgets | Garde un pod de chaque service répliqué pendant une maintenance de noeud. |
| Gateway et HTTPRoute | Seulement si `exposure.gatewayApi.enabled` vaut `true`. |

## Fichiers de valeurs

| Fichier | Rôle |
|---|---|
| `values.yaml` | Tous les réglages et leurs valeurs par défaut. |
| `values-k3s.yaml` | Réglages du cluster k3s : Traefik, nom de domaine, HTTPS, Secret. |
| `image-tag.yaml` | Version des images. Réécrit par Jenkins, ne pas modifier à la main. |

Avec un vrai nom de domaine et cert-manager, remplacez le certificat auto-signé en
renseignant `exposure.gatewayApi.tls.clusterIssuer` et `hostname` dans `values-k3s.yaml`.

## Réglages utiles

| Valeur | Défaut | Effet |
|---|---|---|
| `image.tag` | `latest` | Version des six images. Jenkins y met le numéro du build. |
| `services.<nom>.replicas` | 2 (1 pour notifications) | Nombre de pods. |
| `app.seedDemo` | `true` | 12 produits d'exemple si le catalogue est vide. |
| `networkPolicy.enabled` | `true` | Cloisonnement réseau. `false` pour l'écarter lors d'un diagnostic. |
| `postgresql.enabled` | `true` | `false` : utiliser `externalDatabase.host`. |
| `postgresql.storage.size` | `2Gi` | Taille du volume de la base. |

## Limites à connaître

- **PostgreSQL** tourne en un seul exemplaire : s'il tombe, tout ce qui écrit s'arrête.
  Sauvegardez son volume, ou utilisez une base gérée avec `postgresql.enabled=false`.
- **Bases créées une seule fois**, à la création du volume. Désinstaller la chart ne
  supprime pas le volume (`kubectl -n comptoir get pvc`).
- **NetworkPolicies** : k3s les applique. Sur un autre cluster, elles sont sans effet si le
  plugin réseau ne les gère pas.
- **Certificat auto-signé** : valable 365 jours ; relancez le script pour le renouveler.
- **Mot de passe PostgreSQL** : lettres et chiffres uniquement.
