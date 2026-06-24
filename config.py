"""
DartVision v3 - Configuration
3-camera support with per-camera confidence zones.
"""

import math

# =============================================================================
# CAMERAS
# =============================================================================
CAM_INDEXES = [0, 2, 4]       # USB webcam indexes (legacy, overridé par --cams)
CAM_WIDTH = 1280
CAM_HEIGHT = 720
CAM_FPS = 20
# CAM_INDEXES sert encore en mode dev (`python main.py --cams 0 2 4`) et
# comme fallback si les symlinks udev ne sont pas présents (cf. CAM_SLOTS).
# En production le mapping passe par les paths stables `/dev/dart-cam-*`
# créés par les udev rules dans webui/udev/99-dart-cams.rules.

# Camera mounting positions: segment number where each camera sits.
# Used for confidence zone weighting.
# Set during calibration or via --cam-positions
CAM_POSITIONS = [20, 3, 11]   # Default guess, updated at calibration

# Mapping slot UI (A/B/C) → cam physique. Deux clés possibles :
#   - `device`: chemin V4L2 stable (`/dev/dart-cam-A`...) créé par udev.
#     C'est la voie de production : robuste face aux replug USB.
#   - `index`:  index V4L2 entier (legacy). Utilisé en fallback si le device
#     path n'existe pas (ex: udev rules pas installées en dev local), et
#     comme clé de stockage dans calibration.json.
#
# main.py essaye `device` d'abord, retombe sur `index` si le path est absent.
# `--cams 0 2 4` en CLI override CAM_INDEXES ET clear le device des slots
# (force le mode int-index, pour le dev sans udev).
CAM_SLOTS = [
    {"slot": "A", "device": "/dev/dart-cam-A", "index": 0, "master": False},
    {"slot": "B", "device": "/dev/dart-cam-B", "index": 2, "master": False},
    {"slot": "C", "device": "/dev/dart-cam-C", "index": 4, "master": True},
]

# =============================================================================
# WEB UI
# =============================================================================
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000
WEB_KIOSK_URL = "http://localhost:8000/"

# =============================================================================
# CALIBRATION
# =============================================================================
CALIB_FILE = "calibration.json"
WARP_SIZE = 800
WARP_CENTER = WARP_SIZE // 2
WARP_RADIUS = 360

# =============================================================================
# DART DETECTION
# =============================================================================
DIFF_THRESHOLD = 30
MIN_DART_AREA = 80
MAX_DART_AREA = 15000
STABLE_FRAMES = 8
COOLDOWN_FRAMES = 40

# Dead-time GLOBAL après un lancer scoré : aucun nouveau lancer ne peut être
# scoré pendant ce délai (toutes cams confondues). Laisse les détecteurs
# absorber la fléchette dans leur référence et évite qu'un mouvement juste
# après (bras qui se retire, joueur qui s'approche) soit pris pour un lancer.
# Les fléchettes d'un même tour sont lancées à plusieurs secondes d'intervalle,
# donc 1.5 s ne bloque pas le jeu normal.
INTER_THROW_COOLDOWN_S = 1.5
BLUR_KERNEL = (7, 7)
MORPH_KERNEL_SIZE = 5

# Duplicate detection: min pixel distance between two dart tips
DUPLICATE_MIN_DIST = 25

# Tip detection (contour morphology)
MIN_ELONGATION = 1.8          # Min length/width ratio to qualify as a dart contour
CONTOUR_GROUP_DIST = 35       # Max px distance to merge fragmented contours
TIP_REFINE_RADIUS = 12        # Px radius for tip sub-pixel refinement

# Choix du bout "pointe" du blob : la pointe est l'extrémité FINE
# (aiguille ~2px) et le flight l'extrémité LARGE. Indépendant de la
# position caméra (CAM_POSITIONS s'est avéré non fiable en prod).
TIP_WIDTH_RATIO = 1.25        # ratio large/fin mini pour trancher par la largeur

# Rejet des ombres : un contour n'est candidat que si le 90e percentile du
# diff à l'intérieur dépasse ce facteur × DIFF_THRESHOLD. Une ombre passe à
# peine le seuil partout ; une fléchette a un cœur très contrasté. (p90 et
# non la moyenne : la fermeture morpho inclut des pixels sous le seuil qui
# diluaient la moyenne et faisaient rejeter de vraies fléchettes.)
SHADOW_MEAN_DIFF_FACTOR = 1.5

# Auto-guérison de la référence : si le détecteur boucle en "confirming"
# sans jamais produire de candidat valide (board qui a vibré → anneau de
# bruit permanent sur le diff), on recapture la référence après N cycles
# improductifs au lieu de tourner en rond.
REF_STALE_CYCLES = 4

# Takeout (retrait des fléchettes en fin de tour).
# Après le 3e dart (ou next_turn forcé), la détection est suspendue jusqu'à
# ce que le retrait soit observé (activité puis stabilité), puis la référence
# est recapturée — les trous laissés par les pointes sont ainsi absorbés
# dans la nouvelle référence au lieu de générer de fausses détections.
TAKEOUT_ACTIVITY_MOTION = 1500   # px de motion inter-frame = main dans le champ
# ~12 cycles ≈ 1-1.5s de calme : assez pour que le bras soit reparti, sans
# bloquer le joueur suivant. (35 était trop : si le board ne devenait jamais
# calme 35 frames d'affilée — bruit cam, joueur qui traîne — le takeout
# restait coincé 35-70s et J2 ne pouvait pas scorer.)
TAKEOUT_STABLE_FRAMES = 12
TAKEOUT_MIN_DIFF_AREA = 160      # aire de diff vs ref attestant darts retirées
TAKEOUT_TIMEOUT_S = 8.0          # filet souple : sortie sur calme seul après ce délai
# Plafond DUR : on ne bloque JAMAIS le joueur suivant au-delà de ce délai,
# stabilité atteinte ou non. Garantit que J2 peut toujours scorer ~rapidement.
TAKEOUT_MAX_S = 16.0

# =============================================================================
# CONFIDENCE ZONES
# =============================================================================
# Each camera is most accurate for segments it faces directly.
# Confidence decays with angular distance from camera position.
# A camera facing segment X has max confidence at angle(X) and
# cos-based decay over angular distance.
CONFIDENCE_FALLOFF = 0.3      # Min confidence at 180° away (0.0 = ignore, 1.0 = no falloff)
CONFIDENCE_POWER = 1.5        # Exponent for falloff curve (>1 = sharper drop)

# =============================================================================
# DARTBOARD GEOMETRY
# =============================================================================
BULLSEYE_RADIUS = 6.35 / 170.0
BULL_RADIUS = 15.9 / 170.0
TRIPLE_INNER = 99.0 / 170.0
TRIPLE_OUTER = 107.0 / 170.0
DOUBLE_INNER = 162.0 / 170.0
DOUBLE_OUTER = 1.0

BOARD_ORDER = [20, 1, 18, 4, 13, 6, 10, 15, 2, 17, 3, 19, 7, 16, 8, 11, 14, 9, 12, 5]

# Precompute: angle (degrees, clockwise from top) for each segment center
SEGMENT_ANGLES = {}
for _i, _num in enumerate(BOARD_ORDER):
    SEGMENT_ANGLES[_num] = _i * 18.0

# =============================================================================
# MULTI-CAM FUSION
# =============================================================================
# When multiple cameras detect on the same frame cycle:
# - If tips agree (< FUSION_AGREE_DIST px), average them weighted by confidence
# - If tips disagree, take the one from the highest-confidence camera
# Nombre MINIMUM de caméras distinctes qui doivent corroborer un lancer pour
# qu'il soit scoré. À 2 (recommandé sur 3 cams) : un blob parasite vu par une
# SEULE caméra (main qui retire une fléchette, ombre, trou laissé par une
# pointe…) n'est plus scoré — il faut une vraie fléchette vue par ≥2 cams au
# même endroit. Mettre à 1 si une cam est souvent occultée et que de vrais
# lancers ne sont vus que par une seule (au prix du retour des faux positifs).
FUSION_MIN_CAMS = 2

FUSION_AGREE_DIST = 40        # Max px distance to consider "same dart"
# 1200ms : les cams se stabilisent à des moments différents (STABLE_FRAMES
# à des cadences effectives différentes). À 800ms la 2e cam arrivait souvent
# APRÈS la fusion → score "single" + info perdue.
FUSION_WINDOW_MS = 1200
# Après un lancer fusionné, toute détection à moins de cette distance du point
# scoré est ignorée quelques secondes : empêche les cams retardataires de
# re-scorer LA MÊME fléchette en "single" (vu en prod : 1 dart → 3 throws).
FUSION_SUPPRESS_DIST = 50
FUSION_SUPPRESS_S = 6.0   # 3.0 laissait passer un re-score à 4s (cam lente)

# Validation géométrique de l'intersection des rays. Sans ces gardes, les
# moindres carrés sortent TOUJOURS un point, même quand les rays sont
# incohérents (cam qui a détecté une ombre, mauvais axe PCA…) → scores
# fantômes validés avec une fausse confiance élevée.
FUSION_MIN_RAY_ANGLE = 12.0     # deg mini entre 2 rays (quasi-parallèles = instable)
FUSION_RAY_RESIDUAL_MAX = 25.0  # px : distance max ray↔point pour être "inlier"
FUSION_TIP_SUPPORT_SIGMA = 50.0 # px : échelle du vote des tips par cam
# Une intersection de paire n'est valable que si AU MOINS UNE des deux cams
# a son tip 2D près du point (un tip peut être décalé LE LONG de l'axe —
# mauvais bout choisi — mais deux rays dont AUCUN tip ne corrobore le point
# croisent probablement deux objets différents).
FUSION_MAX_TIP_GAP = 150.0

# =============================================================================
# DISPLAY
# =============================================================================
WINDOW_WIDTH = 1600
WINDOW_HEIGHT = 700
COLOR_GREEN = (0, 255, 0)
COLOR_RED = (0, 0, 255)
COLOR_BLUE = (255, 150, 0)
COLOR_WHITE = (255, 255, 255)
COLOR_YELLOW = (0, 255, 255)
COLOR_CYAN = (255, 255, 0)
COLOR_ORANGE = (0, 165, 255)
COLOR_MAGENTA = (255, 0, 255)
