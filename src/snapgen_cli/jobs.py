"""Job status, polling and downloads."""
from __future__ import annotations

import asyncio
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from snapgen_mcp.client import SnapGenClient, SnapGenError


class JobFailed(SnapGenError):
    """The generation finished with an error."""


class WaitTimeout(SnapGenError):
    """--wait-timeout ran out before the job finished."""


def summarize(h: dict) -> dict:
    """Shrink a uapi history record. Same status rules as snapgen_mcp.server._summarize (observed live:
    status 1 = processing, 2 = completed, 3 = failed; status_desc may be empty or free text)."""
    status_desc = (h.get("status_desc") or "").upper()
    failed = bool(h.get("error_code")) or h.get("status") == 3 or status_desc in {"FAILED", "ERROR"}
    done = not failed and (h.get("status") == 2 or status_desc == "COMPLETED")
    out: dict[str, Any] = {
        "uuid": h.get("uuid"),
        "type": h.get("type"),
        "model": h.get("model_name"),
        "prompt": h.get("input_text"),
        "status": "failed" if failed else "completed" if done else "processing",
        "progress_percent": h.get("status_percentage"),
        "credits_used": h.get("used_credit"),
        "created_at": h.get("created_at"),
        "expires_at": h.get("expired_at"),
    }
    if failed:
        out["error"] = h.get("error_message") or h.get("error_code")
    if done:
        out["image_urls"] = [i.get("image_url") for i in h.get("generated_image") or [] if i.get("image_url")]
        out["video_urls"] = [v.get("video_url") for v in h.get("generated_video") or [] if v.get("video_url")]
    return out


def submitted(resp: dict) -> dict:
    return {
        "uuid": resp.get("uuid"),
        "status": "submitted",
        "model": resp.get("model_name"),
        "estimated_credit": resp.get("estimated_credit"),
    }


async def wait(client: SnapGenClient, uuid: str, timeout: float, interval: float, show_progress: bool) -> dict:
    """Poll until the job completes or fails. Raises JobFailed / WaitTimeout."""
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    try:
        while True:
            job = summarize(await client.get_history(uuid))
            if show_progress:
                pct = job.get("progress_percent")
                pct_txt = f" {pct}%" if isinstance(pct, (int, float)) and pct else ""
                sys.stderr.write(f"\r{job['status']}{pct_txt} - {int(time.monotonic() - started)}s   ")
                sys.stderr.flush()
            if job["status"] == "failed":
                raise JobFailed(f"Generation {uuid} failed: {job.get('error') or 'unknown error'}")
            if job["status"] == "completed":
                return job
            if time.monotonic() + interval > deadline:
                raise WaitTimeout(f"Still processing after {int(timeout)}s. Check later with: snapgen generate wait {uuid}")
            await asyncio.sleep(interval)
    finally:
        if show_progress:
            sys.stderr.write("\r" + " " * 40 + "\r")
            sys.stderr.flush()


def result_urls(job: dict) -> list[str]:
    return [*(job.get("image_urls") or []), *(job.get("video_urls") or [])]


def _extension(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 5:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) if content_type else None
    return guessed or ".bin"


async def download(job: dict, directory: Path) -> list[Path]:
    """Save every result file of a finished job into `directory` as <uuid>[-n].<ext>."""
    urls = result_urls(job)
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as http:
        for n, url in enumerate(urls, 1):
            async with http.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise SnapGenError(f"Download failed ({resp.status_code}) for {url}")
                suffix = f"-{n}" if len(urls) > 1 else ""
                path = directory / f"{job['uuid']}{suffix}{_extension(url, resp.headers.get('content-type'))}"
                with path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
            saved.append(path)
    return saved
