# =============================================================================
# DART-ESIR — alias de debug post-déploiement
# =============================================================================
# À sourcer depuis ~/.bashrc :
#   source /home/rt/DART-ESIR-new/webui/scripts/dart-aliases.sh
# (Ou ajouter cette ligne au .bashrc une fois.)
#
# Tous les alias visent les services --user du compte rt.
# =============================================================================

# Logs live du serveur backend (main.py + boucle OpenCV + fastapi)
alias dartlogs='journalctl --user -u dartvision -f --no-pager'

# Logs live du kiosque (cage + chromium)
alias dartkiosklogs='journalctl --user -u dartvision-kiosk -f --no-pager'

# Statut combiné des 2 services
alias dartstatus='systemctl --user status dartvision dartvision-kiosk --no-pager -l'

# Redémarrage complet (backend + kiosque). Le kiosque sera relancé par cascade
# Wants= mais on les nomme explicitement pour clarté.
alias dartrestart='systemctl --user restart dartvision dartvision-kiosk'

# Stop tout (debug ou install d'une mise à jour)
alias dartstop='systemctl --user stop dartvision-kiosk dartvision'

# Re-vérifie les symlinks udev — utile si une cam ne répond plus
alias dartcams='for c in A B C; do
  echo "/dev/dart-cam-$c → $(readlink /dev/dart-cam-$c 2>/dev/null || echo MISSING)"
  v4l2-ctl -d /dev/dart-cam-$c --info 2>/dev/null | grep -E "Card type|Bus info|Device Caps" -A1 | head -4
  echo
done'
