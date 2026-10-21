"""Safety invariants of the Meshy dashboard router: what it will read, fetch, write and remember.

Real router code against a temp Hermes home + temp user home. The only stand-in is a network tripwire:
every refusal below must happen BEFORE the router reaches Meshy, so any urlopen call fails the test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
from pathlib import Path

import pytest
from fastapi import HTTPException

_SRC = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_TASK = "01a157b8-7317-7551-abc5-f99fe8c1236c"


@pytest.fixture
def api(tmp_path, monkeypatch):
    home, hermes = tmp_path / "home", tmp_path / "hermes"
    data = hermes / "plugin-data" / "agent-plugin-meshy"
    for d in (home, data, tmp_path / "outside"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MESHY_API_KEY", "msy-test-not-a-real-key")  # key present: only validation can stop a call

    def tripwire(*_a, **_k):
        raise AssertionError("network call reached before input validation")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    spec = importlib.util.spec_from_file_location("meshy_plugin_api_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.tmp = tmp_path
    mod.data = data
    return mod


def _status(fn, *args, **kwargs) -> int:
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value.status_code


def test_file_reads_only_models_and_images_inside_allowed_roots(api):
    model = api.data / "meshy_output" / "abc" / "model.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"glTF-binary")
    assert api.file(str(model))["name"] == "model.glb"

    secret = api.data / ".env"
    secret.write_text("MESHY_API_KEY=x")
    exe = api.data / "tool.exe"
    exe.write_bytes(b"MZ")
    outside = api.tmp / "outside" / "stolen.glb"
    outside.write_bytes(b"glTF")
    assert _status(api._resolve_readable, "model.glb") == 400  # relative
    assert _status(api._resolve_readable, str(secret)) == 415
    assert _status(api._resolve_readable, str(exe)) == 415
    assert _status(api.file, str(outside)) == 403

    link = api.data / "innocent.glb"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this Windows runner")
    assert _status(api.file, str(link)) == 403  # resolved target decides, not the link's location


def test_save_and_formats_validate_input_before_calling_meshy(api):
    outside = api.tmp / "outside" / "m.glb"
    outside.write_bytes(b"glTF")
    note = api.data / "notes.txt"
    note.write_text("x")
    assert _status(api.save, {"task": _TASK, "format": "exe"}) == 400
    for bad_task in ("../../etc", "..\\..\\Windows", "", _TASK + "/../x"):
        assert _status(api.save, {"task": bad_task, "format": "stl"}) == 400
        assert _status(api.formats, bad_task) == 400
    assert _status(api.save, {"task": _TASK, "format": "stl", "beside": str(outside)}) == 403
    assert _status(api.formats, _TASK, beside=str(outside)) == 403
    assert _status(api.save, {"task": _TASK, "format": "stl", "beside": str(note)}) == 415
    assert not (api.data / "meshy_output").exists()  # nothing written for a refused request


def test_remote_fetches_only_https_assets_meshy_ai(api):
    for url in ("https://assets.meshy.ai.evil.com/m.glb", "http://assets.meshy.ai/m.glb",
                "https://assets.meshy.ai@evil.com/m.glb", "https://evil.com/assets.meshy.ai/m.glb",
                "https://evilassets.meshy.ai/m.glb", "file:///etc/passwd"):
        assert _status(api.remote, url) == 400, url
    assert _status(api.remote, "https://assets.meshy.ai/x/secrets.env") == 415


def test_asset_index_records_only_allowed_model_paths_and_never_urls(api):
    project = Path.home() / "projects" / "demo" / "ship.glb"
    project.parent.mkdir(parents=True)
    project.write_bytes(b"glTF-ship")
    outside = api.tmp / "outside" / "x.glb"
    outside.write_bytes(b"glTF")

    api.file(str(project), task=_TASK)
    api._record_asset(outside, _TASK)
    api._record_asset(project.with_suffix(".png"), None)
    api.file(str(project), task="https://assets.meshy.ai/signed?Expires=1")

    rows = json.loads(api._index_path().read_text(encoding="utf-8"))
    assert [Path(r["path"]) for r in rows] == [project.resolve()]
    assert rows[0]["task_id"] == _TASK and len(rows[0]["sha256"]) == 64
    assert "http" not in api._index_path().read_text(encoding="utf-8")
    listed = api.local(limit=50)["files"]
    assert [(Path(f["path"]), f["task"]) for f in listed] == [(project.resolve(), _TASK)]


def test_actions_state_a_real_cost_and_only_what_the_task_supports(api, monkeypatch):
    """Card buttons send a chat message naming the cost; a task type the tool can't take gets no button."""
    records = {"preview": {"id": _TASK, "type": "text-to-3d-preview", "consumed_credits": 20, "prompt": "owl"},
               "image": {"id": _TASK, "type": "image-to-3d", "consumed_credits": 15},
               "rig": {"id": _TASK, "type": "rig", "consumed_credits": 5}}
    for kind, row in records.items():
        monkeypatch.setattr(api, "_task", lambda _t, _k, row=row: row)
        monkeypatch.setattr(api, "_meshy_get", lambda *_a, **_k: {"balance": 12})
        acts = {a["id"]: a for a in api.actions(_TASK)["actions"]}
        assert all(isinstance(a["cost"], int) and (f"{a['cost']} credit" in a["message"] or a["cost"] == 0)
                   for a in acts.values()), kind
        if kind == "preview":
            assert acts["regenerate"]["cost"] == 20 and not acts["regenerate"]["affordable"]  # 20 > balance 12
            assert "texture" in acts and "rig" not in acts
        if kind == "image":
            assert "regenerate" not in acts and "texture" not in acts and "rig" in acts
        if kind == "rig":
            assert list(acts) == ["animate"]
    assert _status(api.actions, "not-a-task-id") == 400


def _sessions(api, *ids):
    """This profile's state.db with these chats (the real table name and key column)."""
    import sqlite3
    with sqlite3.connect(api.tmp / "hermes" / "state.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO sessions VALUES (?)", [(i,) for i in ids])


def test_provenance_keeps_what_made_a_model_and_never_a_signed_url(api, monkeypatch):
    """The gallery's "Made from": a child step shows its origin's prompt; tokens, junk and another
    profile's chats are never stored."""
    parent, child = _TASK, "01a15965-4a40-718b-8912-4046ede4837f"
    _sessions(api, "sess-1")
    api.provenance({"task_id": parent, "tool": "meshy_image_to_3d", "session_id": "sess-1",
                    "args": {"image_url": "https://cdn.example.com/frog.png?Signature=SECRET&Expires=9",
                             "texture_prompt": "mossy green frog", "model_type": "smart-topology", "target_polycount": 15000}})
    api.provenance({"task_id": child, "tool": "meshy_remesh", "session_id": "sess-1",
                    "args": {"input_task_id": parent, "target_polycount": 8000, "topology": "triangle"}})
    stored = api._prov_path().read_text(encoding="utf-8")
    assert "SECRET" not in stored and "Signature" not in stored
    for bad in ({"task_id": "x", "tool": "meshy_rig"}, {"task_id": child, "tool": "rm -rf"}):
        assert _status(api.provenance, bad) == 400
    other = {"task_id": "01a15965-4a40-718b-8912-000000000001", "tool": "meshy_rig", "args": {}}
    assert _status(api.provenance, {**other, "session_id": "a-chat-on-another-profile"}) == 409
    assert _status(api.provenance, other) == 409

    monkeypatch.setattr(api, "_api_key", lambda: None)  # no key: details still answers from the record
    d = api.details(child)
    assert d["texture_prompt"] == "mossy green frog" and d["prompt_from"] == parent
    assert d["image"] == "https://cdn.example.com/frog.png" and d["settings"]["target_polycount"] == 8000
    assert [p["task_id"] for p in d["parents"]] == [parent]
    assert [c["task_id"] for c in api.details(parent)["children"]] == [child]
    assert api.details(parent)["session_id"] == "sess-1"


def test_parallel_writes_keep_every_record_and_tasks_get_their_own_folder(api):
    """Completions of a parallel tool_call arrive together, as do /file reads; two tasks a millisecond
    apart share their first 8 characters."""
    from concurrent.futures import ThreadPoolExecutor
    _sessions(api, "s")
    ids = [f"01a15976-d838-7532-99d8-{i:012x}" for i in range(24)]
    models = []
    for i in range(12):
        m = api.data / "meshy_output" / f"m{i}" / "model.glb"
        m.parent.mkdir(parents=True)
        m.write_bytes(b"glTF" + bytes([i]) * 64)
        models.append(m)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: api.provenance({"task_id": i, "tool": "meshy_remesh", "args": {}, "session_id": "s"}), ids))
        list(pool.map(api._record_asset, models))
    assert set(ids) <= set(api._load_prov())
    assert {str(m.resolve()) for m in models} <= {r["path"] for r in api._load_index()}
    assert api._save_target(ids[0], None)[0] != api._save_target(ids[1], None)[0]


def _fake_assets(monkeypatch, api, files: dict[str, bytes | int], delay: float = 0.0) -> dict[str, int]:
    """Serve ``https://assets.meshy.ai/<name>`` from ``files`` (an int = that HTTP error); count each fetch."""
    import io
    import threading
    import time
    import urllib.error
    hits: dict[str, int] = {}
    lock = threading.Lock()

    def urlopen(url, timeout=None):
        name = str(url).rsplit("/", 1)[-1]
        with lock:
            hits[name] = hits.get(name, 0) + 1
        time.sleep(delay)  # long enough for a second request to overlap the first
        if isinstance(files[name], int):
            raise urllib.error.HTTPError(str(url), files[name], "fixture", {}, None)
        return io.BytesIO(files[name])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return hits


def test_save_downloads_a_file_once_and_a_retry_finishes_failed_companions(api, monkeypatch):
    """Two clicks on one export download it once (both succeed), and an OBJ whose .mtl failed is completed
    by the retry instead of being reported saved without its material."""
    from concurrent.futures import ThreadPoolExecutor
    a = "https://assets.meshy.ai/"
    row = {"id": _TASK, "model_urls": {"fbx": a + "m.fbx", "obj": a + "m.obj", "mtl": a + "m.mtl"}}
    monkeypatch.setattr(api, "_task", lambda _t, _k: row)
    files = {"m.fbx": b"FBX" * 1000, "m.obj": b"mtllib material.mtl\nv 0 0 0\n", "m.mtl": 503}
    hits = _fake_assets(monkeypatch, api, files, delay=0.2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = list(pool.map(lambda _: api.save({"task": _TASK, "format": "fbx"}), range(2)))
    assert [s["bytes"] for s in saved] == [3000, 3000] and hits["m.fbx"] == 1

    folder = api.data / "meshy_output" / _TASK
    with pytest.raises(Exception):
        api.save({"task": _TASK, "format": "obj"})
    files["m.mtl"] = b"newmtl material\n"
    api.save({"task": _TASK, "format": "obj"})
    assert (folder / "model.mtl").read_text(encoding="utf-8") == "newmtl material\n" and hits["m.mtl"] == 2
    assert (folder / "model.obj").read_bytes().startswith(b"mtllib model.mtl\n")
    assert not list(folder.glob("*.part"))  # no staging file left behind
