"""
DartVision v3 - Configuration
3-camera support with per-camera confidence zones.
"""

import math

# =============================================================================
# CAMERAS
# =============================================================================
CAM_INDEXES = [0, 2, 4]       # USB webcam indexes (override with --cams)
CAM_WIDTH = 1280
CAM_HEIGHT = 720
CAM_FPS = 20
# Note : les index V4L2 dépendent de l'ordre d'énumération USB au boot du Pi.
# Sur cette config :
#   bus usb-xhci-hcd.0-1 → /dev/video0 (capture) + /dev/video1 (metadata)
#   bus usb-xhci-hcd.1-1 → /dev/video2 (capture) + /dev/video3 (metadata)
#   bus usb-xhci-hcd.0-2 → /dev/video4 (capture) + /dev/video5 (metadata)
# En cas de rebranchement à chaud, le mapping peut être transitoirement
# différent ; recheck avec `v4l2-ctl --list-devices` si une cam tombe.
# Solution structurelle : udev rules en STEP 4 (cf. webui/FUTURE.md).

# Camera mounting positions: segment number where each camera sits.
# Used for confidence zone weighting.
# Set during calibration or via --cam-positions
CAM_POSITIONS = [20, 3, 11]   # Default guess, updated at calibration

# Mapping slot UI (A/B/C) → index physique. Lu par main.py pour pousser
# les frames JPEG dans bridge.set_frame(slot, ...), et par webui pour
# afficher Cam-A/B/C dans l'interface. Modifiable sans toucher au code.
CAM_SLOTS = [
    {"slot": "A", "index": 0, "master": False},
    {"slot": "B", "index": 2, "master": False},
    {"slot": "C", "index": 4, "master": True},   # le design fanzine désigne C=master
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
