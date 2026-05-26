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
BLUR_KERNEL = (7, 7)
MORPH_KERNEL_SIZE = 5

# Duplicate detection: min pixel distance between two dart tips
DUPLICATE_MIN_DIST = 25

# Tip detection (contour morphology)
MIN_ELONGATION = 1.8          # Min length/width ratio to qualify as a dart contour
CONTOUR_GROUP_DIST = 35       # Max px distance to merge fragmented contours
TIP_REFINE_RADIUS = 12        # Px radius for tip sub-pixel refinement

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
FUSION_AGREE_DIST = 40        # Max px distance to consider "same dart"
FUSION_WINDOW_MS = 500        # Time window to group detections from different cams

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
