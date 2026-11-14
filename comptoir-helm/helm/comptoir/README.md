# Chart Helm de Comptoir

Déploie la boutique sur Kubernetes : la gateway, les cinq services et PostgreSQL,
dans un namespace dédié.

## Installer

```bash
# 1. Le Secret, une seule fois (jamais dans Git)
kubectl create namespace comptoir
kubectl -n comptoir create secret generic comptoir-secrets \
  --from-literal=JWT_SECRET="$(openssl rand -base64 48)" \
  --from-literal=INTERNAL_TOKEN="$(openssl rand -base64 48)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=ADMIN_EMAIL="admin@exemple.com" \
  --from-literal=ADMIN_PASSWORD="un-mot-de-passe-solide"

# 2. La boutique (remplacez 12 par le numéro d'un build Jenkins publié sur Docker Hub)
helm upgrade --install comptoir helm/comptoir \
  --namespace comptoir \
  --set secrets.existingSecret=comptoir-secrets \
  --set image.tag=12 \
  --atomic --wait --timeout 10m

# 3. Vérifier
kubectl -n comptoir get pods
```

La boutique répond alors sur `http://<adresse-d-un-noeud>:30080` (accès de test, sans HTTPS).

## Ce que la chart crée

| Objet | Rôle |
|---|---|
| 6 Deployments et Services | gateway, users, products, orders, cart, notifications. Seule la gateway est exposée. |
| StatefulSet `postgres` | PostgreSQL avec un volume persistant et une base par service. |
| ConfigMap `comptoir-config` | Adresses des services, nom de la boutique, devise. |
| 9 NetworkPolicies | Tout est refusé par défaut ; seuls les échanges nécessaires sont ouverts. |
| 5 PodDisruptionBudgets | Garde un pod de chaque service répliqué pendant une maintenance de noeud. |
| Gateway et HTTPRoute | Seulement si `exposure.gatewayApi.enabled` vaut `true`. |

## Passer à Gateway API et HTTPS

Il faut un contrôleur Gateway API dans le cluster, un nom de domaine et, pour les
certificats automatiques, cert-manager avec un ClusterIssuer.

```bash
helm upgrade comptoir helm/comptoir --namespace comptoir --reuse-values \
  --set exposure.gatewayApi.enabled=true \
  --set exposure.gatewayApi.gatewayClassName=<classe-de-votre-controleur> \
  --set exposure.gatewayApi.hostname=boutique.exemple.com \
  --set exposure.gatewayApi.tls.enabled=true \
  --set exposure.gatewayApi.tls.clusterIssuer=letsencrypt-prod \
  --set networkPolicy.gatewayControllerNamespace=<namespace-du-controleur>
```

Le service de la gateway repasse alors en `ClusterIP`, HTTP est redirigé vers HTTPS
et le cookie de session n'est plus envoyé qu'en HTTPS.

## Réglages utiles

| Valeur | Défaut | Effet |
|---|---|---|
| `image.tag` | `latest` | Version des six images. Jenkins y met le numéro du build. |
| `services.<nom>.replicas` | 2 (1 pour notifications) | Nombre de pods. |
| `app.seedDemo` | `true` | 12 produits d'exemple si le catalogue est vide. |
| `networkPolicy.enabled` | `true` | Cloisonnement réseau. `false` pour l'écarter lors d'un diagnostic. |
| `postgresql.enabled` | `true` | `false` : utiliser `externalDatabase.host`. |
| `postgresql.storage.size` | `2Gi` | Taille du volume de la base. |

## Revenir en arrière

```bash
helm -n comptoir history comptoir
helm -n comptoir rollback comptoir <révision>
```

## Limites à connaître

- **PostgreSQL** tourne en un seul exemplaire : s'il tombe, tout ce qui écrit s'arrête.
  Sauvegardez son volume, ou utilisez une base gérée avec `postgresql.enabled=false`.
- **Bases créées une seule fois**, à la création du volume. Désinstaller la chart ne
  supprime pas le volume (`kubectl -n comptoir get pvc`).
- **NetworkPolicies** : sans effet si le plugin réseau du cluster ne les applique pas.
- **Mot de passe PostgreSQL** : lettres et chiffres uniquement.
