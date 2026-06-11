"""
DartVision v3 - Dart Detector
Frame differencing + ray-based tip finding.
Each detector extracts a ray along the dart axis.
Fusion intersects rays from multiple cameras to find the tip.
"""

import cv2
import numpy as np
import math
import config


class DartDetector:

    def __init__(self, cam_id: int = 0):
        self.cam_id = cam_id
        self.reference = None
        self.prev_gray = None
        self.stable_count = 0
        self.cooldown = 0
        self.state = "idle"
        self.candidate_tip = None
        self.candidate_ray = None   # ((x1,y1),(x2,y2)) axis ray
        self.candidate_contour = None
        self.last_detection = None
        self.all_detections = []
        self.dart_count = 0
        self.debug_mask = None
        self._last_diff_score = 0
        # Compteurs de rejets de contours (affichés sur le flux debug pour
        # comprendre POURQUOI "darts: 0" alors que le diff montre un blob)
        self.last_rejections = {"small": 0, "big": 0, "shadow": 0}
        # Cycles de confirmation improductifs consécutifs (diff persistant
        # mais aucun candidat valide) → auto-recapture de référence
        self._unproductive_cycles = 0
        # Direction estimée board→caméra (vecteur unitaire), apprise sur les
        # détections dont le profil de largeur est non-ambigu : le flight se
        # projette à l'opposé de la cam, donc flight→pointe pointe vers elle.
        # Survit à reset() : la caméra ne bouge pas entre les parties.
        self.cam_bearing = None

    def set_reference(self, frame):
        self.reference = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.reference = cv2.GaussianBlur(self.reference, config.BLUR_KERNEL, 0)
        self.prev_gray = self.reference.copy()
        self.state = "idle"
        self.stable_count = 0
        self.cooldown = 0
        self._unproductive_cycles = 0

    def reset(self):
        self.reference = None
        self.prev_gray = None
        self.stable_count = 0
        self.cooldown = 0
        self.state = "idle"
        self.candidate_tip = None
        self.candidate_ray = None
        self.candidate_contour = None
        self.last_detection = None
        self.all_detections = []
        self.dart_count = 0
        self._last_diff_score = 0

    def enter_takeout(self):
        """Suspend la détection le temps que le joueur retire ses fléchettes.

        En takeout, process_frame continue de mesurer motion/diff (exposés
        dans le result pour que le contrôleur décide quand recapturer la
        référence) mais ne déclenche plus aucune détection — sinon les
        silhouettes des darts retirées et les trous laissés par les pointes
        passent pour de nouvelles fléchettes.
        """
        self.state = "takeout"
        self.stable_count = 0
        self.cooldown = 0
        self.candidate_tip = None
        self.candidate_ray = None
        self.candidate_contour = None

    def start_new_turn(self):
        """Oublie les détections du tour précédent (anti-doublon par tour).

        Sans ça, _is_duplicate finit par interdire tout impact à moins de
        DUPLICATE_MIN_DIST px d'une fléchette d'un tour passé.
        """
        self.all_detections = []
        self.dart_count = 0
        self.last_detection = None

    def process_frame(self, frame) -> dict:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Apply CLAHE to boost contrast of the darts against dark/light segments
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        gray = cv2.GaussianBlur(gray, config.BLUR_KERNEL, 0)

        result = {
            "state": self.state, "tip": None, "ray": None,
            "mask": None, "diff_score": 0.0, "contours": [],
            "motion": 0,
        }

        if self.reference is None:
            self.set_reference(frame)
            return result

        if self.cooldown > 0:
            self.cooldown -= 1
            self.prev_gray = gray.copy()
            result["state"] = "cooldown"
            return result

        # Board mask
        board_mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.circle(board_mask, (config.WARP_CENTER, config.WARP_CENTER),
                   int(config.WARP_RADIUS * 1.08), 255, -1)

        # Diff from reference
        diff = cv2.absdiff(gray, self.reference)
        diff = cv2.bitwise_and(diff, board_mask)
        _, thresh = cv2.threshold(diff, config.DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)

        k_sm = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        k_md = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, k_sm, iterations=1)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k_md, iterations=2)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, k_sm, iterations=1)

        self.debug_mask = thresh.copy()
        result["mask"] = thresh

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rej = {"small": 0, "big": 0, "shadow": 0}
        valid = []
        for c in contours:
            a = cv2.contourArea(c)
            if a <= config.MIN_DART_AREA:
                rej["small"] += 1
            elif a >= config.MAX_DART_AREA:
                rej["big"] += 1
            elif self._is_shadow(c, diff):
                rej["shadow"] += 1
            else:
                valid.append(c)
        self.last_rejections = rej

        result["contours"] = valid
        total_area = sum(cv2.contourArea(c) for c in valid)
        result["diff_score"] = total_area
        self._last_diff_score = total_area

        # Inter-frame motion
        motion = 0
        if self.prev_gray is not None:
            fd = cv2.absdiff(gray, self.prev_gray)
            _, ft = cv2.threshold(fd, 20, 255, cv2.THRESH_BINARY)
            motion = cv2.countNonZero(cv2.bitwise_and(ft, board_mask))
        self.prev_gray = gray.copy()
        result["motion"] = motion

        # State machine
        if self.state == "takeout":
            # Détection suspendue : le contrôleur observe motion/diff_score
            # et recapturera la référence quand le retrait est terminé.
            result["state"] = "takeout"
            return result

        if self.state == "idle":
            if total_area > config.MIN_DART_AREA and valid:
                self.state = "motion"
                self.stable_count = 0

        elif self.state == "motion":
            if total_area < config.MIN_DART_AREA:
                self.state = "idle"
                self.stable_count = 0
            elif motion < 800:
                self.state = "confirming"
                self.stable_count = 1
                self._find_dart_tip(valid)

        elif self.state == "confirming":
            if motion > 3000:
                self.state = "motion"
                self.stable_count = 0
            elif total_area < config.MIN_DART_AREA:
                self.state = "idle"
                self.stable_count = 0
            else:
                self.stable_count += 1
                self._find_dart_tip(valid)

                if self.stable_count >= config.STABLE_FRAMES:
                    if self.candidate_tip and not self._is_duplicate(self.candidate_tip):
                        self.dart_count += 1
                        self.last_detection = self.candidate_tip
                        self.all_detections.append(self.candidate_tip)
                        result["tip"] = self.candidate_tip
                        result["ray"] = self.candidate_ray
                        result["state"] = "detected"
                        self.reference = gray.copy()
                        self._unproductive_cycles = 0
                    else:
                        # Diff persistant mais rien de scorable : board qui a
                        # vibré (anneau de bruit), reflet… Après N cycles à
                        # vide on resynchronise la référence au lieu de
                        # boucler en motion/confirming indéfiniment.
                        self._unproductive_cycles += 1
                        if self._unproductive_cycles >= config.REF_STALE_CYCLES:
                            print(f"[DETECTOR cam{self.cam_id}] diff persistant "
                                  f"sans candidat ({self._unproductive_cycles} cycles) "
                                  f"→ recapture auto de la référence")
                            self.reference = gray.copy()
                            self._unproductive_cycles = 0

                    self.cooldown = config.COOLDOWN_FRAMES
                    self.stable_count = 0
                    self.state = "idle"
                    return result

        result["state"] = self.state
        return result

    def confirm_detection(self, frame):
        """Called externally after fusion accepts this detection. Updates reference."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        gray = cv2.GaussianBlur(gray, config.BLUR_KERNEL, 0)
        self.reference = gray

    def _is_duplicate(self, tip, min_dist=None):
        min_dist = min_dist or config.DUPLICATE_MIN_DIST
        for prev in self.all_detections:
            d = math.sqrt((tip[0] - prev[0])**2 + (tip[1] - prev[1])**2)
            if d < min_dist:
                return True
        return False

    def _is_shadow(self, contour, diff):
        """Un contour d'ombre passe à peine DIFF_THRESHOLD partout ; une
        fléchette a un cœur très contrasté. On compare le 90e percentile du
        diff dans le contour au seuil (p90 et non la moyenne : la fermeture
        morpho inclut des pixels sous le seuil qui diluent la moyenne et
        faisaient rejeter de vraies fléchettes)."""
        x, y, w, h = cv2.boundingRect(contour)
        sub = np.zeros((h, w), dtype=np.uint8)
        cv2.drawContours(sub, [contour - [x, y]], -1, 255, -1)
        vals = diff[y:y+h, x:x+w][sub > 0]
        if len(vals) == 0:
            return True
        p90 = float(np.percentile(vals, 90))
        return p90 < config.DIFF_THRESHOLD * config.SHADOW_MEAN_DIFF_FACTOR

    def _anchor_point(self):
        """Point d'ancrage 'côté caméra' pour départager les cas ambigus.

        Utilise le bearing APPRIS sur les détections passées (cf.
        _update_bearing) ; tant qu'on ne sait rien, retombe sur le centre du
        board. On ne se fie PAS à config.CAM_POSITIONS : les valeurs en prod
        se sont avérées être les défauts, jamais les vraies positions.
        """
        if self.cam_bearing is not None:
            r = config.WARP_RADIUS * 1.5
            return (config.WARP_CENTER + self.cam_bearing[0] * r,
                    config.WARP_CENTER + self.cam_bearing[1] * r)
        return (float(config.WARP_CENTER), float(config.WARP_CENTER))

    def _update_bearing(self, vec):
        """EMA de la direction board→caméra (vec = flight→pointe, unitaire)."""
        if self.cam_bearing is None:
            bx, by = vec
        else:
            bx = 0.7 * self.cam_bearing[0] + 0.3 * vec[0]
            by = 0.7 * self.cam_bearing[1] + 0.3 * vec[1]
        n = math.sqrt(bx * bx + by * by)
        if n > 1e-6:
            self.cam_bearing = (bx / n, by / n)

    def _find_dart_tip(self, contours):
        """
        Contour-morphology-based tip + ray finding:
        1. Group nearby contours (handles fragmented detections)
        2. Fit ellipse / minAreaRect to find dart axis (ray) and tip
        3. Store both candidate_tip and candidate_ray for fusion
        """
        if self.debug_mask is None or not contours:
            return

        cx, cy = config.WARP_CENTER, config.WARP_CENTER

        # --- Step 1: Group nearby contours ---
        grouped = self._group_contours(contours)

        # --- Step 2: Analyze each group by morphology ---
        best_tip = None
        best_ray = None
        best_score = -1
        best_contour = None

        for group in grouped:
            merged = np.vstack(group)
            # Use convex hull for valid contour operations
            hull = cv2.convexHull(merged)
            area = cv2.contourArea(hull)
            if area < config.MIN_DART_AREA:
                continue

            # PCA et profil de largeur sur les points d'outline bruts (le hull
            # est trop clairsemé pour estimer la largeur près des extrémités)
            tip, ray, score, elongation = self._analyze_contour_shape(
                merged, cx, cy, hull_area=area)
            if tip is not None and score > best_score:
                best_tip = tip
                best_ray = ray
                best_score = score
                best_contour = hull

        # --- Step 3: Fallback to closest-pixel if morphology failed ---
        if best_tip is None:
            best_tip = self._fallback_closest_pixel(cx, cy)
            if best_tip is None:
                return
            best_contour = max(contours, key=cv2.contourArea) if contours else None
            # Fallback ray: centroid of largest contour → board center
            if best_contour is not None:
                M = cv2.moments(best_contour)
                if M["m00"] > 0:
                    mcx = M["m10"] / M["m00"]
                    mcy = M["m01"] / M["m00"]
                    best_ray = ((mcx, mcy), (float(cx), float(cy)))

        # --- Step 4: Refine tip with local pixel analysis ---
        best_tip = self._refine_tip(best_tip, cx, cy)

        # Validate: skip if too far outside board (allow up to 1.08 to detect MISSes)
        tip_dist = math.sqrt((best_tip[0] - cx)**2 + (best_tip[1] - cy)**2)
        if tip_dist > config.WARP_RADIUS * 1.08:
            return

        self.candidate_tip = best_tip
        self.candidate_ray = best_ray
        if best_contour is not None:
            self.candidate_contour = best_contour

    def _group_contours(self, contours):
        """Group contours that are close to each other (fragmented dart)."""
        if not contours:
            return []

        # Compute centroids
        centroids = []
        for c in contours:
            M = cv2.moments(c)
            if M["m00"] > 0:
                centroids.append((M["m10"]/M["m00"], M["m01"]/M["m00"]))
            else:
                x, y, w, h = cv2.boundingRect(c)
                centroids.append((x + w/2, y + h/2))

        n = len(contours)
        used = [False] * n
        groups = []

        for i in range(n):
            if used[i]:
                continue
            group = [contours[i]]
            used[i] = True
            for j in range(i + 1, n):
                if used[j]:
                    continue
                d = math.sqrt((centroids[i][0] - centroids[j][0])**2 +
                              (centroids[i][1] - centroids[j][1])**2)
                if d < config.CONTOUR_GROUP_DIST:
                    group.append(contours[j])
                    used[j] = True
            groups.append(group)

        return groups

    def _analyze_contour_shape(self, contour, cx, cy, hull_area=None):
        """
        PCA-based axis extraction — robust and angle-convention-free.
        Le bout "pointe" est choisi par le PROFIL DE LARGEUR : l'aiguille est
        fine (~2px), le flight est large. Ne dépend d'aucune position caméra.
        Returns (tip, ray, quality_score, elongation).
        """
        pts = contour.reshape(-1, 2).astype(np.float64)
        n = len(pts)

        if n < 3:
            return self._tip_from_closest_contour_point(contour, cx, cy)

        # --- PCA: find principal axis ---
        mean = np.mean(pts, axis=0)  # centroid
        centered = pts - mean
        cov = np.cov(centered.T)  # 2x2 covariance matrix

        eigenvalues, eigenvectors = np.linalg.eigh(cov)
        # eigh returns sorted ascending; last = largest variance = major axis
        major_axis = eigenvectors[:, -1]  # (dx, dy) unit-ish vector
        minor_axis = eigenvectors[:, 0]
        minor_val = max(eigenvalues[0], 1e-6)
        major_val = max(eigenvalues[1], 1e-6)

        elongation = math.sqrt(major_val / minor_val)
        area = hull_area if hull_area is not None else cv2.contourArea(contour)

        # Normalize direction vector
        axis_len = math.sqrt(major_axis[0]**2 + major_axis[1]**2)
        if axis_len < 1e-6:
            return self._tip_from_closest_contour_point(contour, cx, cy)
        dir_x = major_axis[0] / axis_len
        dir_y = major_axis[1] / axis_len

        # ---- NON-ELONGATED (frontal view) ----
        if elongation < config.MIN_ELONGATION:
            return self._tip_from_closest_contour_point(contour, cx, cy)

        # ---- ELONGATED: project contour points onto major axis ----
        projections = centered @ major_axis
        perp = centered @ minor_axis
        min_proj = float(np.min(projections))
        max_proj = float(np.max(projections))

        # Two extremes along the major axis
        end1 = (mean[0] + dir_x * max_proj, mean[1] + dir_y * max_proj)
        end2 = (mean[0] + dir_x * min_proj, mean[1] + dir_y * min_proj)

        # Largeur moyenne (|perp|) près de chaque extrémité (30% du span).
        zone = 0.30 * (max_proj - min_proj)
        hi = projections > max_proj - zone
        lo = projections < min_proj + zone
        w_hi = float(np.mean(np.abs(perp[hi]))) if np.any(hi) else 0.0
        w_lo = float(np.mean(np.abs(perp[lo]))) if np.any(lo) else 0.0

        w_thin, w_wide = min(w_hi, w_lo), max(w_hi, w_lo)
        r1 = math.dist(end1, (cx, cy))
        r2 = math.dist(end2, (cx, cy))
        rim = config.WARP_RADIUS * 1.02
        if (r1 > rim) != (r2 > rim):
            # Un seul bout sort du board : c'est le flight/fût qui continue
            # hors du plan (la pointe d'une fléchette plantée est sur le
            # board). Signal géométrique fort, prioritaire sur la largeur
            # (le diff peut tronquer le flight et fausser le profil).
            tip_f, other = (end2, end1) if r1 > rim else (end1, end2)
            vx, vy = tip_f[0] - other[0], tip_f[1] - other[1]
            vn = math.sqrt(vx * vx + vy * vy)
            if vn > 1e-6:
                self._update_bearing((vx / vn, vy / vn))
        elif w_wide >= config.TIP_WIDTH_RATIO * max(w_thin, 0.5):
            # Profil net : la pointe est le bout FIN
            tip_f, other = (end1, end2) if w_hi < w_lo else (end2, end1)
            # Apprend la direction de la caméra : flight→pointe pointe vers elle
            vx, vy = tip_f[0] - other[0], tip_f[1] - other[1]
            vn = math.sqrt(vx * vx + vy * vy)
            if vn > 1e-6:
                self._update_bearing((vx / vn, vy / vn))
        else:
            # Ambigu (blob tronqué, aiguille hors masque…) : départage par le
            # bearing appris (ou le centre du board en tout début de session)
            ax, ay = self._anchor_point()
            d1 = math.dist(end1, (ax, ay))
            d2 = math.dist(end2, (ax, ay))
            tip_f = end1 if d1 < d2 else end2
        tip = (int(tip_f[0]), int(tip_f[1]))

        # Ray: anchor at the tip (instead of centroid) to avoid flight bias,
        # and extend along the major axis
        ray_len = config.WARP_RADIUS * 2.0
        ray_end1 = (tip[0] + dir_x * ray_len, tip[1] + dir_y * ray_len)
        ray_end2 = (tip[0] - dir_x * ray_len, tip[1] - dir_y * ray_len)
        ray = (ray_end1, ray_end2)

        tip_dist = math.sqrt((tip[0] - cx)**2 + (tip[1] - cy)**2)
        score = elongation * math.log1p(area) * (1.0 + 100.0 / max(tip_dist, 1))

        return tip, ray, score, elongation

    def _tip_from_closest_contour_point(self, contour, cx, cy):
        """
        For round/small contours: tip = contour point closest to the camera
        side (learned bearing, see _anchor_point).
        Ray = centroid → board center direction.
        Returns (tip, ray, score, elongation=1.0).
        """
        pts = contour.reshape(-1, 2)
        if len(pts) == 0:
            return None, None, 0, 0

        ax, ay = self._anchor_point()
        dx = pts[:, 0].astype(float) - ax
        dy = pts[:, 1].astype(float) - ay
        dists = np.sqrt(dx*dx + dy*dy)
        min_idx = np.argmin(dists)
        tip = (int(pts[min_idx, 0]), int(pts[min_idx, 1]))

        # Ray: centroid → board center (extended)
        M = cv2.moments(contour)
        if M["m00"] > 0:
            mcx = M["m10"] / M["m00"]
            mcy = M["m01"] / M["m00"]
        else:
            mcx, mcy = float(tip[0]), float(tip[1])

        ray = ((mcx, mcy), (float(cx), float(cy)))

        area = cv2.contourArea(contour) if len(pts) >= 3 else 1.0
        tip_dist = dists[min_idx]
        score = math.log1p(max(area, 1)) * (1.0 + 50.0 / max(tip_dist, 1))

        return tip, ray, score, 1.0

    def _fallback_closest_pixel(self, cx, cy):
        """Fallback: white pixel closest to the camera (within board bounds)."""
        white_pixels = cv2.findNonZero(self.debug_mask)
        if white_pixels is None or len(white_pixels) == 0:
            return None

        pixels = white_pixels.reshape(-1, 2)
        dx = pixels[:, 0].astype(float) - cx
        dy = pixels[:, 1].astype(float) - cy
        center_dists = np.sqrt(dx*dx + dy*dy)
        mask = center_dists < config.WARP_RADIUS * 1.08

        if not np.any(mask):
            return None

        pixels = pixels[mask]
        ax, ay = self._anchor_point()
        dxa = pixels[:, 0].astype(float) - ax
        dya = pixels[:, 1].astype(float) - ay
        cam_dists = np.sqrt(dxa*dxa + dya*dya)

        min_idx = np.argmin(cam_dists)
        return (int(pixels[min_idx, 0]), int(pixels[min_idx, 1]))

    def _refine_tip(self, tip, cx, cy):
        """
        Refine tip position using local white pixel density.
        Average the white pixels in a small radius around the candidate,
        weighted by their distance to center (closer = higher weight).
        """
        if self.debug_mask is None:
            return tip

        r = config.TIP_REFINE_RADIUS
        h, w = self.debug_mask.shape

        # Extract local region
        x1 = max(0, tip[0] - r)
        y1 = max(0, tip[1] - r)
        x2 = min(w, tip[0] + r + 1)
        y2 = min(h, tip[1] + r + 1)

        local = self.debug_mask[y1:y2, x1:x2]
        local_pts = cv2.findNonZero(local)

        if local_pts is None or len(local_pts) < 3:
            return tip

        pts = local_pts.reshape(-1, 2).astype(float)
        # Convert to global coords
        pts[:, 0] += x1
        pts[:, 1] += y1

        # Centroïde local pondéré vers le candidat lui-même : lisse le bruit
        # de masque sans introduire de biais directionnel (l'ancien poids vers
        # le centre du board tirait la pointe vers le flight selon le côté).
        dx = pts[:, 0] - tip[0]
        dy = pts[:, 1] - tip[1]
        dists = np.sqrt(dx*dx + dy*dy)
        weights = 1.0 / (dists + 1.0)  # Avoid div by zero

        tip_x = int(np.average(pts[:, 0], weights=weights))
        tip_y = int(np.average(pts[:, 1], weights=weights))

        return (tip_x, tip_y)
