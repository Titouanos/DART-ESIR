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
        # Points récemment scorés : [(x, y, timestamp)]. Toute nouvelle
        # détection trop proche est LA MÊME fléchette vue en retard par une
        # autre cam (références désynchronisées) — on la jette au lieu de
        # re-scorer. Vu en prod : 1 dart physique → 3 throws en 4 s.
        self.recent_throws: List[Tuple[float, float, float]] = []

    def add_detection(self, cam_id: int, tip: Tuple[int, int],
                      diff_score: float,
                      ray: Optional[Tuple] = None) -> Optional[dict]:
        """
        Add a detection from a camera. Returns fused result if ready, else None.
        """
        cx, cy = config.WARP_CENTER, config.WARP_CENTER
        radius = config.WARP_RADIUS

        # Suppression cross-cam : même point qu'un lancer déjà scoré il y a
        # moins de FUSION_SUPPRESS_S secondes → cam retardataire, on ignore.
        now = time.time()
        self.recent_throws = [(x, y, t) for (x, y, t) in self.recent_throws
                              if now - t < config.FUSION_SUPPRESS_S]
        for (x, y, t) in self.recent_throws:
            if math.dist(tip, (x, y)) < config.FUSION_SUPPRESS_DIST:
                print(f"  [FUSION] cam {cam_id}: détection à {tip} ignorée "
                      f"(= lancer déjà scoré en ({x:.0f},{y:.0f}))")
                return None

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

        result = self._fuse_inner(detections)
        if result is not None:
            # Tips 2D par cam : diagnostic systématique de chaque score
            result["fusion_tips"] = {d.cam_id: list(d.tip) for d in detections}
            tx, ty = result["tip_px"]
            self.recent_throws.append((float(tx), float(ty), time.time()))
        return result

    def _fuse_inner(self, detections) -> Optional[dict]:
        # Corroboration multi-caméras : un lancer doit être vu par au moins
        # FUSION_MIN_CAMS caméras DISTINCTES. Élimine la cause n°1 de faux
        # scores — une seule cam qui voit un blob parasite (main au retrait,
        # ombre, trou de pointe) et le score en "single" après le timeout de
        # fusion. (FUSION_MIN_CAMS=1 rétablit l'ancien comportement.)
        distinct_cams = len(set(d.cam_id for d in detections))
        if distinct_cams < config.FUSION_MIN_CAMS:
            tips = {d.cam_id: list(d.tip) for d in detections}
            print(f"  [FUSION] REJET: {distinct_cams} cam(s) seulement "
                  f"(min {config.FUSION_MIN_CAMS}) — tips={tips}")
            return None

        if len(detections) == 1:
            d = detections[0]
            d.score_data["fusion_confidence"] = d.confidence
            d.score_data["fusion_method"] = "single"
            d.score_data["fusion_cams"] = [d.cam_id]
            return d.score_data

        # --- Multiple cameras: try ray intersection first ---
        ray_solution = self._intersect_rays(detections)
        if ray_solution is not None:
            point, inliers, residual = ray_solution
            return self._build_result_from_point(
                point, detections, method="ray_intersect",
                inliers=inliers, residual=residual,
            )

        # Fallback to tip-based fusion
        groups = self._group_by_proximity(detections)
        if len(groups) == 1:
            return self._fuse_group(groups[0], method="agree")

        best_group = max(groups, key=lambda g: sum(d.confidence for d in g))
        if len(best_group) == 1:
            # ≥2 cams, AUCUN consensus : ni paire de rays cohérente, ni deux
            # tips qui s'accordent. Scorer reviendrait à choisir au hasard
            # parmi des estimations contradictoires → on rejette et on logue.
            tips = {d.cam_id: d.tip for d in detections}
            print(f"  [FUSION] REJET: {len(detections)} cams incohérentes, "
                  f"tips={tips}")
            return None

        result = self._fuse_group(best_group, method="best_confidence")
        # Cams en désaccord : la confiance doit le refléter au lieu
        # d'afficher la simple confiance angulaire de la meilleure cam.
        result["fusion_confidence"] *= len(best_group) / len(detections)
        return result

    # -----------------------------------------------------------------
    # RAY INTERSECTION
    # -----------------------------------------------------------------
    @staticmethod
    def _ray_dir(ray):
        (x1, y1), (x2, y2) = ray
        dx, dy = x2 - x1, y2 - y1
        n = math.sqrt(dx * dx + dy * dy)
        if n < 1e-6:
            return None
        return (dx / n, dy / n)

    @staticmethod
    def _perp_dist(pt, ray):
        """Distance perpendiculaire d'un point à la DROITE portée par ray."""
        (x1, y1), (x2, y2) = ray
        dx, dy = x2 - x1, y2 - y1
        n = math.sqrt(dx * dx + dy * dy)
        if n < 1e-6:
            return float("inf")
        return abs((pt[0] - x1) * dy - (pt[1] - y1) * dx) / n

    def _intersect_rays(self, detections):
        """
        Triangulation validée géométriquement (RANSAC-lite).

        L'ancienne version résolvait des moindres carrés sur TOUS les rays
        sans aucun contrôle : un seul ray pourri (ombre, mauvais axe PCA)
        suffisait à déplacer la solution n'importe où sur le board, et elle
        était quand même acceptée. Ici :
          1. candidates = intersections de chaque PAIRE de rays d'angle
             suffisant (quasi-parallèles → instables → exclus), sur le board ;
          2. chaque candidate est notée par le nombre de rays inliers
             (distance perpendiculaire < FUSION_RAY_RESIDUAL_MAX) puis par le
             vote des tips 2D par cam (pondéré confiance, décroissance exp) ;
          3. la solution est raffinée par moindres carrés sur les inliers.

        Returns (point, inlier_detections, residual_px) ou None si aucune
        paire cohérente (le fallback tip-based prend alors la main).
        """
        cx, cy = config.WARP_CENTER, config.WARP_CENTER
        ray_dets = [d for d in detections if d.ray is not None]
        if len(ray_dets) < 2:
            return None

        min_angle = math.radians(config.FUSION_MIN_RAY_ANGLE)
        candidates = []
        for i in range(len(ray_dets)):
            for j in range(i + 1, len(ray_dets)):
                di = self._ray_dir(ray_dets[i].ray)
                dj = self._ray_dir(ray_dets[j].ray)
                if di is None or dj is None:
                    continue
                cross = abs(di[0] * dj[1] - di[1] * dj[0])
                if cross < math.sin(min_angle):
                    continue   # quasi-parallèles : intersection instable
                pt = self._intersect_two_lines(ray_dets[i].ray, ray_dets[j].ray)
                if pt is None:
                    continue
                if math.dist(pt, (cx, cy)) > config.WARP_RADIUS * 1.1:
                    continue
                # Corroboration : au moins un des deux tips de la paire doit
                # être proche du point (sinon = croisement de 2 objets
                # différents, p.ex. la fléchette sur une cam et un bout de
                # bras/ombre sur l'autre).
                gap = min(math.dist(pt, ray_dets[i].tip),
                          math.dist(pt, ray_dets[j].tip))
                if gap > config.FUSION_MAX_TIP_GAP:
                    continue
                candidates.append(pt)

        if not candidates:
            return None

        def support(pt):
            inl = sum(1 for d in ray_dets
                      if self._perp_dist(pt, d.ray) < config.FUSION_RAY_RESIDUAL_MAX)
            sigma = config.FUSION_TIP_SUPPORT_SIGMA
            votes = sum(d.confidence * math.exp(-math.dist(pt, d.tip) / sigma)
                        for d in detections)
            return (inl, votes)

        best_pt = max(candidates, key=support)
        inliers = [d for d in ray_dets
                   if self._perp_dist(best_pt, d.ray) < config.FUSION_RAY_RESIDUAL_MAX]

        # Raffinement moindres carrés sur les inliers uniquement (≥3 rays)
        if len(inliers) >= 3:
            refined = self._intersect_multiple_lines(
                [(d.ray, d.confidence, d.cam_id) for d in inliers])
            if refined is not None and \
               math.dist(refined, (cx, cy)) <= config.WARP_RADIUS * 1.1:
                best_pt = refined

        residual = max(self._perp_dist(best_pt, d.ray) for d in inliers)
        return best_pt, inliers, residual

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

    def _build_result_from_point(self, point, detections, method,
                                  inliers=None, residual=None):
        """Build score_data from an intersection point."""
        cx, cy = config.WARP_CENTER, config.WARP_CENTER
        radius = config.WARP_RADIUS

        used = inliers if inliers else detections
        fused = compute_score(point[0], point[1], cx, cy, radius)
        fused["tip_px"] = (int(point[0]), int(point[1]))
        conf = max(d.confidence for d in used)
        if residual is not None:
            # La cohérence géométrique module la confiance affichée :
            # résidu 0 → ×1.0, résidu = RESIDUAL_MAX → ×~0.6
            conf *= math.exp(-residual / (2 * config.FUSION_RAY_RESIDUAL_MAX))
            fused["fusion_residual_px"] = round(residual, 1)
        fused["fusion_confidence"] = conf
        fused["fusion_method"] = method
        fused["fusion_cams"] = [d.cam_id for d in used]
        fused["cam_id"] = max(used, key=lambda d: d.confidence).cam_id
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
