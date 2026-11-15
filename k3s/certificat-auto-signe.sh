#!/bin/sh
# Crée un certificat auto-signé et l'enregistre dans le cluster.
# usage : sh k3s/certificat-auto-signe.sh [nom-de-domaine] [namespace] [nom-du-secret]
#   boutique : sh k3s/certificat-auto-signe.sh comptoir.test comptoir comptoir-tls  (valeurs par défaut)
#   Grafana  : sh k3s/certificat-auto-signe.sh grafana.test comptoir grafana-tls
set -eu

HOST="${1:-comptoir.test}"
NAMESPACE="${2:-comptoir}"
SECRET="${3:-comptoir-tls}"
DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout "$DIR/tls.key" -out "$DIR/tls.crt" \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=DNS:$HOST"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret tls "$SECRET" \
  --cert="$DIR/tls.crt" --key="$DIR/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Certificat auto-signé enregistré pour $HOST dans $NAMESPACE/$SECRET (valable 365 jours)."
