"""
DartVision v3 - Multi-Camera Fusion
Combines detections from N cameras with confidence zone weighting.
Uses RAY INTERSECTION to triangulate dart tip from multiple camera axes.

Each camera has a "confidence zone" based on which segment it faces.
When multiple cameras detect simultaneously:
- Intersect their dart-axis rays to find the actual tip point
- Single camera: use the per-camera tip estimate as fallback
"""

import math
import time
from typing import List, Optional, Tuple
import numpy as np
import config
from board import compute_score, segment_angle, angular_distance


class CameraConfidence:
    """Computes per-camera confidence for a given board position."""

    def __init__(self, cam_position_segment: int):
        self.segment = cam_position_segment
        self.facing_angle = segment_angle(cam_position_segment)

    def confidence_at_angle(self, dart_angle: float) -> float:
        dist = angular_distance(self.facing_angle, dart_angle)
        norm_dist = dist / 180.0
        falloff = config.CONFIDENCE_FALLOFF
        power = config.CONFIDENCE_POWER
        confidence = 1.0 - (1.0 - falloff) * (norm_dist ** power)
        return max(0.0, min(1.0, confidence))

    def confidence_at_position(self, r_frac: float, angle: float) -> float:
        angle_conf = self.confidence_at_angle(angle)
        if r_frac <= config.BULL_RADIUS:
            return max(angle_conf, 0.8)
        edge_factor = min(r_frac / config.DOUBLE_OUTER, 1.0)
        return angle_conf * (0.5 + 0.5 * (1.0 - edge_factor * 0.3))


class Detection:
    """A single detection from one camera."""

    def __init__(self, cam_id: int, tip: Tuple[int, int],
                 ray: Optional[Tuple[Tuple[float, float], Tuple[float, float]]],
                 score_data: dict, diff_score: float,
                 confidence: float, timestamp: float):
        self.cam_id = cam_id
        self.tip = tip
        self.ray = ray  # ((x1,y1), (x2,y2)) or None
        self.score_data = score_data
        self.diff_score = diff_score
        self.confidence = confidence
        self.timestamp = timestamp


class FusionEngine:
    """
    Fuses detections from multiple cameras into a single result.
    Uses ray intersection when multiple cameras have axis data.
    """

    def __init__(self, cam_confidences: List[CameraConfidence]):
        self.cam_confidences = cam_confidences
        self.pending: List[Detection] = []
        self.last_fused_time = 0

    def add_detection(self, cam_id: int, tip: Tuple[int, int],
                      diff_score: float,
                      ray: Optional[Tuple] = None) -> Optional[dict]:
        """
        Add a detection from a camera. Returns fused result if ready, else None.
        """
        cx, cy = config.WARP_CENTER, config.WARP_CENTER
        radius = config.WARP_RADIUS

        score_data = compute_score(tip[0], tip[1], cx, cy, radius)
        score_data["cam_id"] = cam_id
        score_data["tip_px"] = tip

        if cam_id < len(self.cam_confidences):
            conf = self.cam_confidences[cam_id].confidence_at_position(
                score_data["r_frac"], score_data["angle"]
            )
        else:
            conf = 0.5

        now = time.time()
        det = Detection(cam_id, tip, ray, score_data, diff_score, conf, now)
        self.pending.append(det)

        n_cams = len(self.cam_confidences)
        cam_ids_pending = set(d.cam_id for d in self.pending)

        if len(cam_ids_pending) >= n_cams:
            return self._fuse()

        return None

    def try_fuse(self) -> Optional[dict]:
        if not self.pending:
            return None
        now = time.time()
        oldest = min(d.timestamp for d in self.pending)
        elapsed_ms = (now - oldest) * 1000
        if elapsed_ms >= config.FUSION_WINDOW_MS:
            return self._fuse()
        return None

    def _fuse(self) -> dict:
        if not self.pending:
            return None

        detections = self.pending
        self.pending = []
        self.last_fused_time = time.time()

        if len(detections) == 1:
            d = detections[0]
            d.score_data["fusion_confidence"] = d.confidence
            d.score_data["fusion_method"] = "single"
            d.score_data["fusion_cams"] = [d.cam_id]
            return d.score_data

        # --- Multiple cameras: try ray intersection first ---
        rays_with_conf = [(d.ray, d.confidence, d.cam_id)
                          for d in detections if d.ray is not None]

        if len(rays_with_conf) >= 2:
            # Try ray triangulation
            intersection = self._intersect_rays(rays_with_conf)
            if intersection is not None:
                return self._build_result_from_point(
                    intersection, detections, method="ray_intersect"
                )

        # Fallback to tip-based fusion
        groups = self._group_by_proximity(detections)
        if len(groups) == 1:
            return self._fuse_group(groups[0], method="agree")
        else:
            best_group = max(groups, key=lambda g: sum(d.confidence for d in g))
            return self._fuse_group(best_group, method="best_confidence")

    # -----------------------------------------------------------------
    # RAY INTERSECTION
    # -----------------------------------------------------------------
    def _intersect_rays(self, rays_with_conf):
        """
        Find the point that best fits the intersection of multiple rays.
        Each ray is ((x1,y1), (x2,y2)) with a confidence weight.

        For 2 rays: exact 2D line intersection.
        For 3+ rays: least-squares closest point to all lines.

        Returns (x, y) or None if intersection is invalid.
        """
        cx, cy = config.WARP_CENTER, config.WARP_CENTER

        if len(rays_with_conf) == 2:
            pt = self._intersect_two_lines(
                rays_with_conf[0][0], rays_with_conf[1][0]
            )
        else:
            pt = self._intersect_multiple_lines(rays_with_conf)

        if pt is None:
            return None

        # Validate: intersection must be on the board
        dist = math.sqrt((pt[0] - cx)**2 + (pt[1] - cy)**2)
        if dist > config.WARP_RADIUS * 1.1:
            return None

        return pt

    def _intersect_two_lines(self, ray1, ray2):
        """
        Intersect two 2D lines defined by two points each.
        ray = ((x1,y1), (x2,y2))
        Returns (x, y) or None if parallel.
        """
        (x1, y1), (x2, y2) = ray1
        (x3, y3), (x4, y4) = ray2

        # Direction vectors
        dx1, dy1 = x2 - x1, y2 - y1
        dx2, dy2 = x4 - x3, y4 - y3

        denom = dx1 * dy2 - dy1 * dx2
        if abs(denom) < 1e-6:
            return None  # Parallel lines

        t = ((x3 - x1) * dy2 - (y3 - y1) * dx2) / denom

        ix = x1 + t * dx1
        iy = y1 + t * dy1

        return (ix, iy)

    def _intersect_multiple_lines(self, rays_with_conf):
        """
        Least-squares intersection of multiple 2D lines.
        Finds the point that minimizes sum of squared distances to all lines,
        weighted by confidence.

        Each line: point P + direction D.
        Distance from point X to line: ||(X - P) - ((X-P)·D̂)D̂||
        Minimizing gives a linear system: (Σ wᵢ(I - D̂ᵢD̂ᵢᵀ)) X = Σ wᵢ(I - D̂ᵢD̂ᵢᵀ)Pᵢ
        """
        A = np.zeros((2, 2))
        b = np.zeros(2)

        for ray, conf, _ in rays_with_conf:
            (x1, y1), (x2, y2) = ray
            px, py = x1, y1
            dx, dy = x2 - x1, y2 - y1

            # Normalize direction
            length = math.sqrt(dx*dx + dy*dy)
            if length < 1e-6:
                continue
            dx /= length
            dy /= length

            # I - D̂D̂ᵀ (projection matrix onto perpendicular)
            # [[1-dx², -dx·dy], [-dx·dy, 1-dy²]]
            w = conf
            perp = np.array([
                [1 - dx*dx, -dx*dy],
                [-dx*dy, 1 - dy*dy]
            ])

            A += w * perp
            b += w * perp @ np.array([px, py])

        # Solve A @ X = b
        det = np.linalg.det(A)
        if abs(det) < 1e-6:
            return None  # Degenerate

        result = np.linalg.solve(A, b)
        return (float(result[0]), float(result[1]))

    def _build_result_from_point(self, point, detections, method):
        """Build score_data from an intersection point."""
        cx, cy = config.WARP_CENTER, config.WARP_CENTER
        radius = config.WARP_RADIUS

        fused = compute_score(point[0], point[1], cx, cy, radius)
        fused["tip_px"] = (int(point[0]), int(point[1]))
        fused["fusion_confidence"] = max(d.confidence for d in detections)
        fused["fusion_method"] = method
        fused["fusion_cams"] = [d.cam_id for d in detections]
        fused["cam_id"] = max(detections, key=lambda d: d.confidence).cam_id
        return fused

    # -----------------------------------------------------------------
    # LEGACY TIP-BASED FUSION (fallback)
    # -----------------------------------------------------------------
    def _group_by_proximity(self, detections: List[Detection]) -> List[List[Detection]]:
        groups = []
        used = [False] * len(detections)
        for i, d in enumerate(detections):
            if used[i]:
                continue
            group = [d]
            used[i] = True
            for j in range(i + 1, len(detections)):
                if used[j]:
                    continue
                dist = math.sqrt((d.tip[0] - detections[j].tip[0])**2 +
                                 (d.tip[1] - detections[j].tip[1])**2)
                if dist < config.FUSION_AGREE_DIST:
                    group.append(detections[j])
                    used[j] = True
            groups.append(group)
        return groups

    def _fuse_group(self, group: List[Detection], method: str) -> dict:
        total_weight = sum(d.confidence * d.diff_score for d in group)

        if total_weight <= 0:
            d = group[0]
            d.score_data["fusion_confidence"] = d.confidence
            d.score_data["fusion_method"] = method
            d.score_data["fusion_cams"] = [d.cam_id]
            return d.score_data

        avg_x = sum(d.tip[0] * d.confidence * d.diff_score for d in group) / total_weight
        avg_y = sum(d.tip[1] * d.confidence * d.diff_score for d in group) / total_weight

        fused = compute_score(
            avg_x, avg_y,
            config.WARP_CENTER, config.WARP_CENTER, config.WARP_RADIUS
        )
        fused["tip_px"] = (int(avg_x), int(avg_y))
        fused["fusion_confidence"] = max(d.confidence for d in group)
        fused["fusion_method"] = method
        fused["fusion_cams"] = [d.cam_id for d in group]
        fused["cam_id"] = max(group, key=lambda d: d.confidence).cam_id

        return fused
