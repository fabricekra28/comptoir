#!/bin/sh
# La gateway n'attend plus les autres services : elle démarre seule et
# affiche un mode dégradé tant qu'un service manque.
exec "$@"
