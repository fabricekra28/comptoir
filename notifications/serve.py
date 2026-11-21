"""Lance uvicorn avec les mêmes options que la commande `uvicorn`.

À l'arrêt (SIGTERM envoyé par Kubernetes), uvicorn termine proprement les requêtes
en cours, puis se renvoie le signal reçu pour finir comme le voudrait ce signal.
Sur les nœuds Ubuntu 26.04, le profil AppArmor des conteneurs refuse ce signal
envoyé à soi-même : uvicorn affichait alors une erreur `PermissionError` à
chaque arrêt, alors que l'arrêt était déjà terminé. Dans ce cas seulement, le
programme se termine normalement (code 0) au lieu d'afficher l'erreur.
"""

import signal
import sys

import uvicorn

_raise_signal = signal.raise_signal


def _raise_signal_or_exit(signum: int) -> None:
    try:
        _raise_signal(signum)
    except PermissionError:
        sys.exit(0)


signal.raise_signal = _raise_signal_or_exit

if __name__ == "__main__":
    uvicorn.main()
