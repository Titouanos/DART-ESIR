"""
DartVision v3 - Dartboard Geometry & Scoring
"""

import math
import cv2
import numpy as np
import config


def get_segment_number(angle_deg: float) -> int:
    """Angle in degrees (0=top, clockwise) -> segment number."""
    angle_deg = angle_deg % 360
    shifted = (angle_deg + 9) % 360
    index = int(shifted // 18)
    return config.BOARD_ORDER[index]


def get_ring(radius_fraction: float) -> str:
    """Radius fraction (0=center, 1=edge) -> ring name."""
    if radius_fraction < 0:
        return "outside"
    if radius_fraction <= config.BULLSEYE_RADIUS:
        return "double_bull"
    if radius_fraction <= config.BULL_RADIUS:
        return "single_bull"
    if radius_fraction <= config.TRIPLE_INNER:
        return "inner_single"
    if radius_fraction <= config.TRIPLE_OUTER:
        return "triple"
    if radius_fraction <= config.DOUBLE_INNER:
        return "outer_single"
    if radius_fraction <= config.DOUBLE_OUTER:
        return "double"
    return "outside"


def pixel_to_polar(x, y, cx, cy, radius):
    """Pixel coords -> (radius_fraction, angle_degrees). 0°=top, CW."""
    dx = x - cx
    dy = y - cy
    dist = math.sqrt(dx * dx + dy * dy)
    r_frac = dist / radius if radius > 0 else 999
    angle_rad = math.atan2(dx, -dy)
    angle_deg = math.degrees(angle_rad) % 360
    return r_frac, angle_deg


def compute_score(x, y, cx, cy, radius) -> dict:
    """Compute dart score from pixel position on warped image."""
    r_frac, angle = pixel_to_polar(x, y, cx, cy, radius)
    ring = get_ring(r_frac)

    if ring == "outside":
        return {"number": 0, "multiplier": 0, "ring": ring,
                "score": 0, "label": "MISS", "r_frac": r_frac, "angle": angle}
    if ring == "double_bull":
        return {"number": 25, "multiplier": 2, "ring": ring,
                "score": 50, "label": "D-BULL", "r_frac": r_frac, "angle": angle}
    if ring == "single_bull":
        return {"number": 25, "multiplier": 1, "ring": ring,
                "score": 25, "label": "S-BULL", "r_frac": r_frac, "angle": angle}

    number = get_segment_number(angle)
    if ring == "triple":
        return {"number": number, "multiplier": 3, "ring": ring,
                "score": number * 3, "label": f"T{number}",
                "r_frac": r_frac, "angle": angle}
    if ring == "double":
        return {"number": number, "multiplier": 2, "ring": ring,
                "score": number * 2, "label": f"D{number}",
                "r_frac": r_frac, "angle": angle}
    return {"number": number, "multiplier": 1, "ring": ring,
            "score": number, "label": f"S{number}",
            "r_frac": r_frac, "angle": angle}


def segment_angle(segment_number: int) -> float:
    """Get the center angle (degrees, CW from top) for a segment number."""
    return config.SEGMENT_ANGLES.get(segment_number, 0.0)


def angular_distance(a1: float, a2: float) -> float:
    """Shortest angular distance in degrees between two angles."""
    d = abs(a1 - a2) % 360
    return d if d <= 180 else 360 - d


def draw_board_overlay(img, cx, cy, radius):
    """Draw semi-transparent dartboard grid overlay."""
    overlay = img.copy()

    rings = [
        (config.BULLSEYE_RADIUS, (0, 0, 255)),
        (config.BULL_RADIUS, (0, 255, 0)),
        (config.TRIPLE_INNER, (200, 200, 200)),
        (config.TRIPLE_OUTER, (0, 100, 255)),
        (config.DOUBLE_INNER, (200, 200, 200)),
        (config.DOUBLE_OUTER, (0, 255, 0)),
    ]
    for r_frac, color in rings:
        cv2.circle(overlay, (int(cx), int(cy)), int(r_frac * radius), color, 2)

    for i in range(20):
        angle_deg = i * 18 - 9
        angle_rad = math.radians(angle_deg)
        x_end = cx + radius * math.sin(angle_rad)
        y_end = cy - radius * math.cos(angle_rad)
        x_bull = cx + config.BULL_RADIUS * radius * math.sin(angle_rad)
        y_bull = cy - config.BULL_RADIUS * radius * math.cos(angle_rad)
        cv2.line(overlay, (int(x_bull), int(y_bull)),
                 (int(x_end), int(y_end)), (200, 200, 200), 2)

    for i, num in enumerate(config.BOARD_ORDER):
        angle_rad = math.radians(i * 18)
        label_r = radius * 1.08
        x_l = cx + label_r * math.sin(angle_rad)
        y_l = cy - label_r * math.cos(angle_rad)
        cv2.putText(overlay, str(num), (int(x_l) - 10, int(y_l) + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

    cv2.addWeighted(overlay, 0.5, img, 0.5, 0, img)
    return img
