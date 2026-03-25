"""
DartVision v3 - Camera Calibration
Opens ONE camera at a time to avoid USB bandwidth issues.
"""

import json
import os
import math
import cv2
import numpy as np
import config


CALIB_WINDOW = "DartVision Calibration"

REFERENCE_LABELS = ["20 (top)", "6 (right)", "3 (bottom)", "11 (left)"]

IDEAL_POINTS = np.float32([
    [config.WARP_CENTER, config.WARP_CENTER - config.WARP_RADIUS],
    [config.WARP_CENTER + config.WARP_RADIUS, config.WARP_CENTER],
    [config.WARP_CENTER, config.WARP_CENTER + config.WARP_RADIUS],
    [config.WARP_CENTER - config.WARP_RADIUS, config.WARP_CENTER],
])


class Calibrator:

    def __init__(self, cam_index: int):
        self.cam_index = cam_index
        self.points = []
        self.homography = None
        self.cam_position_segment = None

    def warp_frame(self, frame):
        if self.homography is None:
            return frame
        return cv2.warpPerspective(frame, self.homography,
                                   (config.WARP_SIZE, config.WARP_SIZE))

    def to_dict(self) -> dict:
        return {
            "cam_index": self.cam_index,
            "points": self.points,
            "homography": self.homography.tolist() if self.homography is not None else None,
            "cam_position_segment": self.cam_position_segment,
        }

    def from_dict(self, data: dict):
        self.cam_index = data["cam_index"]
        self.points = [tuple(p) for p in data["points"]]
        h = data.get("homography")
        self.homography = np.float64(h) if h is not None else None
        self.cam_position_segment = data.get("cam_position_segment")


def calibrate_all_cameras(caps_unused, cam_indexes, cam_positions_override=None):
    """
    Calibrate cameras ONE AT A TIME.
    Releases all other cameras to avoid USB bandwidth saturation.
    The 'caps_unused' param is ignored - we open/close our own captures.
    """
    click_points = []

    def mouse_cb(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(click_points) < 4:
            click_points.append((x, y))
            n = len(click_points)
            print(f"  Point {n}/4: ({x}, {y}) -> {REFERENCE_LABELS[n-1]}")

    cv2.namedWindow(CALIB_WINDOW, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(CALIB_WINDOW, 960, 540)
    cv2.setMouseCallback(CALIB_WINDOW, mouse_cb)

    calibrators = []
    total = len(cam_indexes)

    for cam_i, cam_idx in enumerate(cam_indexes):
        click_points.clear()

        print(f"\n{'='*60}")
        print(f"CALIBRATION - Camera {cam_idx} ({cam_i+1}/{total})")
        print(f"{'='*60}")

        # Open ONLY this camera
        cap = cv2.VideoCapture(cam_idx)
        if not cap.isOpened():
            print(f"  ERROR: Cannot open camera {cam_idx}, skipping.")
            continue

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAM_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAM_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, config.CAM_FPS)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"  Opened camera {cam_idx} ({actual_w}x{actual_h})")
        print("  Click OUTER WIRE at: 20(top), 6(right), 3(bottom), 11(left)")
        print("  SPACE=confirm  R=reset  Q=cancel\n")

        # Drain buffer
        for _ in range(15):
            cap.grab()

        # Calibration loop
        confirmed = False
        while True:
            ret, frame = cap.read()
            if not ret:
                cv2.waitKey(30)
                continue

            display = frame.copy()

            # Camera ID
            cv2.putText(display, f"CAMERA {cam_idx}",
                        (display.shape[1] - 250, display.shape[0] - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

            # Draw points
            for i, pt in enumerate(click_points):
                cv2.circle(display, pt, 6, (0, 255, 0), -1)
                cv2.circle(display, pt, 8, (255, 255, 255), 2)
                cv2.putText(display, REFERENCE_LABELS[i],
                            (pt[0]+12, pt[1]-8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            for i in range(len(click_points)):
                j = (i + 1) % len(click_points)
                if j < len(click_points):
                    cv2.line(display, click_points[i], click_points[j],
                             (255, 150, 0), 1)

            # Status
            n = len(click_points)
            if n < 4:
                txt = f"Cam {cam_idx} ({cam_i+1}/{total}) | Click: {REFERENCE_LABELS[n]} ({n+1}/4)"
            else:
                txt = f"Cam {cam_idx} ({cam_i+1}/{total}) | OK! SPACE=confirm  R=redo"

            cv2.rectangle(display, (0, 0), (display.shape[1], 38), (0, 0, 0), -1)
            cv2.putText(display, txt, (10, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)

            cv2.imshow(CALIB_WINDOW, display)
            key = cv2.waitKey(30) & 0xFF

            if key == ord('q') or key == 27:
                cap.release()
                cv2.destroyAllWindows()
                return None

            if key == ord('r'):
                click_points.clear()
                print("  Reset.")

            if n >= 4 and key in (32, 13, 10, ord('c')):
                calib = Calibrator(cam_idx)
                calib.points = list(click_points)

                H, _ = cv2.findHomography(np.float32(calib.points), IDEAL_POINTS)

                if H is None:
                    print("  Homography failed, retry.")
                    click_points.clear()
                    continue

                calib.homography = H

                # Auto-detect position
                pts = calib.points
                dists = []
                for i in range(4):
                    j = (i + 1) % 4
                    d = math.sqrt((pts[i][0]-pts[j][0])**2 + (pts[i][1]-pts[j][1])**2)
                    dists.append(d)
                face_segments = [3, 11, 20, 6]
                calib.cam_position_segment = face_segments[dists.index(max(dists))]

                if cam_positions_override and cam_i < len(cam_positions_override):
                    calib.cam_position_segment = cam_positions_override[cam_i]

                print(f"  Camera {cam_idx} OK -> facing segment {calib.cam_position_segment}")
                calibrators.append(calib)
                confirmed = True
                break

        # RELEASE this camera before opening the next one
        cap.release()
        print(f"  Camera {cam_idx} released.")

    cv2.destroyAllWindows()
    return calibrators


def save_calibrations(calibrators, filepath=None):
    filepath = filepath or config.CALIB_FILE
    data = [c.to_dict() for c in calibrators]
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved to {filepath}")


def load_calibrations(filepath=None):
    filepath = filepath or config.CALIB_FILE
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r') as f:
        data = json.load(f)
    calibrators = []
    for d in data:
        c = Calibrator(d["cam_index"])
        c.from_dict(d)
        calibrators.append(c)
    print(f"Loaded calibration ({len(calibrators)} cameras)")
    return calibrators
