#!/usr/bin/env python3
"""
Lightweight sky-object recorder for Raspberry Pi Zero 2 W.

The detector intentionally favours compact, persistent movement over broad
frame changes, which helps reject moving clouds and lighting shifts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import math
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Deque, Iterable
from uuid import uuid4

import cv2
import numpy as np
import requests
import yaml


LOGGER = logging.getLogger("sky_watcher")


@dataclasses.dataclass
class CameraConfig:
    backend: str = "auto"
    device_index: int = 0
    width: int = 640
    height: int = 480
    fps: int = 12
    warmup_seconds: float = 2.0


@dataclasses.dataclass
class PathsConfig:
    recordings_dir: Path = Path("recordings")
    unsent_dir: Path = Path("recordings/unsent")


@dataclasses.dataclass
class RecordingConfig:
    pre_seconds: int = 4
    post_seconds: int = 5
    max_event_seconds: int = 60
    min_event_seconds: int = 3
    raw_fourcc: str = "MJPG"


@dataclasses.dataclass
class DetectionConfig:
    process_width: int = 320
    background_history: int = 220
    background_var_threshold: int = 28
    learning_rate: float = -1
    min_area: int = 5
    max_area: int = 450
    max_global_motion_ratio: float = 0.055
    min_fill_ratio: float = 0.12
    max_fill_ratio: float = 0.92
    min_contrast: float = 10
    min_track_hits: int = 3
    track_ttl_frames: int = 8
    min_track_distance: float = 5
    merge_distance: float = 28
    debug_preview: bool = False


@dataclasses.dataclass
class CompressionConfig:
    enabled: bool = True
    ffmpeg_path: str = "ffmpeg"
    crf: int = 28
    preset: str = "veryfast"
    scale_width: int = 640
    delete_raw_after_compress: bool = True


@dataclasses.dataclass
class UploadConfig:
    enabled: bool = True
    server_url: str = ""
    api_key: str = "change-me"
    timeout_seconds: int = 45
    retry_count: int = 4
    retry_backoff_seconds: int = 8


@dataclasses.dataclass
class AppConfig:
    camera: CameraConfig
    paths: PathsConfig
    recording: RecordingConfig
    detection: DetectionConfig
    compression: CompressionConfig
    upload: UploadConfig


def _section(cls: type, raw: dict[str, Any], name: str) -> Any:
    values = raw.get(name, {}) or {}
    converted: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        value = values.get(field.name, field.default)
        if isinstance(field.default, Path):
            value = Path(value)
        converted[field.name] = value
    return cls(**converted)


def load_config(path: Path) -> AppConfig:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig(
        camera=_section(CameraConfig, raw, "camera"),
        paths=_section(PathsConfig, raw, "paths"),
        recording=_section(RecordingConfig, raw, "recording"),
        detection=_section(DetectionConfig, raw, "detection"),
        compression=_section(CompressionConfig, raw, "compression"),
        upload=_section(UploadConfig, raw, "upload"),
    )


class CameraSource:
    def read(self) -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        pass


class PiCamera2Source(CameraSource):
    def __init__(self, cfg: CameraConfig) -> None:
        from picamera2 import Picamera2

        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"size": (cfg.width, cfg.height), "format": "RGB888"},
            controls={"FrameRate": cfg.fps},
        )
        self.picam2.configure(config)
        self.picam2.start()
        time.sleep(cfg.warmup_seconds)

    def read(self) -> np.ndarray:
        rgb = self.picam2.capture_array()
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    def close(self) -> None:
        self.picam2.stop()


class OpenCVCameraSource(CameraSource):
    def __init__(self, cfg: CameraConfig) -> None:
        self.capture = cv2.VideoCapture(cfg.device_index)
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self.capture.set(cv2.CAP_PROP_FPS, cfg.fps)
        time.sleep(cfg.warmup_seconds)
        if not self.capture.isOpened():
            raise RuntimeError(f"Cannot open camera index {cfg.device_index}")

    def read(self) -> np.ndarray:
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise RuntimeError("Camera frame read failed")
        return frame

    def close(self) -> None:
        self.capture.release()


def open_camera(cfg: CameraConfig) -> CameraSource:
    backend = cfg.backend.lower()
    if backend in {"auto", "picamera2"}:
        try:
            LOGGER.info("Opening camera with Picamera2")
            return PiCamera2Source(cfg)
        except Exception:
            if backend == "picamera2":
                raise
            LOGGER.warning("Picamera2 unavailable, falling back to OpenCV", exc_info=True)

    LOGGER.info("Opening camera with OpenCV VideoCapture")
    return OpenCVCameraSource(cfg)


@dataclasses.dataclass
class Candidate:
    centroid: tuple[float, float]
    bbox: tuple[int, int, int, int]
    area: float
    contrast: float


@dataclasses.dataclass
class Track:
    track_id: int
    first: tuple[float, float]
    last: tuple[float, float]
    hits: int = 1
    missed: int = 0

    def update(self, centroid: tuple[float, float]) -> None:
        self.last = centroid
        self.hits += 1
        self.missed = 0

    @property
    def distance(self) -> float:
        return math.dist(self.first, self.last)


class MotionDetector:
    def __init__(self, cfg: DetectionConfig, frame_shape: tuple[int, int, int]) -> None:
        self.cfg = cfg
        self.scale = cfg.process_width / frame_shape[1]
        self.process_height = max(1, int(frame_shape[0] * self.scale))
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=cfg.background_history,
            varThreshold=cfg.background_var_threshold,
            detectShadows=False,
        )
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.tracks: list[Track] = []
        self.next_track_id = 1
        self.frame_index = 0

    def detect(self, frame: np.ndarray) -> tuple[bool, dict[str, Any]]:
        self.frame_index += 1
        small = cv2.resize(frame, (self.cfg.process_width, self.process_height))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        fg = self.bg.apply(gray, learningRate=self.cfg.learning_rate)
        _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.kernel, iterations=1)
        fg = cv2.dilate(fg, self.kernel, iterations=1)

        moving_ratio = float(cv2.countNonZero(fg)) / float(fg.size)
        if moving_ratio > self.cfg.max_global_motion_ratio:
            self._age_tracks()
            return False, {
                "reason": "global_motion",
                "moving_ratio": moving_ratio,
                "candidates": 0,
                "valid_tracks": 0,
            }

        candidates = self._extract_candidates(gray, fg)
        self._update_tracks(candidates)
        valid_tracks = [
            track
            for track in self.tracks
            if track.hits >= self.cfg.min_track_hits
            and track.distance >= self.cfg.min_track_distance
        ]

        metadata = {
            "moving_ratio": moving_ratio,
            "candidates": len(candidates),
            "valid_tracks": len(valid_tracks),
            "tracks": [
                {
                    "id": track.track_id,
                    "hits": track.hits,
                    "distance_px_processed": round(track.distance, 2),
                    "last": [round(track.last[0], 1), round(track.last[1], 1)],
                }
                for track in valid_tracks[:5]
            ],
        }
        return bool(valid_tracks), metadata

    def _extract_candidates(self, gray: np.ndarray, mask: np.ndarray) -> list[Candidate]:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[Candidate] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < self.cfg.min_area or area > self.cfg.max_area:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            rect_area = max(1, w * h)
            fill_ratio = area / rect_area
            if fill_ratio < self.cfg.min_fill_ratio or fill_ratio > self.cfg.max_fill_ratio:
                continue

            roi = gray[y : y + h, x : x + w]
            roi_mask = mask[y : y + h, x : x + w]
            foreground = roi[roi_mask > 0]
            if foreground.size == 0:
                continue

            pad = 3
            y1 = max(0, y - pad)
            y2 = min(gray.shape[0], y + h + pad)
            x1 = max(0, x - pad)
            x2 = min(gray.shape[1], x + w + pad)
            surround = gray[y1:y2, x1:x2]
            contrast = abs(float(np.mean(foreground)) - float(np.mean(surround)))
            if contrast < self.cfg.min_contrast:
                continue

            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            centroid = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])
            candidates.append(Candidate(centroid, (x, y, w, h), area, contrast))

        return candidates

    def _update_tracks(self, candidates: Iterable[Candidate]) -> None:
        unmatched_tracks = set(range(len(self.tracks)))
        for candidate in candidates:
            match_idx = None
            match_distance = self.cfg.merge_distance
            for idx, track in enumerate(self.tracks):
                if idx not in unmatched_tracks:
                    continue
                distance = math.dist(track.last, candidate.centroid)
                if distance < match_distance:
                    match_distance = distance
                    match_idx = idx

            if match_idx is None:
                self.tracks.append(
                    Track(
                        track_id=self.next_track_id,
                        first=candidate.centroid,
                        last=candidate.centroid,
                    )
                )
                self.next_track_id += 1
            else:
                self.tracks[match_idx].update(candidate.centroid)
                unmatched_tracks.discard(match_idx)

        for idx in unmatched_tracks:
            self.tracks[idx].missed += 1
        self.tracks = [
            track for track in self.tracks if track.missed <= self.cfg.track_ttl_frames
        ]

    def _age_tracks(self) -> None:
        for track in self.tracks:
            track.missed += 1
        self.tracks = [
            track for track in self.tracks if track.missed <= self.cfg.track_ttl_frames
        ]


class Recorder:
    def __init__(
        self,
        cfg: AppConfig,
        first_frame: np.ndarray,
        executor: concurrent.futures.Executor,
    ) -> None:
        self.cfg = cfg
        self.executor = executor
        self.frame_size = (first_frame.shape[1], first_frame.shape[0])
        self.pre_buffer: Deque[np.ndarray] = deque(
            maxlen=max(1, int(cfg.camera.fps * cfg.recording.pre_seconds))
        )
        self.pre_buffer.append(first_frame.copy())
        self.writer: cv2.VideoWriter | None = None
        self.raw_path: Path | None = None
        self.final_path: Path | None = None
        self.metadata_path: Path | None = None
        self.event_started_at = 0.0
        self.last_detection_at = 0.0
        self.event_id = ""
        self.event_metadata: dict[str, Any] = {}

        cfg.paths.recordings_dir.mkdir(parents=True, exist_ok=True)
        cfg.paths.unsent_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_recording(self) -> bool:
        return self.writer is not None

    def add_frame(self, frame: np.ndarray, detected: bool, detector_info: dict[str, Any]) -> None:
        now = time.monotonic()

        if detected:
            self.last_detection_at = now
            if not self.is_recording:
                self._start(detector_info, now)

        if self.is_recording and self.writer is not None:
            self.writer.write(frame)
            duration = now - self.event_started_at
            silence = now - self.last_detection_at
            should_stop = (
                duration >= self.cfg.recording.min_event_seconds
                and silence >= self.cfg.recording.post_seconds
            ) or duration >= self.cfg.recording.max_event_seconds
            if should_stop:
                self._stop()

        self.pre_buffer.append(frame.copy())

    def close(self) -> None:
        if self.is_recording:
            self._stop()

    def _start(self, detector_info: dict[str, Any], now: float) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.event_id = f"{timestamp}-{uuid4().hex[:8]}"
        self.raw_path = self.cfg.paths.recordings_dir / f"{self.event_id}.avi"
        self.final_path = self.cfg.paths.unsent_dir / f"{self.event_id}.mp4"
        self.metadata_path = self.cfg.paths.unsent_dir / f"{self.event_id}.json"

        fourcc_text = self.cfg.recording.raw_fourcc[:4].ljust(4)
        fourcc = cv2.VideoWriter_fourcc(*fourcc_text)
        self.writer = cv2.VideoWriter(
            str(self.raw_path),
            fourcc,
            float(self.cfg.camera.fps),
            self.frame_size,
        )
        if not self.writer.isOpened():
            self.writer.release()
            self.writer = None
            raise RuntimeError(f"Cannot open video writer for {self.raw_path}")

        for buffered_frame in list(self.pre_buffer):
            self.writer.write(buffered_frame)

        self.event_started_at = now
        self.last_detection_at = now
        self.event_metadata = {
            "event_id": self.event_id,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "pre_buffer_seconds": self.cfg.recording.pre_seconds,
            "camera": dataclasses.asdict(self.cfg.camera),
            "first_detection": detector_info,
        }
        LOGGER.info("Started event %s", self.event_id)

    def _stop(self) -> None:
        assert self.writer is not None
        assert self.raw_path is not None
        assert self.final_path is not None
        assert self.metadata_path is not None

        self.writer.release()
        self.writer = None
        duration = time.monotonic() - self.event_started_at
        metadata = dict(self.event_metadata)
        metadata["duration_seconds"] = round(duration, 2)
        metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

        LOGGER.info("Finished event %s, queued for compression/upload", self.event_id)
        self.executor.submit(
            process_completed_event,
            raw_path=self.raw_path,
            final_path=self.final_path,
            metadata_path=self.metadata_path,
            metadata=metadata,
            compression=self.cfg.compression,
            upload=self.cfg.upload,
        )

        self.raw_path = None
        self.final_path = None
        self.metadata_path = None
        self.event_id = ""
        self.event_metadata = {}


def process_completed_event(
    raw_path: Path,
    final_path: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
    compression: CompressionConfig,
    upload: UploadConfig,
) -> None:
    try:
        if compression.enabled:
            compress_video(raw_path, final_path, compression)
            if compression.delete_raw_after_compress:
                raw_path.unlink(missing_ok=True)
        else:
            shutil.move(str(raw_path), str(final_path))

        metadata["video_file"] = final_path.name
        metadata["sha256"] = sha256_file(final_path)
        metadata["size_bytes"] = final_path.stat().st_size
        metadata["analysis_status"] = "queued"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        if upload.enabled:
            upload_event(final_path, metadata_path, upload)
    except Exception:
        LOGGER.exception("Failed to process event %s", raw_path)


def compress_video(raw_path: Path, final_path: Path, cfg: CompressionConfig) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_suffix(".tmp.mp4")
    cmd = [
        cfg.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(raw_path),
        "-vf",
        f"scale={cfg.scale_width}:-2",
        "-c:v",
        "libx264",
        "-preset",
        cfg.preset,
        "-crf",
        str(cfg.crf),
        "-movflags",
        "+faststart",
        str(tmp_path),
    ]
    LOGGER.info("Compressing %s", raw_path.name)
    subprocess.run(cmd, check=True)
    tmp_path.replace(final_path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_event(video_path: Path, metadata_path: Path, cfg: UploadConfig) -> None:
    if not cfg.server_url:
        LOGGER.warning("Upload enabled but server_url is empty")
        return

    url = cfg.server_url.rstrip("/") + "/api/v1/events"
    metadata = metadata_path.read_text(encoding="utf-8")
    headers = {"X-API-Key": cfg.api_key}
    last_error: Exception | None = None

    for attempt in range(1, cfg.retry_count + 1):
        try:
            with video_path.open("rb") as video_handle:
                response = requests.post(
                    url,
                    headers=headers,
                    data={"metadata": metadata},
                    files={"video": (video_path.name, video_handle, "video/mp4")},
                    timeout=cfg.timeout_seconds,
                )
            response.raise_for_status()
            uploaded_dir = video_path.parent / "uploaded"
            uploaded_dir.mkdir(exist_ok=True)
            video_path.replace(uploaded_dir / video_path.name)
            metadata_path.replace(uploaded_dir / metadata_path.name)
            LOGGER.info("Uploaded %s", video_path.name)
            return
        except Exception as exc:
            last_error = exc
            wait_seconds = cfg.retry_backoff_seconds * attempt
            LOGGER.warning(
                "Upload attempt %s/%s failed for %s: %s",
                attempt,
                cfg.retry_count,
                video_path.name,
                exc,
            )
            time.sleep(wait_seconds)

    LOGGER.error("Upload failed for %s: %s", video_path, last_error)


def upload_pending(unsent_dir: Path, cfg: UploadConfig, executor: concurrent.futures.Executor) -> None:
    if not cfg.enabled:
        return
    for metadata_path in sorted(unsent_dir.glob("*.json")):
        video_path = metadata_path.with_suffix(".mp4")
        if video_path.exists():
            executor.submit(upload_event, video_path, metadata_path, cfg)


def run(config_path: Path) -> None:
    cfg = load_config(config_path)
    cfg.paths.recordings_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.unsent_dir.mkdir(parents=True, exist_ok=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        upload_pending(cfg.paths.unsent_dir, cfg.upload, executor)
        camera = open_camera(cfg.camera)
        try:
            first_frame = camera.read()
            detector = MotionDetector(cfg.detection, first_frame.shape)
            recorder = Recorder(cfg, first_frame, executor)

            LOGGER.info("Sky watcher started")
            next_frame_at = time.monotonic()
            frame_delay = 1.0 / max(1, cfg.camera.fps)
            while True:
                frame = camera.read()
                detected, info = detector.detect(frame)
                recorder.add_frame(frame, detected, info)

                if cfg.detection.debug_preview:
                    preview = frame.copy()
                    cv2.putText(
                        preview,
                        f"detected={detected} {info}",
                        (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (0, 255, 0) if detected else (0, 180, 255),
                        1,
                        cv2.LINE_AA,
                    )
                    cv2.imshow("sky-watcher", preview)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

                next_frame_at += frame_delay
                sleep_for = next_frame_at - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_frame_at = time.monotonic()
        except KeyboardInterrupt:
            LOGGER.info("Interrupted")
        finally:
            try:
                recorder.close()
            except UnboundLocalError:
                pass
            camera.close()
            if cfg.detection.debug_preview:
                cv2.destroyAllWindows()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Raspberry Pi sky object recorder")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/pi_config.yaml"),
        help="Path to YAML configuration",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(args.config)


if __name__ == "__main__":
    main()
