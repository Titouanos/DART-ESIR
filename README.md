# DartVision v3

Système de scoring automatique de fléchettes, multi-caméra avec fusion par zones de confiance.

## Quick Start

```bash
pip install -r requirements.txt

# 3 cams, positions auto-détectées
python main.py --cams 8 6 4 --recalibrate

# 501 à 2 joueurs, positions manuelles
python main.py --mode 501 --names Titouan Kevin --cams 8 6 4 --cam-positions 11 18 6
```

## Architecture v3

```
Cam 0 (seg 11) ─→ Warp ─→ Detect ─→ Tip + DiffScore ──┐
Cam 1 (seg 18) ─→ Warp ─→ Detect ─→ Tip + DiffScore ──┤──→ FUSION ENGINE ──→ Score
Cam 2 (seg  6) ─→ Warp ─→ Detect ─→ Tip + DiffScore ──┘     │
                                                               ├─ Confidence zones
                                                               ├─ Tip agreement check
                                                               └─ Weighted average
```

### Fusion par zones de confiance

Chaque caméra a une "zone de confiance" basée sur le segment qu'elle voit de face :
- Cam face au 11 → max confiance sur segments 8-14, confiance dégradée côté opposé
- Cam face au 18 → max confiance sur segments 1-20-5
- Cam face au 6 → max confiance sur segments 13-6-10

Quand plusieurs cams détectent simultanément :
- **Tips proches** (<40px) → moyenne pondérée par confiance × diff_score
- **Tips divergents** → on garde la détection de la caméra la plus confiante pour cette zone

### Détection de la pointe

1. Frame differencing vs référence → contours
2. `minAreaRect` sur chaque contour → axe principal = direction de la fléchette
3. Projection sur l'axe → extrémité la plus proche du centre = **tip**
4. Moyenne des 3 points les plus extrêmes pour stabilité
5. Shift de 4px vers le centre (point d'entrée réel)

## Contrôles

| Touche | Action |
|--------|--------|
| `ESPACE` | Capturer référence (board vide) |
| `u` | Annuler dernier lancer |
| `n` | Tour suivant |
| `d` | Toggle debug (overlay + masque diff) |
| `r` | Reset complet |
| `c` | Recalibrer |
| `1-3` | Toggle affichage caméra individuelle |
| `q`/`ESC` | Quitter |

## Fichiers

| Fichier | Rôle |
|---------|------|
| `config.py` | Paramètres (seuils, géométrie, fusion) |
| `board.py` | Géométrie du board, scoring polaire |
| `calibration.py` | Homographie 4 points, auto-détection position cam |
| `detector.py` | Détection par frame diff + tip elongation |
| `fusion.py` | **Nouveau** : fusion multi-cam avec zones de confiance |
| `game.py` | Moteur de jeu (free/501/301, bust, undo) |
| `main.py` | Point d'entrée, boucle principale, affichage |

## Tuning (`config.py`)

### Trop de faux positifs
```python
DIFF_THRESHOLD = 50    # Monter (défaut 40)
MIN_DART_AREA = 200    # Monter (défaut 120)
STABLE_FRAMES = 20     # Monter (défaut 15)
```

### Rate des fléchettes
```python
DIFF_THRESHOLD = 30    # Baisser
MIN_DART_AREA = 80     # Baisser
STABLE_FRAMES = 10     # Baisser
```

### Score tombe dans le mauvais segment
- Recalibrer (`c`)
- Vérifier overlay debug (`d`)
- Ajuster `CONFIDENCE_FALLOFF` (0.3 = forte différence entre zones, 0.8 = quasi uniforme)
