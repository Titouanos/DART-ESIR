# DartVision — Améliorations différées

Liste des points connus, non bloquants pour le MVP kiosque, à reprendre dans une PR séparée.

## Rebranchement à chaud d'une caméra USB

**Symptôme actuel** : `cv2.VideoCapture.read()` retourne `False` durablement
après débranchement. Côté `_build_system_snapshot` on détecte bien le passage
`ok → False` (via le timestamp du dernier read OK). Mais une fois la cam
rebranchée, `cv2.VideoCapture` ne re-probe pas le device — il faut release
+ reopen explicite.

**Ce qu'il faudrait** :
- nouveau état "reopening" par cam dans `DartVision`
- détection du dernier-read-OK > seuil → tentative de reopen avec backoff
  exponentiel (1s, 2s, 4s, …, plafonné à 30s) pour ne pas spammer V4L2
- coordination avec la boucle principale pour éviter race condition sur
  `self.caps[i]` pendant le release+reopen
- pause de la détection sur cette cam pendant la fenêtre de reopen
- restauration du `cam_position_segment` et de la référence après reopen
- endpoint `/api/cam/{slot}/reopen` pour déclencher manuellement aussi

**Estimation honnête** : ~150 lignes + tests d'intégration. Pas 5.

**Pour l'instant** : cas d'usage = cams branchées en permanence sur le
kiosque. Le débranchement est détecté (UX correcte : badge rouge dans
la topbar), il suffit juste de relancer `main.py` après rebranchement.

## Latence de détection MJPEG

Le streaming actuel passe par un encodage JPEG q=75 dans la boucle
OpenCV puis lecture côté `/api/cam/{slot}/mjpeg`. Latence observée :
200-400ms côté navigateur. Acceptable pour le kiosque mais pas pour le
contrôle distant mobile.

Pistes : WebRTC via aiortc, ou H.264 hardware via `gst-launch-1.0`.

## Résiduel RANSAC réel

Le design affiche un résiduel "0.62 mm" — actuellement `_build_system_snapshot`
renvoie `0.0` parce que `calibration.py` ne le calcule pas. Il faudrait
exposer la sortie RANSAC de `cv2.findHomography` et la stocker dans le
Calibrator.

## Multi-leg X01

Structure prête côté `GameEngine.get_full_state()` : `leg.current` / `leg.total`.
Pour l'instant `total=1` toujours. À implémenter quand demandé.
