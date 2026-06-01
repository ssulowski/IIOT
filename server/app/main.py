from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse


API_KEY = os.getenv("API_KEY", "change-me")
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "/data"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "250"))
EVENT_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,120}$")

app = FastAPI(title="Sky Watcher Upload Server", version="1.0.0")


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def event_root() -> Path:
    root = STORAGE_DIR / "events"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_event_id(metadata: dict[str, Any]) -> str:
    raw = str(metadata.get("event_id") or uuid4().hex)
    if not EVENT_ID_RE.match(raw):
        return uuid4().hex
    return raw


def today_dir() -> Path:
    now = datetime.now(timezone.utc)
    directory = event_root() / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def save_upload(upload: UploadFile, target: Path) -> int:
    written = 0
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                tmp.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Upload too large")
            handle.write(chunk)
    tmp.replace(target)
    return written


def find_event_file(event_id: str, suffix: str) -> Path:
    if not EVENT_ID_RE.match(event_id):
        raise HTTPException(status_code=400, detail="Invalid event id")
    matches = list(event_root().glob(f"**/{event_id}{suffix}"))
    if not matches:
        raise HTTPException(status_code=404, detail="Event not found")
    return matches[0]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/events", dependencies=[Depends(require_api_key)])
async def create_event(
    metadata: str = Form(default="{}"),
    video: UploadFile = File(...),
) -> dict[str, Any]:
    try:
        parsed_metadata = json.loads(metadata)
        if not isinstance(parsed_metadata, dict):
            raise ValueError("metadata must be a JSON object")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid metadata: {exc}") from exc

    event_id = safe_event_id(parsed_metadata)
    directory = today_dir()
    video_path = directory / f"{event_id}.mp4"
    metadata_path = directory / f"{event_id}.json"

    size_bytes = await save_upload(video, video_path)
    sha256 = sha256_file(video_path)

    stored_metadata = {
        **parsed_metadata,
        "event_id": event_id,
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "stored_video": str(video_path.relative_to(STORAGE_DIR)),
        "size_bytes": size_bytes,
        "sha256": sha256,
        "analysis_status": parsed_metadata.get("analysis_status", "queued"),
    }
    metadata_path.write_text(json.dumps(stored_metadata, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "event_id": event_id,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


@app.get("/api/v1/events", dependencies=[Depends(require_api_key)])
def list_events(limit: int = 50) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for metadata_path in sorted(event_root().glob("**/*.json"), reverse=True):
        try:
            events.append(json.loads(metadata_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            events.append({"event_id": metadata_path.stem, "metadata_error": True})
        if len(events) >= limit:
            break
    return {"events": events}


@app.get("/api/v1/events/{event_id}/metadata", dependencies=[Depends(require_api_key)])
def get_event_metadata(event_id: str) -> dict[str, Any]:
    metadata_path = find_event_file(event_id, ".json")
    return json.loads(metadata_path.read_text(encoding="utf-8"))


@app.get("/api/v1/events/{event_id}/video", dependencies=[Depends(require_api_key)])
def get_event_video(event_id: str) -> FileResponse:
    video_path = find_event_file(event_id, ".mp4")
    return FileResponse(video_path, media_type="video/mp4", filename=video_path.name)


@app.delete("/api/v1/events/{event_id}", dependencies=[Depends(require_api_key)])
def delete_event(event_id: str) -> dict[str, Any]:
    metadata_path = find_event_file(event_id, ".json")
    video_path = metadata_path.with_suffix(".mp4")
    trash_dir = STORAGE_DIR / "deleted"
    trash_dir.mkdir(parents=True, exist_ok=True)
    for path in [metadata_path, video_path]:
        if path.exists():
            shutil.move(str(path), str(trash_dir / path.name))
    return {"ok": True, "event_id": event_id}
