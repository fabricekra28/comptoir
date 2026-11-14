#!/bin/sh
# Plus d'attente bloquante ici : le service patiente lui-même jusqu'à ce que
# sa base réponde (voir init_db dans common.py), quel que soit son nom d'hôte.
exec "$@"
