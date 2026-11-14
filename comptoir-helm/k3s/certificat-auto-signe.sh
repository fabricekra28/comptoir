#!/bin/sh
# Crée un certificat auto-signé pour la boutique et l'enregistre dans le cluster.
# usage : sh k3s/certificat-auto-signe.sh [nom-de-domaine]     (défaut : comptoir.test)
set -eu

HOST="${1:-comptoir.test}"
DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout "$DIR/tls.key" -out "$DIR/tls.crt" \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=DNS:$HOST"

kubectl create namespace comptoir --dry-run=client -o yaml | kubectl apply -f -

kubectl -n comptoir create secret tls comptoir-tls \
  --cert="$DIR/tls.crt" --key="$DIR/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Certificat auto-signé enregistré pour $HOST (valable 365 jours)."
