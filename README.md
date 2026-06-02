# DartVision v3

Scoring automatique de fléchettes sur Raspberry Pi 5 avec 3 caméras USB,
fusion par zones de confiance, UI fanzine servie en kiosque Chromium.

---

## Architecture

```
                          /dev/dart-cam-A,B,C (udev rules, stables)
                                   │
                          ┌────────┴────────┐
                          │     main.py     │
                          │ (boucle OpenCV  │
                          │   + GameEngine  │
                          │   + Bridge WS)  │
                          └────────┬────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
       ┌──────┴──────┐      ┌──────┴──────┐      ┌──────┴──────┐
       │ MJPEG       │      │ WebSocket   │      │ Static HTML │
       │ /api/cam/*  │      │ /ws         │      │ /static/*   │
       └──────┬──────┘      └──────┬──────┘      └──────┬──────┘
              └────────────────────┼────────────────────┘
                                   │
                          ┌────────┴────────┐
                          │ FastAPI :8000   │
                          └────────┬────────┘
                                   │
                          ┌────────┴────────┐
                          │ cage + Chromium │  ← écran HDMI kiosque
                          │     --kiosk     │  ← + téléphones LAN (lecture)
                          └─────────────────┘
```

**Boot flow (Pi)** :
1. systemd boot → `loginctl enable-linger rt` → user services up sans login
2. `dartvision.service` (--user) démarre `main.py --headless` → ouvre les 3 cams via `/dev/dart-cam-*`, lance fastapi sur :8000
3. `getty@tty1.service` (override autologin) → `agetty --autologin rt` → `login -- rt` → bash login shell → `~/.bash_profile`
4. `.bash_profile` → `dart-kiosk-launch.sh` → attend que :8000 réponde → exec `cage -- chromium --kiosk http://localhost:8000/`

---

## Quick Start — Mode dev

```bash
# Sur ton laptop ou n'importe quelle machine avec 3 webcams + python venv
git clone https://github.com/Titouanos/DART-ESIR
cd DART-ESIR
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Lancement avec UI OpenCV native (clavier focus dans la fenêtre cv2)
python main.py --mode 501 --names Titouan Kevin --cams 0 2 4

# Ou en mode --headless : pas de fenêtre OpenCV, tu pilotes via le web
python main.py --mode 501 --names Titouan Kevin --cams 0 2 4 --headless
# Puis http://localhost:8000/ dans ton navigateur
```

Si tu n'utilises pas les udev rules (déploiement Pi), passe `--cams INT INT INT`
pour pointer sur les indexes V4L2 directement. Sinon le default est défini
dans `config.py` (`CAM_SLOTS` avec `device='/dev/dart-cam-*'`).

---

## Installation production sur Raspberry Pi 5

Testé sur Pi 5, Raspberry Pi OS Bookworm (kernel 6.12+).

### 1. Système

```bash
sudo apt-get update
sudo apt-get install -y \
  python3-venv python3-pip \
  cage chromium grim v4l-utils curl
```

### 2. Clone + venv

```bash
cd /home/rt
git clone https://github.com/Titouanos/DART-ESIR.git DART-ESIR-new
cd DART-ESIR-new
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3. Calibration

Avec un écran HDMI branché (la calibration est interactive : on clique
4 points sur chaque flux cam pour calculer l'homographie) :

```bash
./venv/bin/python main.py --cams 0 2 4 --recalibrate
```

Suis les instructions à l'écran : clique sur les 4 marqueurs visibles du
board pour chaque caméra. Le résultat est sauvegardé dans `calibration.json`.

### 4. udev rules (symlinks cam stables)

```bash
sudo cp webui/udev/99-dart-cams.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=video4linux --action=add
ls -l /dev/dart-cam-*   # → 3 symlinks dart-cam-A/B/C
```

Le mapping est fait par port USB physique (`KERNELS=="1-1"` etc.) — il
ne casse pas si tu débranches/rebranches une cam. Si tu **déplaces** une
cam sur un autre port USB, édite `webui/udev/99-dart-cams.rules`.

### 5. Service backend (systemd --user)

```bash
mkdir -p ~/.config/systemd/user/
cp webui/systemd/dartvision.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable dartvision.service
sudo loginctl enable-linger rt        # service démarre sans login
systemctl --user start dartvision.service
systemctl --user status dartvision    # → "active (running)"
curl http://localhost:8000/           # → HTML setup page
```

### 6. Kiosque autologin tty1

```bash
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d/
sudo cp webui/systemd/getty-autologin.conf /etc/systemd/system/getty@tty1.service.d/override.conf
sudo systemctl daemon-reload

# Snippet bash_profile (idempotent — re-exec sans risque)
grep -q "dart-kiosk-launch.sh" ~/.bash_profile || cat webui/scripts/bash_profile.snippet >> ~/.bash_profile
```

### 7. Reboot + check

```bash
sudo reboot
```

Au boot :
- `dartvision.service` démarre avec linger (avant tout login)
- `getty@tty1` autologin rt → `.bash_profile` → `dart-kiosk-launch.sh`
- Le launcher attend que `:8000` réponde (timeout 90s)
- `cage -- chromium --kiosk http://localhost:8000/` plein écran HDMI

L'UI doit s'afficher en plein écran sans intervention.

---

## Accès distant — téléphone / autre machine

L'UI est accessible depuis n'importe quel device sur le LAN du Pi :

```
http://<IP-du-Pi>:8000/
```

Toutes les actions (Undo, Next, Reset, Recalibrer) marchent depuis mobile —
les commandes passent par WebSocket et l'état est synchro sur tous les
clients (le kiosque HDMI + tous les téléphones se voient updater en live).

---

## Contrôles

### Clavier (UI OpenCV native uniquement, mode dev sans `--headless`)

| Touche | Action |
|---|---|
| `ESPACE` | Capturer référence (board vide) |
| `u` | Annuler dernier lancer |
| `n` | Tour suivant |
| `d` | Toggle debug (overlay + masque diff) |
| `r` | Reset partie |
| `c` | Recalibrer (ouvre flow interactif 4-points/cam) |
| `1-3` | Toggle affichage caméra individuelle |
| `q` / `ESC` | Quitter |

### Web UI

Boutons de l'écran de jeu : Undo, Next, Menu (Reset / Recalibrer / Quit).
**Recalibrer** ne fonctionne qu'en mode non-headless (besoin du clavier
+ écran). Sur le kiosque, le bouton est désactivé visuellement avec un
tooltip explicite — relance `main.py` sans `--headless` pour recalibrer.

---

## Troubleshooting

### Le kiosque ne démarre pas au boot

```bash
# Backend up ?
systemctl --user status dartvision       # → "active (running)" attendu
journalctl --user -u dartvision -n 50    # voir les erreurs

# Port :8000 réponse ?
curl -sf http://localhost:8000/ && echo OK

# Launcher kiosque a tourné ?
cat /tmp/dart-kiosk.log                  # log persistant à chaque tentative

# Cage tourne ?
pgrep -xa cage                           # → /usr/bin/cage -d -- /usr/bin/chromium ...
```

### Une cam ne répond plus → "ok: false" dans l'UI

```bash
# Symlinks udev OK ?
ls -l /dev/dart-cam-*                    # → 3 symlinks
v4l2-ctl -d /dev/dart-cam-A --info | grep -A2 "Device Caps"
# → doit contenir "Video Capture" (pas "Metadata Capture")
```

Si un symlink est manquant ou pointe vers un Metadata Capture :

```bash
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=video4linux --action=add
ls -l /dev/dart-cam-*
```

Si toujours KO après replug + re-trigger, vérifie le port USB physique
de la cam et le mapping `KERNELS=="X-Y"` dans
`/etc/udev/rules.d/99-dart-cams.rules` (cf. `v4l2-ctl --list-devices`
pour voir sur quel bus chaque cam est branchée).

### Le boot échoue avec `[FATAL] Slot X : device path … introuvable`

Le health check du démarrage refuse de lancer un kiosque qui pointerait
vers les mauvaises cams. Le message d'erreur cite les commandes de remédiation
à exécuter (re-install udev rules, vérification path, etc.).

### Recalibrer la cible

Le bouton **Recalibrer** de l'UI web est désactivé en mode kiosque
(flow interactif OpenCV avec clics souris pour les 4 points = pas
possible via web pour l'instant).

Pour recalibrer :

```bash
# 1. Arrête le kiosque (sinon il occupe le seat HDMI)
systemctl --user stop dartvision
sudo systemctl stop getty@tty1
# 2. Branche clavier + souris USB + écran HDMI
# 3. Lance main.py SANS --headless
cd /home/rt/DART-ESIR-new
./venv/bin/python main.py --cams 0 2 4 --recalibrate
# 4. Suis les instructions à l'écran (4 clics par cam)
# 5. Une fois fini, relance les services
systemctl --user start dartvision
sudo systemctl start getty@tty1
```

### Commandes utiles (alias bash)

Sourcer dans `~/.bashrc` ou ajoutées par le snippet `bash_profile.snippet` :

| Alias | Action |
|---|---|
| `dartlogs` | `journalctl --user -u dartvision -f` (logs serveur live) |
| `dartkiosklogs` | journalctl pour les logs systemd du kiosque |
| `dartstatus` | `systemctl --user status dartvision` |
| `dartrestart` | restart backend + kiosque |
| `dartstop` | stop tout |
| `dartcams` | liste symlinks udev + Device Caps de chaque |

### Tout reset à zéro (kiosque cassé, on repart propre)

```bash
systemctl --user disable dartvision
sudo rm /etc/systemd/system/getty@tty1.service.d/override.conf
sudo systemctl daemon-reload
# Retire le snippet de ~/.bash_profile manuellement
```

---

## Fichiers

| Fichier | Rôle |
|---|---|
| `config.py` | Paramètres (cams, seuils, géométrie, fusion, web port) |
| `board.py` | Géométrie du board, scoring polaire |
| `calibration.py` | Homographie 4-points (flow interactif OpenCV) |
| `calibration.json` | Homographies sauvegardées par `cam_index` |
| `detector.py` | Détection par frame diff + tip elongation |
| `fusion.py` | Fusion multi-cam avec zones de confiance |
| `game.py` | Moteur de jeu (free/501/301), Observer pattern, stats étendues |
| `main.py` | Point d'entrée, boucle OpenCV, hooks vers `Bridge` |
| `webui/server.py` | FastAPI : pages, MJPEG, WebSocket |
| `webui/bridge.py` | Pont thread-safe entre boucle sync OpenCV et asyncio uvicorn |
| `webui/static/*.html` | Pages UI fanzine (design haute fidélité) |
| `webui/static/*.js` | app.js (WS shared) + setup/game/calibration/end.js |
| `webui/static/style.css` | Tokens design (riso, off-register, IBM Plex Mono…) |
| `webui/udev/99-dart-cams.rules` | Symlinks stables `/dev/dart-cam-*` |
| `webui/systemd/dartvision.service` | Unit --user du backend |
| `webui/systemd/getty-autologin.conf` | Drop-in autologin tty1 |
| `webui/scripts/dart-kiosk-launch.sh` | Lance cage + Chromium depuis `.bash_profile` |
| `webui/scripts/bash_profile.snippet` | Lignes à ajouter à `~/.bash_profile` |
| `webui/scripts/dart-aliases.sh` | Alias debug |
| `webui/FUTURE.md` | Améliorations différées (hot-reconnect cam, RANSAC, etc.) |

---

## Tuning (`config.py`)

### Trop de faux positifs

```python
DIFF_THRESHOLD = 50    # Monter (défaut 30)
MIN_DART_AREA = 200    # Monter (défaut 80)
STABLE_FRAMES = 20     # Monter (défaut 8)
```

### Rate des fléchettes

```python
DIFF_THRESHOLD = 30    # Baisser
MIN_DART_AREA = 80     # Baisser
STABLE_FRAMES = 10     # Baisser
```

### Score tombe dans le mauvais segment

- Recalibrer (cf. troubleshooting plus haut)
- Vérifier overlay debug (`d` en mode dev)
- Ajuster `CONFIDENCE_FALLOFF` (0.3 = forte différence entre zones, 0.8 = quasi uniforme)

---

## Améliorations futures

Cf. [`webui/FUTURE.md`](webui/FUTURE.md) — rebranchement à chaud des cams,
recalibration via UI web, exposition du résiduel RANSAC réel, multi-leg X01.
