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
import queue
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import threading
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
    fps: int = 8
    warmup_seconds: float = 2.0
    ae_enable: bool = True
    exposure_time_us: int = 0
    analogue_gain: float = 0.0
    awb_enable: bool = True


@dataclasses.dataclass
class PathsConfig:
    recordings_dir: Path = Path("recordings")
    unsent_dir: Path = Path("recordings/unsent")


@dataclasses.dataclass
class RecordingConfig:
    pre_seconds: int = 4
    post_seconds: int = 15
    max_event_seconds: int = 60
    min_event_seconds: int = 3
    raw_fourcc: str = "MJPG"
    raw_quality: int = 95
    writer_queue_frames: int = 64


@dataclasses.dataclass
class DetectionConfig:
    process_width: int = 480
    background_history: int = 500
    background_var_threshold: int = 45
    learning_rate: float = -1
    min_area: int = 2
    max_area: int = 320
    max_global_motion_ratio: float = 0.010
    max_candidates_per_frame: int = 4
    max_candidate_area_ratio: float = 0.0045
    max_bbox_area: int = 420
    max_aspect_ratio: float = 4.5
    min_fill_ratio: float = 0.08
    max_fill_ratio: float = 0.95
    min_contrast: float = 8
    min_dark_contrast: float = 8
    max_foreground_brightness: float = 140
    min_surround_brightness: float = 60
    max_surround_stddev: float = 65
    min_track_hits: int = 2
    track_ttl_frames: int = 10
    min_track_distance: float = 3
    min_track_speed: float = 0.8
    merge_distance: float = 44
    debug_preview: bool = False
    log_rejections_every_frames: int = 120


@dataclasses.dataclass
class CompressionConfig:
    enabled: bool = True
    ffmpeg_path: str = "ffmpeg"
    crf: int = 24
    preset: str = "veryfast"
    scale_width: int = 640
    sharpen: bool = True
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
        controls: dict[str, Any] = {"FrameRate": cfg.fps}
        if not cfg.ae_enable:
            controls["AeEnable"] = False
            if cfg.exposure_time_us > 0:
                controls["ExposureTime"] = cfg.exposure_time_us
            if cfg.analogue_gain > 0:
                controls["AnalogueGain"] = cfg.analogue_gain
        if not cfg.awb_enable:
            controls["AwbEnable"] = False

        config = self.picam2.create_video_configuration(
            main={"size": (cfg.width, cfg.height), "format": "RGB888"},
            controls=controls,
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
    dark_contrast: float
    aspect_ratio: float
    foreground_mean: float
    surround_mean: float
    surround_stddev: float


@dataclasses.dataclass
class Track:
    track_id: int
    first: tuple[float, float]
    last: tuple[float, float]
    first_frame: int
    last_frame: int
    hits: int = 1
    missed: int = 0

    def update(self, centroid: tuple[float, float], frame_index: int) -> None:
        self.last = centroid
        self.last_frame = frame_index
        self.hits += 1
        self.missed = 0

    @property
    def distance(self) -> float:
        return math.dist(self.first, self.last)

    @property
    def average_speed(self) -> float:
        frames = max(1, self.last_frame - self.first_frame)
        return self.distance / frames


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
            metadata = {
                "reason": "global_motion",
                "moving_ratio": moving_ratio,
                "candidates": 0,
                "valid_tracks": 0,
            }
            self._log_rejection(metadata)
            return False, metadata

        candidates = self._extract_candidates(gray, fg)
        candidate_area_ratio = sum(candidate.area for candidate in candidates) / float(fg.size)
        if (
            len(candidates) > self.cfg.max_candidates_per_frame
            or candidate_area_ratio > self.cfg.max_candidate_area_ratio
        ):
            self._age_tracks()
            metadata = {
                "reason": "cloud_like_motion",
                "moving_ratio": moving_ratio,
                "candidates": len(candidates),
                "candidate_area_ratio": round(candidate_area_ratio, 5),
                "valid_tracks": 0,
            }
            self._log_rejection(metadata)
            return False, metadata

        self._update_tracks(candidates)
        valid_tracks = [
            track
            for track in self.tracks
            if track.hits >= self.cfg.min_track_hits
            and track.distance >= self.cfg.min_track_distance
            and track.average_speed >= self.cfg.min_track_speed
        ]

        metadata = {
            "moving_ratio": moving_ratio,
            "candidates": len(candidates),
            "candidate_area_ratio": round(candidate_area_ratio, 5),
            "valid_tracks": len(valid_tracks),
            "tracks": [
                {
                    "id": track.track_id,
                    "hits": track.hits,
                    "distance_px_processed": round(track.distance, 2),
                    "speed_px_per_frame_processed": round(track.average_speed, 2),
                    "last": [round(track.last[0], 1), round(track.last[1], 1)],
                }
                for track in valid_tracks[:5]
            ],
        }
        if not valid_tracks:
            metadata["reason"] = "no_valid_track"
        return bool(valid_tracks), metadata

    def _log_rejection(self, metadata: dict[str, Any]) -> None:
        if self.cfg.log_rejections_every_frames <= 0:
            return
        if metadata.get("reason") == "no_valid_track":
            return
        if self.frame_index % self.cfg.log_rejections_every_frames == 0:
            LOGGER.info("Detector rejected: %s", metadata)

    def _extract_candidates(self, gray: np.ndarray, mask: np.ndarray) -> list[Candidate]:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[Candidate] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < self.cfg.min_area or area > self.cfg.max_area:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            rect_area = max(1, w * h)
            if rect_area > self.cfg.max_bbox_area:
                continue

            aspect_ratio = max(w, h) / max(1, min(w, h))
            if aspect_ratio > self.cfg.max_aspect_ratio:
                continue

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
            foreground_mean = float(np.mean(foreground))
            surround_mean = float(np.mean(surround))
            surround_stddev = float(np.std(surround))
            dark_contrast = surround_mean - foreground_mean
            contrast = abs(dark_contrast)
            if contrast < self.cfg.min_contrast:
                continue
            if dark_contrast < self.cfg.min_dark_contrast:
                continue
            if foreground_mean > self.cfg.max_foreground_brightness:
                continue
            if surround_mean < self.cfg.min_surround_brightness:
                continue
            if surround_stddev > self.cfg.max_surround_stddev:
                continue

            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            centroid = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])
            candidates.append(
                Candidate(
                    centroid=centroid,
                    bbox=(x, y, w, h),
                    area=area,
                    contrast=contrast,
                    dark_contrast=dark_contrast,
                    aspect_ratio=aspect_ratio,
                    foreground_mean=foreground_mean,
                    surround_mean=surround_mean,
                    surround_stddev=surround_stddev,
                )
            )

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
                        first_frame=self.frame_index,
                        last_frame=self.frame_index,
                    )
                )
                self.next_track_id += 1
            else:
                self.tracks[match_idx].update(candidate.centroid, self.frame_index)
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


class AsyncVideoWriter:
    def __init__(
        self,
        path: Path,
        fourcc: int,
        fps: float,
        frame_size: tuple[int, int],
        quality: int,
        max_queue_frames: int,
    ) -> None:
        self.path = path
        self.writer = cv2.VideoWriter(str(path), fourcc, fps, frame_size)
        if not self.writer.isOpened():
            self.writer.release()
            raise RuntimeError(f"Cannot open video writer for {path}")
        self.writer.set(cv2.VIDEOWRITER_PROP_QUALITY, float(quality))

        self.frames: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=max_queue_frames)
        self.dropped_frames = 0
        self.written_frames = 0
        self._closed = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"video-writer-{path.stem}",
            daemon=True,
        )
        self._thread.start()

    def write(self, frame: np.ndarray) -> bool:
        if self._closed:
            return False
        try:
            self.frames.put_nowait(frame.copy())
            return True
        except queue.Full:
            self.dropped_frames += 1
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.frames.put(None)
        self._thread.join()
        if self._error is not None:
            raise RuntimeError(f"Video writer failed for {self.path}") from self._error

    def _run(self) -> None:
        try:
            while True:
                frame = self.frames.get()
                if frame is None:
                    break
                self.writer.write(frame)
                self.written_frames += 1
        except BaseException as exc:
            self._error = exc
        finally:
            self.writer.release()


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
        self.writer: AsyncVideoWriter | None = None
        self.raw_path: Path | None = None
        self.final_path: Path | None = None
        self.metadata_path: Path | None = None
        self.event_started_at = 0.0
        self.last_detection_at = 0.0
        self.event_id = ""
        self.event_metadata: dict[str, Any] = {}
        self.current_recording_fps = float(cfg.camera.fps)
        self.frames_written = 0
        self.pre_frames_written = 0
        self.writer_dropped_frames = 0

        cfg.paths.recordings_dir.mkdir(parents=True, exist_ok=True)
        cfg.paths.unsent_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_recording(self) -> bool:
        return self.writer is not None

    def add_frame(
        self,
        frame: np.ndarray,
        detected: bool,
        detector_info: dict[str, Any],
        observed_fps: float,
    ) -> None:
        now = time.monotonic()
        self.current_recording_fps = observed_fps

        if detected:
            self.last_detection_at = now
            if not self.is_recording:
                self._start(detector_info, now)

        if self.is_recording and self.writer is not None:
            if self.writer.write(frame):
                self.frames_written += 1
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
        self.writer = AsyncVideoWriter(
            path=self.raw_path,
            fourcc=fourcc,
            fps=float(self.current_recording_fps),
            frame_size=self.frame_size,
            quality=self.cfg.recording.raw_quality,
            max_queue_frames=self.cfg.recording.writer_queue_frames,
        )

        self.frames_written = 0
        self.pre_frames_written = 0
        self.writer_dropped_frames = 0
        for buffered_frame in list(self.pre_buffer):
            if self.writer.write(buffered_frame):
                self.frames_written += 1
                self.pre_frames_written += 1

        self.event_started_at = now
        self.last_detection_at = now
        self.event_metadata = {
            "event_id": self.event_id,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "pre_buffer_seconds": self.cfg.recording.pre_seconds,
            "recording_fps": round(self.current_recording_fps, 2),
            "camera": dataclasses.asdict(self.cfg.camera),
            "first_detection": detector_info,
        }
        LOGGER.info("Started event %s", self.event_id)

    def _stop(self) -> None:
        assert self.writer is not None
        assert self.raw_path is not None
        assert self.final_path is not None
        assert self.metadata_path is not None

        writer = self.writer
        writer.close()
        self.writer_dropped_frames = writer.dropped_frames
        self.writer = None
        duration = time.monotonic() - self.event_started_at
        pre_buffer_duration = self.pre_frames_written / max(0.001, self.current_recording_fps)
        expected_video_duration = pre_buffer_duration + duration
        metadata = dict(self.event_metadata)
        metadata["duration_seconds"] = round(duration, 2)
        metadata["pre_buffer_actual_seconds"] = round(pre_buffer_duration, 2)
        metadata["expected_video_duration_seconds"] = round(expected_video_duration, 2)
        metadata["frames_written"] = self.frames_written
        metadata["pre_frames_written"] = self.pre_frames_written
        metadata["writer_dropped_frames"] = self.writer_dropped_frames
        metadata["writer_actual_written_frames"] = writer.written_frames
        metadata["effective_video_fps"] = round(
            self.frames_written / max(0.001, expected_video_duration),
            2,
        )
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
        self.frames_written = 0
        self.pre_frames_written = 0
        self.writer_dropped_frames = 0


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
            compress_video(raw_path, final_path, compression, metadata)
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


def compress_video(
    raw_path: Path,
    final_path: Path,
    cfg: CompressionConfig,
    metadata: dict[str, Any],
) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_suffix(".tmp.mp4")
    video_filter = build_video_filter(cfg, metadata)
    cmd = [
        cfg.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(raw_path),
        "-vf",
        video_filter,
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


def build_video_filter(cfg: CompressionConfig, metadata: dict[str, Any]) -> str:
    filters: list[str] = []
    frames_written = int(metadata.get("frames_written") or 0)
    encoded_fps = float(metadata.get("recording_fps") or 0)
    expected_duration = float(metadata.get("expected_video_duration_seconds") or 0)
    if frames_written > 0 and encoded_fps > 0 and expected_duration > 0:
        encoded_duration = frames_written / encoded_fps
        if encoded_duration > 0:
            correction = expected_duration / encoded_duration
            metadata["playback_correction_factor"] = round(correction, 4)
            if abs(correction - 1.0) > 0.05:
                filters.append(f"setpts={correction:.6f}*PTS")

    filters.append(f"scale={cfg.scale_width}:-2")
    if cfg.sharpen:
        filters.append("unsharp=5:5:0.8:3:3:0.4")
    return ",".join(filters)


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
            frame_times: Deque[float] = deque(maxlen=max(10, cfg.camera.fps * 5))

            LOGGER.info("Sky watcher started")
            next_frame_at = time.monotonic()
            frame_delay = 1.0 / max(1, cfg.camera.fps)
            while True:
                frame = camera.read()
                captured_at = time.monotonic()
                frame_times.append(captured_at)
                observed_fps = float(cfg.camera.fps)
                if len(frame_times) >= 2:
                    elapsed = frame_times[-1] - frame_times[0]
                    if elapsed > 0:
                        observed_fps = max(
                            1.0,
                            min(float(cfg.camera.fps), (len(frame_times) - 1) / elapsed),
                        )

                detected, info = detector.detect(frame)
                recorder.add_frame(frame, detected, info, observed_fps)

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
