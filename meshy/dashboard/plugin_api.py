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
    GET /active             Meshy tasks still PENDING/IN_PROGRESS with their progress % (progress chip)
    GET /actions?task=      the follow-up actions a finished task supports (texture, remesh, rig ...), each with
                            its credit cost and the chat message a card button sends; plus the live balance
    POST /provenance        {task_id, tool, args, session_id?}: remember what made a task (Desktop records it
                            from the tool call that created it): prompt, style, source image, settings, parent
    GET /details?task=      "Made from" for the gallery: prompt(s), source image, settings, credits, the chain of
                            tasks it came from and what was made from it, and the chat that made it
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
                    ("retexture", "/openapi/v1/retexture"), ("rigging", "/openapi/v1/rigging"),
                    ("animation", "/openapi/v1/animations"))


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
            urls = _result_urls(row)
            # A rig's bind-pose character sits still; its walking clip shows what the rig is for.
            clips = [u for k, u in sorted(urls.items()) if k.startswith("glb:")]
            glb = (clips[-1] if clips and task_type == "rigging" else None) or urls.get("glb") or next(iter(clips), None)
            merged.append({"task_id": row.get("id"), "task_type": task_type, "prompt": row.get("prompt") or _clip_label(urls),
                           "created_at": row.get("created_at"), "thumbnail_url": row.get("thumbnail_url"),
                           "glb_url": glb})
    merged.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
    merged = merged[:limit]
    credits = _credits_by_task(key)
    prov = _load_prov()
    for m in merged:
        m["credits"] = credits.get(m["task_id"])
        m["prompt"] = _label_for(m["task_id"], prov) or m["prompt"]
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
    prov = _load_prov()
    for f in files:
        f["prompt"] = (_label_for(f["task"], prov) if f["task"] else None) or _project_prompt(Path(f["path"]))
    return {"dir": str(out_dir), "files": files}


def _project_prompt(model: Path) -> str | None:
    try:
        name = json.loads((model.parent / "metadata.json").read_text(encoding="utf-8")).get("project_name")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and name not in ("text-to-3d", "image-to-3d", "") else None


def _label_for(task_id: str, prov: dict[str, dict]) -> str | None:
    """A one-line "what made this": its own prompt, else the nearest recorded ancestor's, else its image."""
    cur, hops = task_id, 0
    while cur and hops < 12:
        rec = prov.get(cur) or {}
        text = rec.get("prompt") or rec.get("text_style_prompt") or rec.get("texture_prompt")
        if text:
            return text
        if rec.get("image"):
            return f"from image {Path(urllib.parse.urlsplit(rec['image']).path).name}"
        cur, hops = rec.get("parent"), hops + 1
    return None


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


# Rigging and animation tasks carry no ``model_urls``: their files sit in ``result`` as
# ``<what>_<fmt>_url`` (``rigged_character_glb_url``, ``animation_fbx_url``) and, for a rig's free
# clips, ``result.basic_animations.<clip>_<fmt>_url``. The main file maps to the plain format key;
# extra clips map to ``<fmt>:<clip>`` (``glb:walking``) so the Download menu lists one row each.
_MAIN_OUTPUTS = ("rigged_character", "animation", "processed")
_VARIANT = re.compile(r"^[a-z0-9_]{1,40}$")


def _result_urls(row: dict) -> dict[str, str]:
    """``{format or format:clip: url}`` for any task: ``model_urls`` as-is, else its ``result`` files."""
    urls = row.get("model_urls")
    if isinstance(urls, dict) and urls:
        return {k: v for k, v in urls.items() if isinstance(v, str) and v}
    result = row.get("result")
    out: dict[str, str] = {}
    if not isinstance(result, dict):
        return out

    def take(prefix: str, field: str, url: object) -> None:
        if not (isinstance(url, str) and url and field.endswith("_url")):
            return
        stem = field[:-4]
        fmt = next((f for f in _FORMAT_ORDER if stem == f or stem.endswith("_" + f)), None)
        if not fmt:
            return
        what = stem[: -len(fmt)].rstrip("_")
        if "armature" in what:
            return  # skeleton-only exports: not a model a user means to open
        name = what if not prefix else f"{prefix}_{what}" if what else prefix
        if not prefix and (not what or what in _MAIN_OUTPUTS):
            out.setdefault(fmt, url)
            return
        clip = re.sub(r"^(basic_animations_|processed_)", "", name)
        if _VARIANT.match(clip):
            out.setdefault(f"{fmt}:{clip}", url)

    for field, value in result.items():
        if isinstance(value, dict):
            for sub, url in value.items():
                take("", sub, url) if field == "basic_animations" else take(field, sub, url)
        else:
            take("", field, value)
    return out


def _owning_record(row: dict, task_id: str, key: str) -> dict:
    """Meshy answers a task id on every endpoint, reshaped to that endpoint (a rig id read from
    text-to-3d has ``model_urls`` and no rig files). The record's own ``type`` names the real owner;
    re-read rigging/animation tasks there so their ``result`` files are what the menus list."""
    kind = str(row.get("type") or "").lower()
    path = "/openapi/v1/rigging" if kind.startswith("rig") else "/openapi/v1/animations" if "anim" in kind else None
    if not path or isinstance(row.get("result"), dict):
        return row
    try:
        owned = _meshy_get(f"{path}/{task_id}", key)
    except (urllib.error.URLError, OSError, ValueError):
        return row
    return owned if isinstance(owned, dict) and owned.get("id") == task_id else row


def _clip_label(urls: dict[str, str]) -> str:
    """A rig/animation task has no prompt; name it by the clips it carries (``walking, running``)."""
    clips = sorted({_split_format(k)[1] for k in urls if ":" in k} - {None})
    return ", ".join(c.replace("_", " ") for c in clips if c)


def _split_format(key: str) -> tuple[str, str | None]:
    base, _, clip = key.partition(":")
    return base, (clip or None)


def _format_sort_key(key: str) -> tuple[int, str]:
    base, clip = _split_format(key)
    return (_FORMAT_ORDER.index(base) if base in _FORMAT_ORDER else 99, clip or "")


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
            return _owning_record(row, task_id, key)
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
    urls = _result_urls(_task(task, key))
    out = []
    for fmt in sorted((k for k in urls if _split_format(k)[0] in _FORMAT_ORDER), key=_format_sort_key):
        saved = folder / _saved_name(stem, fmt)
        out.append({"format": fmt, "saved": str(saved) if saved.is_file() else None})
    return {"task": task, "formats": out}


@router.post("/save")
def save(body: dict):
    """Download one format of a finished task. Meshy produced every listed format at generation time,
    so this spends no credits. OBJ brings its .mtl and base-colour texture, renamed per model."""
    task_id, fmt = str(body.get("task") or ""), str(body.get("format") or "").lower()
    base, clip = _split_format(fmt)
    if base not in _FORMAT_ORDER or (clip is not None and not _VARIANT.match(clip)):
        raise HTTPException(status_code=400, detail="unknown format")
    beside = body.get("beside") or None
    folder, stem = _save_target(task_id, beside)
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task_id, key)
    urls = _result_urls(row)
    if not urls.get(fmt):
        raise HTTPException(status_code=404, detail=f"this model has no {fmt.upper()} file")
    folder.mkdir(parents=True, exist_ok=True)
    if not beside:
        (folder / _TASK_SIDECAR).write_text(task_id, encoding="utf-8")
    dest = folder / _saved_name(stem, fmt)
    if not dest.is_file():
        _download(urls[fmt], dest)
        if fmt == "obj" and urls.get("mtl"):
            _save_obj_material(row, urls["mtl"], folder, stem)
    if fmt in ("glb", "gltf"):
        _record_asset(dest, task_id)
    return {"path": str(dest), "bytes": dest.stat().st_size}


def _saved_name(stem: str, fmt: str) -> str:
    """``model.glb`` for the main file, ``model_walking.glb`` for a rig's extra clip."""
    base, clip = _split_format(fmt)
    return f"{stem}_{clip}.{base}" if clip else f"{stem}.{base}"


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


# In-flight tasks for the Desktop progress chip. Meshy's list endpoints return status + progress per
# task; the chip polls this only while a Meshy tool runs or a task is still in flight.
_ACTIVE_STATUSES = ("PENDING", "IN_PROGRESS")
_ACTIVE_ENDPOINTS = (*_MODEL_ENDPOINTS, ("text-to-image", "/openapi/v1/text-to-image"),
                     ("animation", "/openapi/v1/animations"))
# A task queued longer than this is Meshy's problem, not a progress bar's (Meshy advises 60 min max).
_ACTIVE_MAX_AGE_MS = 60 * 60 * 1000


def _active_rows(task_type: str, path: str, key: str) -> list[dict]:
    try:
        rows = _meshy_get(f"{path}?page_size=5&sort_by=-created_at", key, timeout=8.0)
    except (urllib.error.URLError, OSError, ValueError):
        return []  # one endpoint failing never hides the others
    now_ms = time.time() * 1000
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") not in _ACTIVE_STATUSES:
            continue
        created = row.get("created_at") or 0
        if isinstance(created, (int, float)) and created and now_ms - created > _ACTIVE_MAX_AGE_MS:
            continue
        stage = str(row.get("mode") or row.get("type") or task_type)
        out.append({"task_id": row.get("id"), "task_type": task_type, "stage": stage,
                    "status": row.get("status"), "progress": int(row.get("progress") or 0),
                    "prompt": str(row.get("prompt") or "")[:120], "created_at": created})
    return out


@router.get("/active")
def active():
    """Meshy tasks still PENDING / IN_PROGRESS (newest first), with Meshy's own progress %."""
    key = _api_key()
    if not key:
        return {"configured": False, "tasks": []}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(_ACTIVE_ENDPOINTS)) as pool:
        batches = list(pool.map(lambda e: _active_rows(e[0], e[1], key), _ACTIVE_ENDPOINTS))
    tasks = sorted((t for b in batches for t in b), key=lambda t: t.get("created_at") or 0, reverse=True)
    return {"configured": True, "tasks": tasks}


# Card buttons. Each one SENDS A CHAT MESSAGE (the agent runs the tool behind Hermes' approval card); the
# router never spends credits. Which actions a task offers follows each Meshy tool's documented input
# ("input_task_id: a SUCCEEDED text-to-3d, image-to-3d or remesh task" ...); costs are Meshy's API prices.
_GENERATED = ("text-to-3d-refine", "image-to-3d", "multi-image-to-3d")
_ACTIONS = (
    # id, label, cost (None = the task's own consumed_credits), task types it applies to, message
    ("texture", "Texture it", 10, ("text-to-3d-preview",),
     "texture it with meshy_text_to_3d_refine (10 credits). Ask me for a texture style only if I haven't given one."),
    # Text only: an image task's record does not keep its input image, so it cannot be re-run from the id.
    ("regenerate", "Regenerate", None, ("text-to-3d-preview",),
     ("regenerate it: a NEW meshy_text_to_3d preview with the same prompt and model_type (read them with "
      "meshy_get_task_status), new seed ({cost} credits, what the original cost). Meshy's API has no free retry.")),
    ("remesh", "Remesh", 5, ("text-to-3d-preview", *_GENERATED, "retexture"),
     ("remesh it with meshy_remesh (5 credits). Suggest a triangle target for what it is (player character "
      "~15k, NPC 5-15k, weapon ~3k, small prop ~1.5k; mobile about half; close-up or print: keep detail) and ask "
      "me the target and triangle or quad before calling.")),
    ("retexture", "Retexture", 10, ("text-to-3d-preview", *_GENERATED, "remesh"),
     "retexture it with meshy_retexture (10 credits). Ask me for the style in words first."),
    ("rig", "Rig", 5, (*_GENERATED, "retexture", "remesh"),
     ("rig it with meshy_rig (5 credits, includes walking + running clips). Only if it is a textured humanoid; "
      "say so and stop if it is not.")),
    ("animate", "Animate", 3, ("rig",),
     ("animate this rig with meshy_animate (3 credits per action). Look up action ids with "
      "meshy_list_animations (free) and ask me which ones.")),
    ("resize", "Resize", 1, (*_GENERATED, "remesh", "retexture"),
     "resize it to real-world size with meshy_resize (1 credit). Ask me for the height."),
    ("printability", "Check printability", 0, ("text-to-3d-preview", *_GENERATED, "remesh", "retexture"),
     "check it for 3D printing with meshy_analyze_printability (free) and summarise the result."),
)


@router.get("/actions")
def actions(task: str):
    if not _TASK_ID.match(task or ""):
        raise HTTPException(status_code=400, detail="task id required")
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task, key)
    kind = str(row.get("type") or "").lower()
    spent = row.get("consumed_credits") if isinstance(row.get("consumed_credits"), (int, float)) else None
    try:
        balance = _meshy_get("/openapi/v1/balance", key).get("balance")
    except (urllib.error.URLError, OSError, ValueError):
        balance = None
    what = str(row.get("prompt") or "").strip()[:80]
    subject = f'Meshy task {task}' + (f' ("{what}")' if what else "")
    out = []
    for aid, label, cost, kinds, message in _ACTIONS:
        if kind not in kinds:
            continue
        cost = int(spent) if cost is None and spent is not None else cost
        if cost is None:
            continue  # unknown price: never show a button that can't state what it spends
        out.append({"id": aid, "label": label, "cost": cost,
                    "affordable": balance is None or cost <= balance,
                    "covers": (int(balance // cost) if cost and isinstance(balance, (int, float)) else None),
                    "message": f"{subject}: {message.format(cost=cost)} This button message is my confirmation "
                               f"of the {cost}-credit cost." if cost else f"{subject}: {message}"})
    return {"task": task, "type": kind, "model_type": row.get("model_type"), "spent": spent,
            "balance": balance, "actions": out}


# Provenance: what made each model. Meshy's task record keeps a text prompt but not an image task's
# input, nor which task a remesh/rig/animation came from, so Desktop records the creating tool call here.
_PROV_NAME = "provenance.json"
_PROV_CAP = 2000
_PROV_TEXT = ("prompt", "texture_prompt", "text_style_prompt", "negative_prompt", "object_prompt")
_PROV_PARENT = ("preview_task_id", "input_task_id", "rig_task_id")
_PROV_SETTINGS = ("ai_model", "model_type", "topology", "target_polycount", "texture_resolution", "enable_pbr",
                  "geometry_resolution", "pose_mode", "symmetry_mode", "should_texture", "resize_height",
                  "height_meters", "action_id", "action_ids")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _prov_path() -> Path:
    return _data_root() / _PROV_NAME


def _load_prov() -> dict[str, dict]:
    try:
        rows = json.loads(_prov_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in rows.items() if isinstance(v, dict) and _TASK_ID.match(k)} if isinstance(rows, dict) else {}


def _image_ref(args: dict) -> str | None:
    """A source image worth showing again: a local path, or a URL without its query (signed tokens)."""
    path = args.get("file_path")
    if isinstance(path, str) and path.strip() and not path.startswith("data:"):
        return path.strip()[:1024]
    url = args.get("image_url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:1024]
    return None


@router.post("/provenance")
def provenance(body: dict):
    task_id, tool = str(body.get("task_id") or ""), str(body.get("tool") or "")
    if not _TASK_ID.match(task_id) or not re.fullmatch(r"meshy_[a-z0-9_]{1,40}", tool):
        raise HTTPException(status_code=400, detail="task_id and a meshy_* tool required")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    row: dict = {"tool": tool, "recorded_at": int(time.time() * 1000)}
    for key in _PROV_TEXT:
        if isinstance(args.get(key), str) and args[key].strip():
            row[key] = args[key].strip()[:800]
    parent = next((args[k] for k in _PROV_PARENT if isinstance(args.get(k), str) and _TASK_ID.match(args[k])), None)
    if parent:
        row["parent"] = parent
    if image := _image_ref(args):
        row["image"] = image
    settings = {k: args[k] for k in _PROV_SETTINGS
                if isinstance(args.get(k), (str, int, float, bool)) or (k == "action_ids" and isinstance(args.get(k), list))}
    if settings:
        row["settings"] = {k: (v[:10] if isinstance(v, list) else v) for k, v in settings.items()}
    sid = body.get("session_id")
    if isinstance(sid, str) and _SESSION_ID.match(sid):
        row["session_id"] = sid
    rows = _load_prov()
    rows[task_id] = row
    if len(rows) > _PROV_CAP:
        rows = dict(sorted(rows.items(), key=lambda kv: kv[1].get("recorded_at") or 0)[-_PROV_CAP:])
    target = _prov_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    tmp.replace(target)
    return {"ok": True}


def _download_lineage(task_id: str) -> tuple[str | None, list[dict]]:
    """The MCP server's own record for downloaded models (any surface, CLI included): the project prompt and
    its task chain in order, from ``meshy_output/<project>/metadata.json``."""
    out_dir = _data_root() / "meshy_output"
    for meta_path in out_dir.glob("*/metadata.json") if out_dir.is_dir() else ():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tasks = [t for t in meta.get("tasks") or [] if isinstance(t, dict) and _TASK_ID.match(str(t.get("task_id") or ""))]
        if any(t["task_id"] == task_id for t in tasks):
            seen, chain = set(), []
            for t in tasks:
                if t["task_id"] not in seen:
                    seen.add(t["task_id"])
                    chain.append({"task_id": t["task_id"], "type": str(t.get("task_type") or ""), "stage": str(t.get("stage") or "")})
            return (meta.get("project_name") if isinstance(meta.get("project_name"), str) else None), chain
    return None, []


def _earlier_in(download_chain: list[dict], task: str) -> list[dict]:
    """Tasks before ``task`` in its download project (the MCP server records them in order)."""
    ids = [c["task_id"] for c in download_chain]
    # The MCP server types every text-to-3d stage alike; its stage says preview vs textured.
    def step(c: dict) -> str:
        return "meshy_text_to_3d_refine" if c["stage"] == "refined" else c["type"]
    return [{"task_id": c["task_id"], "tool": step(c)} for c in download_chain[:ids.index(task)]] if task in ids else []


@router.get("/details")
def details(task: str):
    if not _TASK_ID.match(task or ""):
        raise HTTPException(status_code=400, detail="task id required")
    prov = _load_prov()
    rec = prov.get(task, {})
    key = _api_key()
    row: dict = {}
    if key:
        try:
            row = _task(task, key)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise
    # Walk recorded parents for the chain, and find what was made from this task.
    chain, cur, hops = [], rec.get("parent"), 0
    while cur and hops < 12 and cur not in {c["task_id"] for c in chain}:
        chain.insert(0, {"task_id": cur, "tool": prov.get(cur, {}).get("tool")})
        cur, hops = prov.get(cur, {}).get("parent"), hops + 1
    children = [{"task_id": k, "tool": v.get("tool")} for k, v in prov.items() if v.get("parent") == task]
    project_prompt, download_chain = _download_lineage(task)
    # A remesh/rig/animation has no prompt of its own: each origin field comes from the nearest step that has
    # one (a text model's prompt; an image model's source image and texture prompt), labelled as inherited.
    own = {"prompt": rec.get("prompt") or (row.get("prompt") if isinstance(row.get("prompt"), str) else ""),
           "texture_prompt": rec.get("texture_prompt") or (row.get("texture_prompt") if isinstance(row.get("texture_prompt"), str) else ""),
           "image": rec.get("image")}
    inherited = None
    for field in ("prompt", "texture_prompt", "image"):
        if own[field]:
            continue
        for link in reversed(chain):
            if anc := prov.get(link["task_id"], {}).get(field):
                own[field], inherited = anc, inherited or link["task_id"]
                break
    if not own["prompt"] and not own["image"] and project_prompt and project_prompt not in ("text-to-3d", "image-to-3d"):
        own["prompt"], inherited = project_prompt, inherited or (download_chain[0]["task_id"] if download_chain else None)
    prompt, texture, image = own["prompt"], own["texture_prompt"], own["image"]
    image_local = bool(image) and not str(image).startswith(("http://", "https://"))
    if image_local:
        try:
            _resolve_readable(str(image))
        except HTTPException:
            image_local = False  # moved, or outside the folders the viewer may read
    style = rec.get("text_style_prompt") or (row.get("text_style_prompt") if isinstance(row.get("text_style_prompt"), str) else "")
    settings = dict(rec.get("settings") or {})
    if isinstance(row.get("model_type"), str) and row["model_type"]:
        settings.setdefault("model_type", row["model_type"])
    return {
        "task": task, "type": str(row.get("type") or rec.get("tool") or ""),
        "prompt": prompt or None, "prompt_from": inherited,
        "texture_prompt": texture or None, "style_prompt": style or None,
        "negative_prompt": rec.get("negative_prompt"),
        "image": image, "image_viewable": image_local,
        "settings": settings, "seed": row.get("seed") or None,  # 0 = a step with no seed (resize, rig, ...)
        "credits": row.get("consumed_credits"), "created_at": row.get("created_at") or rec.get("recorded_at"),
        "parents": chain or _earlier_in(download_chain, task),
        "children": children,
        "session_id": rec.get("session_id"),
        "recorded": bool(rec),
    }
