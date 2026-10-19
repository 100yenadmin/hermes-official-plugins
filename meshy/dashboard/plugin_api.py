"""Meshy plugin backend: what the Desktop half needs from the machine Hermes runs on.

Mounted at ``/api/plugins/meshy/`` once ``meshy`` is in ``plugins.enabled``. The Desktop renderer
cannot read files or call Meshy itself, so this router does both, scoped to the requesting profile
(the host sets ``HERMES_HOME`` per request; nothing here binds a path or a key at import).

Routes:
    GET /status             key configured? + balance (no key → configured: false, never an error)
                            + the user's Settings ▸ Plugins ▸ Meshy preferences
    GET /models?limit=      recent finished Meshy generations: task id, type, prompt, thumbnail, credits
    GET /local?limit=       .glb files under the plugin's meshy_output/ (the server's default download dir)
    GET /file?path=         bytes of one .glb/.gltf/.png/.jpg (data URL), refused outside allowed roots
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

_log = logging.getLogger(__name__)
router = APIRouter()

_API = "https://api.meshy.ai"
_KEY_ENV = "MESHY_API_KEY"
# A Meshy GLB with 2K textures is ~30 MB; the cap leaves room for 4K/8K while bounding memory.
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MIME = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
# Model types the list endpoints serve, newest-first per type; merged and re-sorted below.
_MODEL_ENDPOINTS = (("text-to-3d", "/openapi/v2/text-to-3d"), ("image-to-3d", "/openapi/v1/image-to-3d"),
                    ("multi-image-to-3d", "/openapi/v1/multi-image-to-3d"), ("remesh", "/openapi/v1/remesh"),
                    ("retexture", "/openapi/v1/retexture"), ("rigging", "/openapi/v1/rigging"))


# Defaults mirror plugin.json ``extensions.com.nousresearch.hermes.config_schema``; Settings ▸ Plugins
# writes the user's choices to ``plugins.entries.meshy.settings``.
_SETTING_DEFAULTS = {"show_credits": True, "inline_viewer": True, "auto_rotate": True, "gallery_limit": 24}


def _settings() -> dict:
    from hermes_cli.config import load_config_readonly
    entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get("meshy") or {}
    saved = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
    out = dict(_SETTING_DEFAULTS)
    for key, default in _SETTING_DEFAULTS.items():
        value = saved.get(key)
        # bool is an int subclass: match the default's exact type so True never passes as a count.
        if type(value) is type(default):
            out[key] = value
    out["gallery_limit"] = max(1, min(60, out["gallery_limit"]))
    return out


def _api_key() -> str | None:
    from agent.secret_scope import get_secret
    return get_secret(_KEY_ENV) or None


def _meshy_get(path: str, key: str, timeout: float = 15.0):
    req = urllib.request.Request(_API + path, headers={
        "Authorization": f"Bearer {key}", "User-Agent": "hermes-meshy-plugin/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


def _meshy_error(exc: Exception) -> HTTPException:
    if isinstance(exc, urllib.error.HTTPError):
        detail = "Meshy rejected the API key" if exc.code in (401, 403) else f"Meshy API error {exc.code}"
        return HTTPException(status_code=502, detail=detail)
    return HTTPException(status_code=502, detail="Meshy API unreachable")


def _data_root() -> Path:
    """The plugin's data dir (the MCP server's cwd, so its default ``meshy_output/`` lives here)."""
    from hermes_constants import get_hermes_home
    base = get_hermes_home() / "plugin-data"
    found = sorted(base.glob("agent-plugin-meshy-*")) if base.is_dir() else []
    return found[0] if found else base / "agent-plugin-meshy"


def _allowed_roots() -> list[Path]:
    """Where /file may read: the plugin's output dir, the Hermes home's working dirs, the user's home.

    The viewer opens files the agent saved with ``save_to`` (usually inside a project), so the home
    directory is the practical root; secrets are excluded by extension, not by location.
    """
    roots = [_data_root(), Path.home()]
    return [r.resolve() for r in roots if r.exists()]


def _resolve_readable(raw: str) -> Path:
    if not raw or "\x00" in raw:
        raise HTTPException(status_code=400, detail="path required")
    target = Path(os.path.expanduser(raw))
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="file not found") from None
    if resolved.suffix.lower() not in _MIME:
        raise HTTPException(status_code=415, detail="only .glb, .gltf and image files")
    if not any(resolved.is_relative_to(root) for root in _allowed_roots()):
        raise HTTPException(status_code=403, detail="outside the allowed folders")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if resolved.stat().st_size > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file too large for the viewer")
    return resolved


@router.get("/status")
async def status():
    key = _api_key()
    if not key:
        return {"configured": False, "key_env": _KEY_ENV, "settings": _settings()}
    try:
        bal = _meshy_get("/openapi/v1/balance", key)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The status chip shows WHY there is no number (bad key vs offline) instead of a stale one.
        return {"configured": True, "settings": _settings(), "error": _meshy_error(exc).detail}
    return {"configured": True, "settings": _settings(), "balance": bal.get("balance"),
            "monthly_credits": bal.get("monthly_credits"), "permanent_credits": bal.get("permanent_credits"),
            "monthly_resets_at": bal.get("monthly_resets_at")}


def _credits_by_task(key: str) -> dict[str, int]:
    try:
        rows = _meshy_get("/openapi/v1/usage/tasks?page_size=50", key)
    except (urllib.error.URLError, OSError, ValueError):
        return {}  # usage is a team/API-key view; a personal key may not have it
    return {r["task_id"]: int(r["consumed_credits"]) for r in rows
            if isinstance(r, dict) and r.get("task_id") and isinstance(r.get("consumed_credits"), (int, float))}


@router.get("/models")
async def models(limit: int | None = Query(None, ge=1, le=60)):
    limit = limit or _settings()["gallery_limit"]
    key = _api_key()
    if not key:
        return {"configured": False, "models": []}
    merged: list[dict] = []
    for task_type, path in _MODEL_ENDPOINTS:
        try:
            rows = _meshy_get(f"{path}?page_size={limit}&sort_by=-created_at", key)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise _meshy_error(exc) from exc
            continue  # one type failing (404 on an endpoint the account lacks) never hides the rest
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _meshy_error(exc) from exc
        for row in rows if isinstance(rows, list) else []:
            if row.get("status") != "SUCCEEDED":
                continue
            urls = row.get("model_urls") or {}
            merged.append({"task_id": row.get("id"), "task_type": task_type, "prompt": row.get("prompt") or "",
                           "created_at": row.get("created_at"), "thumbnail_url": row.get("thumbnail_url"),
                           "glb_url": urls.get("glb") if isinstance(urls, dict) else None})
    merged.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
    merged = merged[:limit]
    credits = _credits_by_task(key)
    for m in merged:
        m["credits"] = credits.get(m["task_id"])
    return {"configured": True, "models": merged}


@router.get("/local")
async def local(limit: int = Query(50, ge=1, le=200)):
    out_dir = _data_root() / "meshy_output"
    if not out_dir.is_dir():
        return {"dir": str(out_dir), "files": []}
    files = sorted(out_dir.rglob("*.glb"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return {"dir": str(out_dir), "files": [
        {"path": str(p), "name": p.name, "project": p.parent.name, "bytes": p.stat().st_size,
         "modified": int(p.stat().st_mtime * 1000)} for p in files]}


@router.get("/file")
async def file(path: str):
    target = _resolve_readable(path)
    data = target.read_bytes()
    return {"name": target.name, "bytes": len(data), "mtime": int(target.stat().st_mtime * 1000),
            "dataUrl": f"data:{_MIME[target.suffix.lower()]};base64,{base64.b64encode(data).decode('ascii')}"}


@router.get("/remote")
async def remote(url: str):
    """Fetch a Meshy asset URL (signed ``assets.meshy.ai`` link) server-side; the renderer cannot
    (no CORS on that host). Only that host, only model/image types."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "assets.meshy.ai":
        raise HTTPException(status_code=400, detail="only assets.meshy.ai URLs")
    suffix = Path(parts.path).suffix.lower()
    if suffix not in _MIME:
        raise HTTPException(status_code=415, detail="only .glb and image assets")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - host pinned above
            data = resp.read(_MAX_FILE_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise _meshy_error(exc) from exc
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="asset too large for the viewer")
    _log.debug("meshy remote asset %s: %d bytes in %.1fs", parts.path, len(data), time.monotonic() - started)
    return {"name": Path(parts.path).name, "bytes": len(data),
            "dataUrl": f"data:{_MIME[suffix]};base64,{base64.b64encode(data).decode('ascii')}"}
