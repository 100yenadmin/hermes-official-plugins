"""Safety invariants of the Meshy dashboard router: what it will read, fetch, write and remember.

Real router code against a temp Hermes home + temp user home. The only stand-in is a network tripwire:
every refusal below must happen BEFORE the router reaches Meshy, so any urlopen call fails the test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
import urllib.error
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


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
@pytest.mark.parametrize("operation", ["preview", "download"])
def test_asset_failures_never_claim_the_api_key_was_rejected(api, monkeypatch, status, operation):
    url = "https://assets.meshy.ai/model.glb"

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(url, status, "asset error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(HTTPException) as err:
        if operation == "preview":
            api.remote(url)
        else:
            api._download(url, api.data / "model.glb")
    assert "key" not in err.value.detail.lower()
    assert err.value.status_code == (410 if status in (401, 403, 404, 410) else 502)
    if status == 403:
        # API authentication remains a different failure from a denied CDN signature.
        assert "API key" in api._meshy_error(urllib.error.HTTPError(url, status, "", {}, None)).detail


def test_gallery_includes_image_sets_expiry_and_local_fallback(api, monkeypatch):
    image_id = "01a15965-4a40-718b-8912-4046ede4837f"
    image2_id = "01a15965-4a40-718b-8912-4046ede48380"
    local = api.data / "ship.glb"
    local.write_bytes(b"local copy")
    api.file(str(local), task=_TASK)
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    records = {
        "/openapi/v1/image-to-3d": [{"id": _TASK, "status": "SUCCEEDED", "created_at": 100,
                                    "expires_at": 999000, "consumed_credits": 15,
                                    "model_urls": {"glb": "https://assets.meshy.ai/ship.glb"}}],
        "/openapi/v1/text-to-image": [{"id": image_id, "status": "SUCCEEDED", "created_at": 200,
                                      "expires_at": 1100000, "consumed_credits": 3,
                                      "image_urls": ["https://assets.meshy.ai/front.png", "https://assets.meshy.ai/back.png"]}],
        "/openapi/v1/image-to-image": [{"id": image2_id, "status": "SUCCEEDED", "created_at": 300,
                                       "expires_at": 0, "image_urls": ["https://assets.meshy.ai/edit.png"]}],
    }
    monkeypatch.setattr(api, "_meshy_get", lambda path, *_a, **_k: records.get(path.split("?")[0], []))
    result = api.models(limit=10)["models"]
    assert [r["task_id"] for r in result] == [image2_id, image_id, _TASK]
    model, image, edited = result[2], result[1], result[0]
    assert model["kind"] == "model" and model["expired"]
    assert Path(model["local_path"]) == local.resolve()
    assert model["credits"] == 15  # available without the team-only Usage API
    assert image["kind"] == "image" and not image["expired"]
    assert len(image["image_urls"]) == 2 and image["glb_url"] is None
    assert not edited["expired"]  # zero/absent expiry is not evidence of expiration


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


def test_provenance_keeps_what_made_a_model_and_never_a_signed_url(api, monkeypatch):
    """The gallery's "Made from": a child step shows its origin's prompt; tokens and junk are never stored."""
    parent, child = _TASK, "01a15965-4a40-718b-8912-4046ede4837f"
    api.provenance({"task_id": parent, "tool": "meshy_image_to_3d", "session_id": "sess-1",
                    "args": {"image_url": "https://cdn.example.com/frog.png?Signature=SECRET&Expires=9",
                             "texture_prompt": "mossy green frog", "model_type": "smart-topology", "target_polycount": 15000}})
    api.provenance({"task_id": child, "tool": "meshy_remesh",
                    "args": {"input_task_id": parent, "target_polycount": 8000, "topology": "triangle"}})
    stored = api._prov_path().read_text(encoding="utf-8")
    assert "SECRET" not in stored and "Signature" not in stored
    for bad in ({"task_id": "x", "tool": "meshy_rig"}, {"task_id": child, "tool": "rm -rf"}):
        assert _status(api.provenance, bad) == 400

    monkeypatch.setattr(api, "_api_key", lambda: None)  # no key: details still answers from the record
    d = api.details(child)
    assert d["texture_prompt"] == "mossy green frog" and d["prompt_from"] == parent
    assert d["image"] == "https://cdn.example.com/frog.png" and d["settings"]["target_polycount"] == 8000
    assert [p["task_id"] for p in d["parents"]] == [parent]
    assert [c["task_id"] for c in api.details(parent)["children"]] == [child]
    assert api.details(parent)["session_id"] == "sess-1"


def test_image_details_recovers_provider_prompt_without_local_provenance(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    def get(path, *_a, **_k):
        if path == f"/openapi/v1/text-to-image/{_TASK}":
            return {"id": _TASK, "type": "text-to-image", "prompt": "low poly mech concept"}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)
    monkeypatch.setattr(api, "_meshy_get", get)
    assert api.details(_TASK)["prompt"] == "low poly mech concept"


def test_gallery_hides_expired_unsaved_assets_and_deduplicates_tasks(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    current = {"id": _TASK, "type": "image-to-image", "status": "SUCCEEDED", "created_at": 100,
               "expires_at": 2000000, "image_urls": ["https://assets.meshy.ai/current.png"]}
    old = {**current, "id": "01a15965-4a40-718b-8912-4046ede4837f", "created_at": 200,
           "expires_at": 999000}
    def get(path, *_a, **_k):
        if path.split("?")[0] in {"/openapi/v1/text-to-image", "/openapi/v1/image-to-image"}:
            return [old, current]
        return []
    monkeypatch.setattr(api, "_meshy_get", get)
    result = api.models(limit=1)["models"]
    assert len(result) == 1  # expired rows must not consume the visible limit
    assert result[0]["task_id"] == _TASK
    assert result[0]["task_type"] == "image-to-image"


@pytest.fixture
def archive(api, monkeypatch):
    clock = [1000]
    prefs = {**api._SETTING_DEFAULTS, "auto_save": True}
    assets, downloaded = [], []
    monkeypatch.setattr(api, "_settings", lambda: prefs)
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api.time, "time", lambda: clock[0])
    monkeypatch.setattr(api, "_list_models", lambda *_a, **_k: {"models": assets})
    def download(url, dest):
        downloaded.append(url)
        dest.write_bytes(b"asset")
    monkeypatch.setattr(api, "_download", download)
    api.autosave()  # establish the opt-in boundary before any new tasks exist
    api.archive_clock, api.archive_prefs = clock, prefs
    api.archive_assets, api.archive_downloads = assets, downloaded
    return api


@pytest.mark.parametrize("kind", ["image", "model"])
def test_autosave_preserves_new_outputs_once_and_lists_them_offline(archive, monkeypatch, kind):
    api = archive
    api.archive_clock[0] = 1001
    api.archive_assets.append({"task_id": _TASK, "kind": kind, "created_at": 1001000,
                               "prompt": "Mech", "local_path": None})
    row = {"image_urls": ["https://assets.meshy.ai/a.png?Signature=SECRET", "https://assets.meshy.ai/b.png"],
           "model_urls": {"glb": "https://assets.meshy.ai/a.glb"}}
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: row)
    first = api.autosave()
    expected = 2 if kind == "image" else 1
    assert first["files"] == expected and first["bytes"] == expected * 5
    api.autosave()
    assert len(api.archive_downloads) == expected
    assert "SECRET" not in (api.data / "autosave.json").read_text()
    assert "https://" not in (api.data / "autosave.json").read_text()
    monkeypatch.setattr(api, "_api_key", lambda: None)
    files = api.local(limit=200)["files"]
    assert len(files) == 1 and files[0]["kind"] == kind
    assert files[0]["task"] == _TASK and files[0]["prompt"] == "Mech"
    assert api.file(files[0]["path"])["bytes"] == 5
    if kind == "image":
        assert len(files[0]["images"]) == 2


def test_autosave_skips_history_and_off_period_but_uses_completion_time(archive, monkeypatch):
    api = archive
    def asset(created, finished=None):
        return {"task_id": _TASK, "kind": "image", "created_at": created, "finished_at": finished, "prompt": "Mech"}
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png"]})
    api.archive_assets[:] = [asset(999000)]
    api.autosave()
    assert not api.archive_downloads
    api.archive_prefs["auto_save"] = False
    api.archive_assets[:] = [asset(1001000)]
    api.autosave()
    assert not api.archive_downloads
    api.archive_clock[0] = 1002
    api.archive_prefs["auto_save"] = True
    api.autosave()
    assert not api.archive_downloads  # enabling does not backfill tasks completed while off
    api.archive_assets[:] = [asset(999000, 1003000)]
    api.autosave()
    assert len(api.archive_downloads) == 1  # an older task that finishes after enabling is new
    api.archive_prefs["auto_save"] = False
    api.autosave()
    assert api.storage()["files"] == 1  # disabling never deletes files


def test_autosave_reports_failure_retries_and_does_not_count_partial_files_as_saved(archive, monkeypatch):
    api = archive
    api.archive_assets.append({"task_id": _TASK, "kind": "image", "created_at": 1001000, "prompt": "Mech"})
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png"]})
    def fail(*_a):
        raise HTTPException(status_code=502, detail="Asset download failed")
    monkeypatch.setattr(api, "_download", fail)
    assert api.autosave()["failures"][0]["task"] == _TASK
    assert api.local(limit=200)["files"] == []
    monkeypatch.setattr(api, "_download", lambda _url, dest: dest.write_bytes(b"saved"))
    assert api.autosave()["failures"] == []
    assert len(api.local(limit=200)["files"]) == 1


def test_storage_counts_companions_and_indexed_files_once_and_is_profile_scoped(api, monkeypatch):
    folder = api.data / "meshy_output" / "project"
    folder.mkdir(parents=True)
    model = folder / "model.glb"
    model.write_bytes(b"model")
    (folder / "texture.png").write_bytes(b"texture")
    (folder / "interrupted.part").write_bytes(b"partial")
    api._record_asset(model, _TASK)
    summary = api.storage()
    assert summary["files"] == 2 and summary["bytes"] == 12
    other = api.data.parent / "other-profile"
    other.mkdir()
    monkeypatch.setattr(api, "_data_root", lambda: other)
    assert api.storage()["files"] == 0
    assert api.local(limit=200)["files"] == []


def test_autosave_skips_existing_model_copies_without_starving_new_images(archive, monkeypatch):
    api = archive
    api.archive_assets[:] = [{"task_id": _TASK, "kind": "model", "created_at": 1001000,
                             "local_path": "/saved/model.glb"}] * 4
    api.archive_assets.append({"task_id": _TASK, "kind": "image", "created_at": 1001000, "prompt": "Mech"})
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png"]})
    api.autosave()
    assert len(api.archive_downloads) == 1


def test_autosave_keeps_partial_images_and_repairs_missing_glb_variants(archive, monkeypatch):
    api = archive
    api.archive_assets.append({"task_id": _TASK, "kind": "image", "created_at": 1001000, "prompt": "Mech"})
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png", "https://assets.meshy.ai/b.png"]})
    def partial(url, dest):
        if url.endswith("b.png"):
            raise HTTPException(status_code=502, detail="Temporary failure")
        dest.write_bytes(b"first")
    monkeypatch.setattr(api, "_download", partial)
    assert api.autosave()["failures"]
    assert len(api.local(limit=200)["files"][0]["images"]) == 1
    monkeypatch.setattr(api, "_download", lambda _url, dest: dest.write_bytes(b"second"))
    assert api.autosave()["failures"] == []
    assert len(api.local(limit=200)["files"][0]["images"]) == 2
    assert (api._archive_root() / _TASK / "image-1.png").read_bytes() == b"first"
    # A different model task has several self-contained GLBs; deleting one must be repairable.
    model_id = "01a15965-4a40-718b-8912-4046ede4837f"
    asset = {"task_id": model_id, "kind": "model", "created_at": 1001000, "prompt": "Rig", "local_path": None}
    api.archive_assets[:] = [asset]
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"result": {
        "rigged_character_glb_url": "https://assets.meshy.ai/rig.glb",
        "basic_animations": {"walking_glb_url": "https://assets.meshy.ai/walk.glb"}}})
    api.autosave()
    paths = api._archive_state()["tasks"][model_id]["paths"]
    assert len(paths) == 2
    Path(paths[1]).unlink()
    asset["local_path"] = paths[0]
    api.autosave()
    assert Path(paths[1]).is_file()


def test_autosave_rotates_failed_attempts_and_bounds_each_pass(archive, monkeypatch):
    api = archive
    seen = []
    for i in range(5):
        api.archive_assets.append({"task_id": f"01a15965-4a40-718b-8912-4046ede4837{i}",
                                   "kind": "image", "created_at": 1001000, "prompt": "Mech"})
    def fail(task, *_a, **_k):
        seen.append(task)
        raise HTTPException(status_code=502, detail="Failed")
    monkeypatch.setattr(api, "_task", fail)
    api.autosave()
    assert len(seen) == 3
    api.autosave()
    assert len(seen) == 6 and len(set(seen)) == 5


def test_autosave_persists_disable_during_download_and_resets_on_reenable(archive, monkeypatch):
    api = archive
    api.archive_assets.append({"task_id": _TASK, "kind": "image", "created_at": 1001000, "prompt": "Mech"})
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png"]})
    def disable(_url, dest):
        dest.write_bytes(b"saved")
        api.archive_prefs["auto_save"] = False
    monkeypatch.setattr(api, "_download", disable)
    api.autosave()
    assert not api._archive_state()["enabled"]
    api.archive_clock[0] = 1010
    api.archive_prefs["auto_save"] = True
    api.autosave()
    assert api._archive_state()["since"] == 1010000


def test_autosave_discovery_keeps_endpoint_results_beyond_gallery_limit(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    def get(path, *_a, **_k):
        base = path.split("?")[0]
        if base in {"/openapi/v1/text-to-image", "/openapi/v1/image-to-image"}:
            offset = 0 if base.endswith("text-to-image") else 60
            return [{"id": f"01a15965-4a40-718b-8912-{i + offset:012x}", "status": "SUCCEEDED",
                     "created_at": i + offset, "image_urls": ["https://assets.meshy.ai/a.png"]} for i in range(60)]
        return []
    monkeypatch.setattr(api, "_meshy_get", get)
    assert len(api.models(limit=60)["models"]) == 60
    assert len(api._list_models(60, truncate=False)["models"]) == 120



def test_autosave_reenable_retries_tracked_tasks_and_keeps_survivors_after_failed_repair(archive, monkeypatch):
    api = archive
    api.archive_assets.append({"task_id": _TASK, "kind": "image", "created_at": 1001000, "prompt": "Mech"})
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": [f"https://assets.meshy.ai/{i}.png" for i in range(3)]})
    api.autosave()
    paths = api._archive_state()["tasks"][_TASK]["paths"]
    Path(paths[1]).unlink()
    def fail(*_a):
        raise HTTPException(status_code=502, detail="Failed to repair")
    monkeypatch.setattr(api, "_download", fail)
    assert api.autosave()["failures"]
    assert api.local(limit=200)["files"][0]["images"] == [paths[0], paths[2]]
    api.archive_prefs["auto_save"] = False
    api.autosave()
    api.archive_clock[0] = 1010
    api.archive_prefs["auto_save"] = True
    monkeypatch.setattr(api, "_download", lambda _url, dest: dest.write_bytes(b"repaired"))
    assert api.autosave()["failures"] == []
    assert api.local(limit=200)["files"][0]["images"] == paths
