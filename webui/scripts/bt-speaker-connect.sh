#!/usr/bin/env bash
# DartVision — reconnecte l'enceinte Bluetooth au boot et la définit comme
# sink audio par défaut. Lancé par dartvision-speaker.service (--user).
#
# Boucle de réessais car au démarrage le Bluetooth et PipeWire/WirePlumber
# mettent quelques secondes à être prêts, et l'enceinte (SRS-XB10) peut
# tarder à accepter la connexion. On s'arrête dès que le sink bluez existe.
#
# Prérequis (faits une fois, persistants) : enceinte appairée+trusted, et
# SURTOUT pulseaudio masqué (sinon conflit -> sink instable). Voir mémoire
# dartvision-audio.
set -u

MAC="B8:D5:0B:65:4E:1D"
TRIES=30
SLEEP=4

# Le contrôleur doit être allumé pour pouvoir connecter.
bluetoothctl power on >/dev/null 2>&1

for i in $(seq 1 "$TRIES"); do
    # (Re)connexion si l'enceinte n'est pas déjà connectée.
    if ! bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; then
        bluetoothctl connect "$MAC" >/dev/null 2>&1
    fi

    # Le sink bluez n'apparaît que si WirePlumber a réussi à créer le nœud.
    BT=$(pactl list short sinks 2>/dev/null | grep -i bluez | cut -f2 | head -1)
    if [ -n "$BT" ]; then
        pactl set-default-sink "$BT"
        echo "[speaker] OK (essai $i) — sink par défaut : $BT"
        exit 0
    fi
    sleep "$SLEEP"
done

echo "[speaker] ÉCHEC — sink bluez introuvable après $TRIES essais"
exit 1
