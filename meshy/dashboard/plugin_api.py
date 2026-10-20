"""Meshy plugin backend: what the Desktop half needs from the machine Hermes runs on.

Mounted at ``/api/plugins/meshy/`` once ``meshy`` is in ``plugins.enabled``. The Desktop renderer
cannot read files or call Meshy itself, so this router does both, scoped to the requesting profile
(the host sets ``HERMES_HOME`` per request; nothing here binds a path or a key at import).

Routes:
    GET /status             key configured? + balance (no key → configured: false, never an error)
                            + the user's Settings ▸ Plugins ▸ Meshy preferences
    GET /models?limit=      recent finished Meshy generations: task id, type, prompt, thumbnail, credits
    GET /local?limit=       .glb files under the plugin's meshy_output/ (the server's default download dir),
                            plus models in the asset index (project folders), each with its Meshy task id when known
    GET /file?path=&task=   bytes of one .glb/.gltf/.png/.jpg (data URL), refused outside allowed roots;
                            a served model is recorded in the profile's asset index (path, size, sha256, task)
    GET /formats?task=      the file types a finished task offers (glb, fbx, obj, stl, usdz ...) + any already saved
    POST /save              {task, format, beside?}: download one format next to ``beside`` (no credits)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
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
        "Authorization": f"Bearer {key}", "User-Agent": "hermes-meshy-plugin/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
def status():
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
def models(limit: int | None = Query(None, ge=1, le=60)):
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


# Profile-scoped index of models this plugin served or saved, so files the agent wrote into a project
# (``save_to``) reach the gallery without scanning the home folder. Paths, sizes and hashes only: never
# a provider URL or a key. Capped; vanished files drop out on read.
_INDEX_NAME = "asset-index.json"
_INDEX_CAP = 500
_INDEXED = (".glb", ".gltf")


def _index_path() -> Path:
    return _data_root() / _INDEX_NAME


def _load_index() -> list[dict]:
    try:
        rows = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("path"), str)] if isinstance(rows, list) else []


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _record_asset(path: Path, task_id: str | None = None) -> None:
    """Upsert one model into the index. Only a resolved .glb/.gltf inside the allowed roots is recorded
    (the same gate /file applies), and a task id only when it is a well-formed Meshy id."""
    try:
        resolved = path.resolve(strict=True)
        if resolved.suffix.lower() not in _INDEXED or not resolved.is_file():
            return
        if not any(resolved.is_relative_to(root) for root in _allowed_roots()):
            return
        st = resolved.stat()
        task = task_id if isinstance(task_id, str) and _TASK_ID.match(task_id) else None
        now = int(time.time() * 1000)
        rows = _load_index()
        key = str(resolved)
        row = next((r for r in rows if r.get("path") == key), None)
        if row is None:
            row = {"path": key, "first_seen": now}
            rows.append(row)
        if row.get("bytes") != st.st_size or row.get("mtime") != int(st.st_mtime * 1000) or not row.get("sha256"):
            row.update(bytes=st.st_size, mtime=int(st.st_mtime * 1000), sha256=_sha256(resolved))
        row.update(name=resolved.name, last_opened=now, task_id=task or row.get("task_id"))
        rows.sort(key=lambda r: r.get("last_opened") or 0, reverse=True)
        target = _index_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_text(json.dumps(rows[:_INDEX_CAP], indent=1), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:  # the index is a convenience; serving the file never fails on it
        _log.debug("meshy asset index: %s", exc)


@router.get("/local")
def local(limit: int = Query(50, ge=1, le=200)):
    """meshy_output/ downloads plus indexed models that still exist, deduped by resolved path."""
    out_dir = _data_root() / "meshy_output"
    seen: dict[str, dict] = {}
    for p in out_dir.rglob("*.glb") if out_dir.is_dir() else ():
        st = p.stat()
        seen[str(p.resolve())] = {"path": str(p), "name": p.name, "project": p.parent.name, "bytes": st.st_size,
                                  "modified": int(st.st_mtime * 1000), "task": _local_task(p)}
    for row in _load_index():
        p = Path(row["path"])
        if str(p) in seen or not p.is_file():
            if str(p) in seen and not seen[str(p)]["task"] and row.get("task_id"):
                seen[str(p)]["task"] = row["task_id"]
            continue
        st = p.stat()
        seen[str(p)] = {"path": str(p), "name": p.name, "project": p.parent.name, "bytes": st.st_size,
                        "modified": max(int(st.st_mtime * 1000), int(row.get("last_opened") or 0)),
                        "task": row.get("task_id") if isinstance(row.get("task_id"), str) else None}
    files = sorted(seen.values(), key=lambda f: f["modified"], reverse=True)[:limit]
    return {"dir": str(out_dir), "files": files}


# /save writes the full task id here (its folder is named by the first 8 characters only).
_TASK_SIDECAR = ".meshy-task"


def _local_task(model: Path) -> str | None:
    """The Meshy task a downloaded file came from: the MCP server's project ``metadata.json`` (the task
    whose ``files`` list it, else the project's root task), or the sidecar /save leaves; else None."""
    found = None
    try:
        meta = json.loads((model.parent / "metadata.json").read_text(encoding="utf-8"))
        tasks = [t for t in meta.get("tasks") or [] if isinstance(t, dict)]
        found = next((t.get("task_id") for t in tasks if model.name in (t.get("files") or [])), None)
        found = found or meta.get("root_task_id")
    except (OSError, ValueError, AttributeError):
        try:
            found = (model.parent / _TASK_SIDECAR).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return found if isinstance(found, str) and _TASK_ID.match(found) else None


@router.get("/file")
def file(path: str, task: str | None = None):
    target = _resolve_readable(path)
    data = target.read_bytes()
    if target.suffix.lower() in _INDEXED:
        _record_asset(target, task)
    return {"name": target.name, "bytes": len(data), "mtime": int(target.stat().st_mtime * 1000),
            "dataUrl": f"data:{_MIME[target.suffix.lower()]};base64,{base64.b64encode(data).decode('ascii')}"}


# Display order and labels for the download menu; ``mtl`` rides along with ``obj``, never on its own.
_FORMAT_ORDER = ("glb", "fbx", "obj", "stl", "usdz", "3mf", "blend")
_TASK_ID = re.compile(r"^[0-9a-f][0-9a-f-]{15,63}$")
# A refined Meshy OBJ is ~180 MB; the cap bounds a runaway response, not a real model.
_MAX_SAVE_BYTES = 1024 * 1024 * 1024


def _task(task_id: str, key: str) -> dict:
    """The task record (with signed ``model_urls``), found on whichever generation endpoint owns it."""
    if not _TASK_ID.match(task_id or ""):
        raise HTTPException(status_code=400, detail="task id required")
    for _task_type, path in _MODEL_ENDPOINTS:
        try:
            row = _meshy_get(f"{path}/{task_id}", key)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise _meshy_error(exc) from exc
            continue  # 404/400: another endpoint owns this id
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _meshy_error(exc) from exc
        if isinstance(row, dict) and row.get("id") == task_id:
            return row
    raise HTTPException(status_code=404, detail="Meshy has no finished task with that id")


def _asset_url(url: object) -> str:
    parts = urllib.parse.urlsplit(str(url or ""))
    if parts.scheme != "https" or parts.hostname != "assets.meshy.ai":
        raise HTTPException(status_code=502, detail="unexpected asset host")
    return str(url)


def _save_target(task_id: str, beside: str | None) -> tuple[Path, str]:
    """Folder + file stem for a saved format: next to the model the card shows, else meshy_output/<task>.
    Validates both inputs, so callers run it BEFORE spending a Meshy API call on a bad request."""
    if not _TASK_ID.match(task_id or ""):
        raise HTTPException(status_code=400, detail="task id required")
    if beside:
        anchor = _resolve_readable(beside)
        return anchor.parent, anchor.stem
    return _data_root() / "meshy_output" / task_id[:8], "model"


def _download(url: str, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(_asset_url(url), timeout=120) as resp, open(part, "wb") as out:
            copied = 0
            while chunk := resp.read(1 << 20):
                copied += len(chunk)
                if copied > _MAX_SAVE_BYTES:
                    raise HTTPException(status_code=413, detail="asset larger than 1 GB")
                out.write(chunk)
        part.replace(dest)
    except (urllib.error.URLError, OSError) as exc:
        raise _meshy_error(exc) from exc
    finally:
        part.unlink(missing_ok=True)


@router.get("/formats")
def formats(task: str, beside: str | None = None):
    folder, stem = _save_target(task, beside)
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    urls = _task(task, key).get("model_urls") or {}
    out = []
    for fmt in _FORMAT_ORDER:
        if isinstance(urls, dict) and urls.get(fmt):
            saved = folder / f"{stem}.{fmt}"
            out.append({"format": fmt, "saved": str(saved) if saved.is_file() else None})
    return {"task": task, "formats": out}


@router.post("/save")
def save(body: dict):
    """Download one format of a finished task. Meshy produced every listed format at generation time,
    so this spends no credits. OBJ brings its .mtl and base-colour texture, renamed per model."""
    task_id, fmt = str(body.get("task") or ""), str(body.get("format") or "").lower()
    if fmt not in _FORMAT_ORDER:
        raise HTTPException(status_code=400, detail="unknown format")
    beside = body.get("beside") or None
    folder, stem = _save_target(task_id, beside)
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task_id, key)
    urls = row.get("model_urls") or {}
    if not isinstance(urls, dict) or not urls.get(fmt):
        raise HTTPException(status_code=404, detail=f"this model has no {fmt.upper()} file")
    folder.mkdir(parents=True, exist_ok=True)
    if not beside:
        (folder / _TASK_SIDECAR).write_text(task_id, encoding="utf-8")
    dest = folder / f"{stem}.{fmt}"
    if not dest.is_file():
        _download(urls[fmt], dest)
        if fmt == "obj" and urls.get("mtl"):
            _save_obj_material(row, urls["mtl"], folder, stem)
    if fmt in ("glb", "gltf"):
        _record_asset(dest, task_id)
    return {"path": str(dest), "bytes": dest.stat().st_size}


def _save_obj_material(row: dict, mtl_url: str, folder: Path, stem: str) -> None:
    """The .mtl names ``texture_0.png``; save it as ``<stem>_texture.png`` so two models in one folder
    never share (and overwrite) a texture, and point the .mtl at the new name."""
    with urllib.request.urlopen(_asset_url(mtl_url), timeout=60) as resp:
        mtl = resp.read(1 << 20).decode("utf-8", "replace")
    textures = row.get("texture_urls") or []
    base = textures[0].get("base_color") if textures and isinstance(textures[0], dict) else None
    if base:
        _download(base, folder / f"{stem}_texture.png")
        mtl = re.sub(r"(?m)^(\s*map_Kd\s+).*$", rf"\g<1>{stem}_texture.png", mtl)
    (folder / f"{stem}.mtl").write_text(mtl, encoding="utf-8")
    # Repoint ``mtllib`` (a header line) by streaming: the OBJ is ~180 MB, never read it whole.
    obj = folder / f"{stem}.obj"
    tmp = obj.with_name(obj.name + ".part")
    with open(obj, "rb") as src, open(tmp, "wb") as dst:
        for _ in range(64):
            line = src.readline()
            if not line:
                break
            dst.write(f"mtllib {stem}.mtl\n".encode() if line.startswith(b"mtllib ") else line)
        shutil.copyfileobj(src, dst, 1 << 20)
    tmp.replace(obj)


@router.get("/remote")
def remote(url: str):
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
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read(_MAX_FILE_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise _meshy_error(exc) from exc
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="asset too large for the viewer")
    _log.debug("meshy remote asset %s: %d bytes in %.1fs", parts.path, len(data), time.monotonic() - started)
    return {"name": Path(parts.path).name, "bytes": len(data),
            "dataUrl": f"data:{_MIME[suffix]};base64,{base64.b64encode(data).decode('ascii')}"}
