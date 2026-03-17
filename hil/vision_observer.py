#!/usr/bin/env python3
"""Observe two stepper-linked pointers with the Pi camera and log coarse angles."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import socket
import statistics
import sys
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import vision_config as config


def normalize_angle(angle_deg: float) -> float:
    return angle_deg % 360.0


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    return float(raw)


def env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


class AxisTracker:
    """Median + deadband filter for one circular angle stream."""

    def __init__(self, window: int, deadband_deg: float):
        self._history = deque(maxlen=window)
        self._deadband_deg = deadband_deg
        self._last_unwrapped = None
        self._last_filtered = None

    def update(self, raw_angle_deg):
        if raw_angle_deg is None:
            return None, False

        unwrapped = self._unwrap(raw_angle_deg)
        self._history.append(unwrapped)

        filtered = statistics.median(self._history)
        if self._last_filtered is not None and abs(filtered - self._last_filtered) < self._deadband_deg:
            filtered = self._last_filtered

        self._last_unwrapped = unwrapped
        self._last_filtered = filtered
        return round(normalize_angle(filtered), 3), True

    def _unwrap(self, angle_deg: float) -> float:
        if self._last_unwrapped is None:
            return angle_deg
        candidates = (angle_deg - 360.0, angle_deg, angle_deg + 360.0)
        return min(candidates, key=lambda item: abs(item - self._last_unwrapped))


def get_sharpest_edge_angle(np, approx):
    """Return the pointing angle of a detected triangle contour."""
    if len(approx) != 3:
        return None, None

    angles = []
    for index in range(3):
        p1 = approx[index][0]
        p2 = approx[(index + 1) % 3][0]
        p3 = approx[(index + 2) % 3][0]

        v1 = p1 - p2
        v2 = p3 - p2
        cosine = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
        angle = np.arccos(np.clip(cosine, -1, 1)) * 180 / np.pi
        angles.append((angle, index))

    _, sharpest_index = min(angles, key=lambda item: item[0])
    corner = approx[sharpest_index][0].astype(float)
    other_corners = [
        approx[(sharpest_index + 1) % 3][0].astype(float),
        approx[(sharpest_index + 2) % 3][0].astype(float),
    ]
    centroid = (other_corners[0] + other_corners[1]) / 2
    direction = centroid - corner
    pointing_angle = np.arctan2(direction[1], direction[0]) * 180 / np.pi
    return (pointing_angle + 90) % 360, None


class MjpegStreamServer:
    """Serve the latest encoded JPEG frame over HTTP without a second camera client."""

    def __init__(self, host: str, port: int):
        self._condition = threading.Condition()
        self._frame_id = 0
        self._jpeg_bytes = None
        self._stopped = False
        self._server = ThreadingHTTPServer((host, port), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def update_frame(self, jpeg_bytes: bytes) -> None:
        with self._condition:
            self._jpeg_bytes = jpeg_bytes
            self._frame_id += 1
            self._condition.notify_all()

    def get_latest_frame(self):
        with self._condition:
            return self._frame_id, self._jpeg_bytes

    def wait_for_frame(self, last_frame_id: int, timeout_s: float = 1.0):
        with self._condition:
            if not self._stopped and self._frame_id <= last_frame_id:
                self._condition.wait(timeout=timeout_s)
            return self._frame_id, self._jpeg_bytes, self._stopped

    def _build_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._serve_index()
                    return
                if self.path in ("/stream", "/stream.mjpg"):
                    self._serve_stream()
                    return
                if self.path == "/snapshot.jpg":
                    self._serve_snapshot()
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown path")

            def log_message(self, format, *args):
                return

            def _serve_index(self):
                body = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>dmx2stepper camera</title>"
                    "<style>body{font-family:sans-serif;background:#111;color:#eee;margin:0;padding:1rem;}"
                    "img{display:block;max-width:100%;height:auto;border:1px solid #333;}"
                    "a{color:#7cc6ff}</style></head><body>"
                    "<h1>dmx2stepper camera</h1>"
                    "<p>Live stream: <a href='/stream.mjpg'>/stream.mjpg</a></p>"
                    "<p>Snapshot: <a href='/snapshot.jpg'>/snapshot.jpg</a></p>"
                    "<img src='/stream.mjpg' alt='Live motor camera stream'>"
                    "</body></html>"
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_snapshot(self):
                _, jpeg_bytes = server.get_latest_frame()
                if jpeg_bytes is None:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "No frame available yet")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg_bytes)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg_bytes)

            def _serve_stream(self):
                self.send_response(HTTPStatus.OK)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()

                last_frame_id = -1
                while True:
                    frame_id, jpeg_bytes, stopped = server.wait_for_frame(last_frame_id)
                    if stopped:
                        return
                    if jpeg_bytes is None or frame_id == last_frame_id:
                        continue
                    last_frame_id = frame_id
                    try:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg_bytes)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(jpeg_bytes)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        return

        return Handler


def process_frame(cv2, np, frame):
    """Detect both triangle targets and return coarse angles for T1 and T2."""
    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    if config.ROTATE_180:
        frame_bgr = cv2.rotate(frame_bgr, cv2.ROTATE_180)

    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    lower, upper = config.get_orange_hsv_range()
    mask = cv2.inRange(hsv, np.array(lower), np.array(upper))

    kernel = np.ones((config.MORPH_KERNEL_SIZE, config.MORPH_KERNEL_SIZE), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    triangles = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < config.MIN_AREA:
            continue

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, config.APPROX_EPSILON * perimeter, True)
        if len(approx) == 3 and cv2.isContourConvex(approx):
            x_pos = float(np.mean(approx[:, 0, 0]))
            triangles.append((x_pos, approx))

    observations = {"T1": None, "T2": None}
    detections = []
    for x_pos, triangle in sorted(triangles, key=lambda item: item[0]):
        pointing_angle, _ = get_sharpest_edge_angle(np, triangle)
        if pointing_angle is None:
            continue

        if x_pos < config.LEFT_RIGHT_SPLIT_X:
            if observations["T1"] is None:
                observations["T1"] = round(normalize_angle(pointing_angle + config.LEFT_OFFSET_DEG), 1)
                detections.append({"axis": "T1", "triangle": triangle, "raw_angle_deg": observations["T1"]})
        else:
            if observations["T2"] is None:
                observations["T2"] = round(normalize_angle(pointing_angle + config.RIGHT_OFFSET_DEG), 1)
                detections.append({"axis": "T2", "triangle": triangle, "raw_angle_deg": observations["T2"]})

    return frame_bgr, observations, detections


def annotate_stream_frame(cv2, frame_bgr, detections, axis_states, frame_index: int):
    annotated = frame_bgr.copy()
    split_x = int(config.LEFT_RIGHT_SPLIT_X)
    cv2.line(annotated, (split_x, 0), (split_x, annotated.shape[0] - 1), (255, 0, 0), 1)

    for detection in detections:
        contour = detection["triangle"]
        axis = detection["axis"]
        raw_angle = detection["raw_angle_deg"]
        color = (0, 255, 0) if axis == "T1" else (0, 255, 255)
        cv2.drawContours(annotated, [contour], -1, color, 2)
        moments = cv2.moments(contour)
        if moments["m00"]:
            cx = int(moments["m10"] / moments["m00"])
            cy = int(moments["m01"] / moments["m00"])
            cv2.putText(
                annotated,
                f"{axis} raw {raw_angle:05.1f}",
                (max(0, cx - 50), max(20, cy - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )

    summary_lines = [f"frame {frame_index}"]
    for axis in ("T1", "T2"):
        state = axis_states[axis]
        if state["visible"]:
            summary_lines.append(
                f"{axis} raw {state['raw_angle_deg']:05.1f} filt {state['filtered_angle_deg']:05.1f}"
            )
        else:
            summary_lines.append(f"{axis} raw --- filt ---")

    for line_index, text in enumerate(summary_lines):
        cv2.putText(
            annotated,
            text,
            (12, 24 + (line_index * 22)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    return annotated


def guess_stream_url(host: str, port: int) -> str:
    if host not in {"0.0.0.0", "::", ""}:
        return f"http://{host}:{port}/"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip_addr = sock.getsockname()[0]
    except OSError:
        ip_addr = "127.0.0.1"
    finally:
        sock.close()
    return f"http://{ip_addr}:{port}/"


def timestamped_path(output_dir: Path, prefix: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{prefix}_{stamp}.csv"


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Camera-based coarse angle observer")
    parser.add_argument("--output", help="Explicit output CSV path")
    parser.add_argument("--output-dir", default=str(config.CAPTURE_DIR), help="Directory for timestamped CSV output")
    parser.add_argument("--duration-s", type=float, default=0.0, help="Optional capture duration, 0 = run until interrupted")
    parser.add_argument("--status-interval", type=float, default=1.0, help="Console status interval in seconds")
    parser.add_argument("--prefix", default="vision", help="Filename prefix when --output is not set")
    parser.add_argument(
        "--filter-window",
        type=int,
        default=config.FILTER_WINDOW,
        help="Median filter window for each axis",
    )
    parser.add_argument(
        "--deadband-deg",
        type=float,
        default=config.JITTER_DEADBAND_DEG,
        help="Deadband applied after median filtering",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        default=env_bool("VISION_STREAM", False),
        help="Expose the latest camera frame over HTTP/MJPEG while processing continues",
    )
    parser.add_argument(
        "--stream-host",
        default=env_str("VISION_STREAM_HOST", config.STREAM_BIND_HOST),
        help="Bind host for the HTTP stream",
    )
    parser.add_argument(
        "--stream-port",
        type=int,
        default=env_int("VISION_STREAM_PORT", config.STREAM_PORT),
        help="Bind port for the HTTP stream",
    )
    parser.add_argument(
        "--stream-fps",
        type=float,
        default=env_float("VISION_STREAM_FPS", config.STREAM_MAX_FPS),
        help="Maximum MJPEG publish rate; 0 or less means publish every processed frame",
    )
    parser.add_argument(
        "--stream-jpeg-quality",
        type=int,
        default=env_int("VISION_STREAM_JPEG_QUALITY", config.STREAM_JPEG_QUALITY),
        help="JPEG quality for the MJPEG stream",
    )
    return parser


def configure_camera(picamera2_class):
    picam2 = picamera2_class()
    preview = picam2.create_preview_configuration(main={"size": config.RESOLUTION})
    picam2.configure(preview)
    picam2.start()

    controls = {}
    if config.AWB_MODE is not None:
        controls["AwbMode"] = config.AWB_MODE
    if config.EXPOSURE_COMPENSATION is not None:
        controls["ExposureValue"] = config.EXPOSURE_COMPENSATION
    if controls:
        picam2.set_controls(controls)

    return picam2


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    try:
        import cv2
        import numpy as np
        from picamera2 import Picamera2
    except ImportError as exc:
        print(f"[ERROR] Missing dependency: {exc}", file=sys.stderr)
        print("Install with: pip install picamera2 numpy opencv-python", file=sys.stderr)
        return 2

    output_path = Path(args.output) if args.output else timestamped_path(Path(args.output_dir), args.prefix)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    trackers = {
        "T1": AxisTracker(max(1, int(args.filter_window)), max(0.0, float(args.deadband_deg))),
        "T2": AxisTracker(max(1, int(args.filter_window)), max(0.0, float(args.deadband_deg))),
    }

    csv_file = None
    picam2 = None
    stream_server = None
    camera_started = False
    frame_index = 0
    status_start = time.monotonic()
    status_frames = 0
    capture_start = status_start
    next_stream_publish_at = capture_start

    try:
        picam2 = configure_camera(Picamera2)
        camera_started = True
        if args.stream:
            stream_server = MjpegStreamServer(args.stream_host, args.stream_port)
            stream_server.start()
            print(f"[INFO] Camera stream available at {guess_stream_url(args.stream_host, stream_server.port)}")
        csv_file = output_path.open("w", newline="")
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "t_monotonic",
                "t_wall",
                "frame_index",
                "axis",
                "raw_angle_deg",
                "filtered_angle_deg",
                "visible",
            ],
        )
        writer.writeheader()

        while True:
            now_monotonic = time.monotonic()
            if args.duration_s > 0 and (now_monotonic - capture_start) >= args.duration_s:
                break

            frame = picam2.capture_array()
            frame_bgr, observations, detections = process_frame(cv2, np, frame)
            now_wall = dt.datetime.now().isoformat(timespec="milliseconds")

            frame_parts = []
            axis_states = {}
            for axis in ("T1", "T2"):
                raw_angle = observations[axis]
                filtered_angle, visible = trackers[axis].update(raw_angle)
                axis_states[axis] = {
                    "raw_angle_deg": raw_angle,
                    "filtered_angle_deg": filtered_angle,
                    "visible": visible,
                }
                writer.writerow(
                    {
                        "t_monotonic": f"{now_monotonic:.6f}",
                        "t_wall": now_wall,
                        "frame_index": frame_index,
                        "axis": axis,
                        "raw_angle_deg": "" if raw_angle is None else f"{raw_angle:.3f}",
                        "filtered_angle_deg": "" if filtered_angle is None else f"{filtered_angle:.3f}",
                        "visible": int(visible),
                    }
                )

                if visible:
                    frame_parts.append(f"{axis} raw={raw_angle:05.1f} filt={filtered_angle:05.1f}")
                else:
                    frame_parts.append(f"{axis} raw=--- filt=---")

            if stream_server is not None and (
                args.stream_fps <= 0 or now_monotonic >= next_stream_publish_at
            ):
                annotated_frame = annotate_stream_frame(cv2, frame_bgr, detections, axis_states, frame_index)
                quality = min(100, max(1, int(args.stream_jpeg_quality)))
                encoded_ok, encoded_frame = cv2.imencode(
                    ".jpg",
                    annotated_frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), quality],
                )
                if encoded_ok:
                    stream_server.update_frame(encoded_frame.tobytes())
                if args.stream_fps > 0:
                    next_stream_publish_at = now_monotonic + (1.0 / args.stream_fps)

            frame_index += 1
            status_frames += 1
            if frame_index % 10 == 0:
                csv_file.flush()

            elapsed = now_monotonic - status_start
            if elapsed >= args.status_interval:
                fps = status_frames / elapsed if elapsed > 0 else 0.0
                print(f"{now_wall} | frame {frame_index} | fps {fps:04.1f} | {' | '.join(frame_parts)}")
                status_start = now_monotonic
                status_frames = 0

    except KeyboardInterrupt:
        print("[INFO] Vision capture interrupted")
        return_code = 130
    except Exception as exc:
        print(f"[ERROR] Vision capture failed: {exc}", file=sys.stderr)
        sys.stderr.flush()
        sys.stdout.flush()
        if not camera_started:
            os._exit(1)
        return_code = 1
    else:
        return_code = 0
    finally:
        if stream_server is not None:
            stream_server.stop()
        if picam2 is not None and camera_started:
            picam2.stop()
        if csv_file is not None:
            csv_file.flush()
            csv_file.close()

    if return_code == 0:
        print(f"[INFO] Vision capture written to {output_path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
