#!/usr/bin/env bash
# =============================================================================
# DART-ESIR — Lance le kiosque Chromium dans cage sur la session tty1
# =============================================================================
# Appelé depuis ~/.bash_profile quand l'user rt s'autologue sur tty1
# (cf. webui/systemd/getty-autologin.conf). Garde-fous :
#
# - Ne s'exécute QUE sur tty1 — sinon un SSH ouvrirait des kiosques en
#   chaîne et casserait tout.
# - Wait-for-port : attend que le serveur dartvision écoute sur :8000
#   avant de lancer cage. Évite l'écran "Connection refused" au boot.
# - Verrou single-instance : si un cage tourne déjà, on n'en relance pas
#   un deuxième (sinon double cage = écran noir + double seat-grab).
# =============================================================================

set -u

LOCK=/tmp/dart-kiosk.lock
LOG=/tmp/dart-kiosk.log
URL=${DART_KIOSK_URL:-http://localhost:8000/}

# Log persistant pour debug (.bash_profile n'a pas toujours un terminal
# qui survit après le exec cage).
{ echo "--- $(date -Iseconds) dart-kiosk-launch.sh appelé ---"
  echo "tty=$(tty 2>/dev/null || echo none) XDG_VTNR=${XDG_VTNR:-unset} XDG_SEAT=${XDG_SEAT:-unset}"
  echo "user=$(id -un) pid=$$"
} >> "$LOG" 2>&1

# 1) On ne lance le kiosque QUE sur tty1.
# `tty(1)` est fiable dès que la session est ouverte ; XDG_VTNR pouvait
# arriver après le .bash_profile selon l'ordre PAM/systemd-logind.
CUR_TTY=$(tty 2>/dev/null || true)
case "$CUR_TTY" in
  /dev/tty1) echo "[kiosk] sur tty1, on continue" >> "$LOG" ;;
  *)         echo "[kiosk] tty=$CUR_TTY, skip" >> "$LOG"
             return 0 2>/dev/null || exit 0 ;;
esac

# 2) Single instance — `-x cage` matche le nom exécutable exact (PROC name),
#    pas la cmdline ; évite les faux positifs où un shell qui contient le mot
#    "cage" dans sa cmdline serait vu comme une instance de cage.
if pgrep -u "$(id -u)" -x cage >/dev/null 2>&1; then
  echo "[kiosk] cage déjà actif, skip." >> "$LOG"
  return 0 2>/dev/null || exit 0
fi

# 3) Attente du serveur sur :8000 (max 90s).
echo "[kiosk] Attente serveur :8000..." >> "$LOG"
for i in $(seq 1 180); do
  if curl -sf "$URL" >/dev/null 2>&1; then
    echo "[kiosk] serveur OK après ${i}*0.5s, démarrage cage." >> "$LOG"
    break
  fi
  sleep 0.5
done
if ! curl -sf "$URL" >/dev/null 2>&1; then
  echo "[kiosk] TIMEOUT serveur :8000" >> "$LOG"
  exit 1
fi

# 4) Lock pour éviter les double-launch concurrents.
exec 9>"$LOCK"
flock -n 9 || { echo "[kiosk] lock détenu, abort." >> "$LOG"; exit 0; }

# 5) cage + chromium. cage prend les args APRÈS `--`. `-d` fait que cage
#    exit quand le child exit (= si chromium crash, cage exit, et getty
#    respawn → .bash_profile relance).
echo "[kiosk] exec cage maintenant" >> "$LOG"
exec /usr/bin/cage -d -- /usr/bin/chromium \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI \
  --check-for-update-interval=31536000 \
  --ozone-platform=wayland \
  "$URL"
