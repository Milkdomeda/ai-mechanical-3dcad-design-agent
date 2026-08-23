#!/usr/bin/env python3
"""Download one selected STEP.parts record into the verified global cache."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_API = "https://api.step.parts"
SKILL_COMMIT = "4fd71ea75fbb8a80b0d7c76862e0fd73c52a8989"
USER_AGENT = "freecad-standard-parts/1.0"


def _default_cache_root() -> Path:
    configured = os.environ.get("MECH_DESIGN_STANDARD_PART_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "ai-mechanical-3dcad-design-agent" / "standard-parts" / "step-parts"


DEFAULT_CACHE = str(_default_cache_root())


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"Unsafe cache path segment: {value!r}")
    return cleaned


def _request(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_part(part_id: str, cache_root: str = DEFAULT_CACHE, api_origin: str = DEFAULT_API, timeout: float = 30.0) -> dict:
    api_origin = api_origin.rstrip("/")
    quoted_id = urllib.parse.quote(part_id, safe="")
    api_url = f"{api_origin}/v1/parts/{quoted_id}"
    record = json.loads(_request(api_url, timeout).decode("utf-8"))
    resolved_id = str(record.get("id") or part_id)
    download_url = record.get("downloadUrl") or record.get("stepUrl")
    if not download_url:
        raise ValueError(f"STEP.parts record {resolved_id!r} has no download URL")
    payload = _request(str(download_url), timeout)
    actual = _sha256(payload)
    expected = str(record.get("sha256") or "").lower()
    if not expected:
        raise ValueError(f"STEP.parts record {resolved_id!r} has no SHA-256")
    if actual != expected:
        raise ValueError(f"Checksum mismatch for {resolved_id}: expected {expected}, got {actual}")

    source_name = Path(urllib.parse.urlparse(str(download_url)).path).name
    filename = _safe_segment(source_name or f"{resolved_id}.step")
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        filename = f"{filename}.step"
    destination = Path(cache_root).expanduser().resolve() / _safe_segment(resolved_id) / actual
    destination.mkdir(parents=True, exist_ok=True)
    step_path = destination / filename
    if step_path.exists() and _sha256(step_path.read_bytes()) != actual:
        raise ValueError(f"Refusing to overwrite corrupt cache entry: {step_path}")
    if not step_path.exists():
        partial = destination / f"{filename}.partial"
        partial.write_bytes(payload)
        partial.replace(step_path)

    manifest = {
        "schema_version": 1,
        "provider": "STEP.parts",
        "part_id": resolved_id,
        "sha256": actual,
        "step_file": filename,
        "api_url": api_url,
        "source_url": record.get("pageUrl") or record.get("apiUrl") or api_url,
        "download_url": download_url,
        "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "skill_repository": "https://github.com/earthtojake/text-to-cad",
        "skill_path": "skills/step-parts",
        "skill_commit": SKILL_COMMIT,
        "part": record,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "cached",
        "part_id": resolved_id,
        "step_path": str(step_path),
        "manifest_path": str(manifest_path),
        "sha256": actual,
        "checksum_verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="Exact STEP.parts part ID selected from a prior search")
    parser.add_argument("--cache-root", default=DEFAULT_CACHE)
    parser.add_argument("--api-origin", default=DEFAULT_API)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    print(json.dumps(cache_part(args.id, args.cache_root, args.api_origin, args.timeout), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
