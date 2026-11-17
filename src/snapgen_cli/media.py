"""Reference media: each --image/--video/--audio value is a URL, a local file (auto-uploaded) or a generation uuid."""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

from snapgen_mcp.client import SnapGenClient, SnapGenError

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PLACEHOLDER = "upload://"


@dataclass
class Refs:
    image_urls: list[str] = field(default_factory=list)
    history_uuids: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)  # placeholders upload://<n> point here

    def as_kwargs(self) -> dict[str, list[str]]:
        return {"image_urls": self.image_urls, "history_uuids": self.history_uuids,
                "video_urls": self.video_urls, "audio_urls": self.audio_urls}


def _is_url(value: str) -> bool:
    return value.startswith(("https://", "http://"))


def collect(images: list[str], videos: list[str], audios: list[str], history: list[str]) -> Refs:
    """Sort the values into slots. Local files get a placeholder so the request can be validated before uploading."""
    refs = Refs(history_uuids=list(history))

    def add(value: str, slot: list[str], kind: str) -> None:
        if _is_url(value):
            slot.append(value)
            return
        path = Path(value).expanduser()
        if path.is_file():
            refs.files.append(path)
            slot.append(f"{PLACEHOLDER}{len(refs.files) - 1}")
            return
        if kind == "image" and UUID_RE.match(value):
            refs.history_uuids.append(value)  # a previous generation used as a reference
            return
        hint = ", a local file or a generation uuid" if kind == "image" else " or a local file"
        raise SnapGenError(f"--{kind} {value!r}: not found. Pass an http(s) URL{hint}.")

    for v in images:
        add(v, refs.image_urls, "image")
    for v in videos:
        add(v, refs.video_urls, "video")
    for v in audios:
        add(v, refs.audio_urls, "audio")
    return refs


async def upload_file(client: SnapGenClient, path: Path) -> dict:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        return await client.upload(path.name, fh, content_type)


async def upload_placeholders(client: SnapGenClient, refs: Refs, on_upload=None) -> None:
    """Upload each local file once and swap its placeholder for the returned URL."""
    urls: list[str] = []
    for path in refs.files:
        if on_upload:
            on_upload(path)
        result = await upload_file(client, path)
        url = result.get("url")
        if not url:
            raise SnapGenError(f"Upload of {path.name} returned no URL.")
        urls.append(url)

    def swap(values: list[str]) -> list[str]:
        return [urls[int(v[len(PLACEHOLDER):])] if v.startswith(PLACEHOLDER) else v for v in values]

    refs.image_urls, refs.video_urls, refs.audio_urls = swap(refs.image_urls), swap(refs.video_urls), swap(refs.audio_urls)
    refs.files = []
