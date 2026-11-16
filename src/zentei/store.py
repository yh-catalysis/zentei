"""Append-only event files, one directory per machine."""

from __future__ import annotations

import os
import re
import socket
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from zentei.model import Event


def data_dir() -> Path:
    if override := os.environ.get("ZENTEI_DATA"):
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "zentei"


def machine_name() -> str:
    name = os.environ.get("ZENTEI_MACHINE") or socket.gethostname()
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name) or "unknown"


def new_event(type: str, data: dict[str, Any], origin: dict[str, Any] | None = None) -> Event:
    fields: dict[str, Any] = {
        "id": f"evt_{uuid.uuid4().hex}",
        "at": datetime.now().astimezone(),
        "machine": machine_name(),
        "type": type,
        "data": data,
    }
    if origin is not None:
        fields["origin"] = origin
    return Event.model_validate(fields)


def read_events(root: Path) -> list[Event]:
    events = []
    for path in sorted(root.glob("events/*/*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(Event.model_validate_json(line))
            except ValueError as e:
                raise ValueError(f"{path}:{number}: {e}") from e
    return events


def append_events(root: Path, events: list[Event]) -> None:
    for event in events:
        path = root / "events" / event.machine / f"{event.at:%Y-%m}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json(exclude_none=True) + "\n")
