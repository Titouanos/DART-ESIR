#!/usr/bin/env bash
# DART-ESIR — check santé "le Pi est-il prêt ?" depuis un poste client (laptop).
#
#   ./dart-check.sh            # check une fois
#   ./dart-check.sh -w         # surveille en boucle (toutes les 3s)
#
# Stratégie d'adresse, dans l'ordre :
#   1. $DART_HOST si défini
#   2. DART.local (mDNS/avahi — stable même si l'IP DHCP du hotspot change)
#   3. scan du sous-réseau hotspot 172.20.10.0/28 par MAC Raspberry (2c:cf:67…)
#
# Le bug récurrent en prod : le hotspot réattribue une IP différente au Pi à
# chaque boot (172.20.10.3 -> .2 -> …). DART.local évite de la chercher.

set -uo pipefail
PORT="${DART_PORT:-8000}"

c_red=$'\e[31m'; c_grn=$'\e[32m'; c_yel=$'\e[33m'; c_dim=$'\e[2m'; c_rst=$'\e[0m'
ok()   { echo "${c_grn}✓${c_rst} $*"; }
bad()  { echo "${c_red}✗${c_rst} $*"; }
warn() { echo "${c_yel}!${c_rst} $*"; }

resolve_host() {
  # 1. override explicite
  if [ -n "${DART_HOST:-}" ]; then echo "$DART_HOST"; return 0; fi
  # 2. mDNS
  if getent hosts DART.local >/dev/null 2>&1; then echo "DART.local"; return 0; fi
  # 3. scan hotspot par MAC Raspberry (préfixes connus)
  for i in $(seq 1 14); do ping -c1 -W1 "172.20.10.$i" >/dev/null 2>&1 & done; wait
  local ip
  ip=$(ip neigh 2>/dev/null | awk '/172\.20\.10\./ && /lladdr/ {print $1, $5}' \
        | grep -iE "2c:cf:67|b8:27:eb|dc:a6:32|e4:5f:01|d8:3a:dd" | awk '{print $1; exit}')
  if [ -n "$ip" ]; then echo "$ip"; return 0; fi
  return 1
}

check_once() {
  local host
  if ! host=$(resolve_host); then
    bad "Pi introuvable : ni DART.local, ni Raspberry sur le hotspot 172.20.10.x"
    warn "Vérifie que le Pi est allumé et connecté au hotspot (LED, ~30s au boot)."
    return 2
  fi
  local base="http://${host}:${PORT}"
  local json
  json=$(curl -s --max-time 6 "${base}/api/health" 2>/dev/null)
  if [ -z "$json" ]; then
    bad "Pi joignable à ${host} mais le serveur web ne répond pas sur :${PORT}"
    warn "Le service est peut-être en train de démarrer. Sur le Pi : 'dartrestart'."
    return 2
  fi
  # Parse JSON avec python3 (présent partout) ; sortie: ready cams_ok cams_total ref calib state
  read -r ready cams_ok cams_total ref calib state caminfo <<<"$(python3 - "$json" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
cams=" ".join(f"{c['id']}={'ok' if c['ok'] else 'DOWN'}" for c in d.get("cams",[]))
print(d.get("ready"),d.get("cams_ok"),d.get("cams_total"),
      d.get("reference_ok"),d.get("calibration_ok"),d.get("game_state"),cams or "-")
PY
)"
  echo "${c_dim}Pi: ${host}:${PORT}${c_rst}"
  [ "$ready" = "True" ] && ok "PRÊT À JOUER" || warn "pas encore prêt"
  [ "$cams_ok" = "$cams_total" ] && ok "caméras $cams_ok/$cams_total  ($caminfo)" \
                                 || bad "caméras $cams_ok/$cams_total  ($caminfo)"
  [ "$ref" = "True" ]   && ok "référence capturée" || warn "référence non capturée (bouton 📸 / capture)"
  [ "$calib" = "True" ] && ok "calibration chargée" || bad "calibration absente"
  echo "${c_dim}partie: ${state}${c_rst}"
  [ "$ready" = "True" ] && return 0 || return 1
}

if [ "${1:-}" = "-w" ]; then
  while true; do clear; date "+%H:%M:%S"; check_once; sleep 3; done
else
  check_once
fi
