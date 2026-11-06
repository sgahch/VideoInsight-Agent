from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


STREAM_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def encode_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def iter_events(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    event = ""
    data: list[str] = []
    for line in lines:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "", []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()
    if data:
        yield event, "\n".join(data)
