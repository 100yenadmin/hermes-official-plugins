"""Tests for the `hermes xchat` CLI — key-blob overwrite protection and
secure blob writes. No network, no native SDK.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from plugins.platforms.xchat import cli as xchat_cli


def test_write_private_blob_mode_0600(tmp_path):
    path = tmp_path / "private_keys.b64"
    xchat_cli._write_private_blob(path, "YmxvYg==")
    assert path.read_text(encoding="utf-8") == "YmxvYg==\n"
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600


def test_write_private_blob_tightens_existing_loose_mode(tmp_path):
    path = tmp_path / "private_keys.b64"
    path.write_text("old\n", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(path, 0o644)
    xchat_cli._write_private_blob(path, "bmV3")
    assert path.read_text(encoding="utf-8") == "bmV3\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_register_refuses_to_overwrite_orphan_blob(monkeypatch, tmp_path, capsys):
    """A key blob with a missing/corrupt registration marker must NOT be
    silently regenerated over — no forward secrecy means that permanently
    kills every existing conversation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("XCHAT_USER_ID", "999")

    blob = xchat_cli._blob_path()
    blob.write_text("ZXhpc3Rpbmc=\n", encoding="utf-8")
    assert not xchat_cli._marker_path().exists()  # orphan: no marker

    rc = xchat_cli.cmd_register(force=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "PERMANENTLY" in out
    # The blob is untouched.
    assert blob.read_text(encoding="utf-8") == "ZXhpc3Rpbmc=\n"


def test_register_force_backs_up_existing_blob(monkeypatch, tmp_path, capsys):
    """--force mints a new identity but first backs the old blob up."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("XCHAT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("XCHAT_USER_ID", "999")

    blob = xchat_cli._blob_path()
    blob.write_text("b2xk\n", encoding="utf-8")

    class FakeCrypto:
        def generate_and_register_payload(self):
            return {
                "registration": {
                    "public_key": {"public_key": "PK"},
                },
                "version": "1",
                "private_keys_b64": "bmV3YmxvYg==",
            }

    class FakeApi:
        def __init__(self, *a, **kw):
            pass

        async def get_public_keys(self, user_id):
            # Pretend our key is already registered — skips the POST.
            return [{"public_key": "PK", "public_key_version": "1"}]

        async def aclose(self):
            pass

    import plugins.platforms.xchat.api as api_mod
    import plugins.platforms.xchat.crypto as crypto_mod

    monkeypatch.setattr(api_mod, "XChatApi", FakeApi)
    monkeypatch.setattr(crypto_mod, "XChatCrypto", FakeCrypto)
    saved = {}
    monkeypatch.setattr(xchat_cli, "_save_env", lambda k, v: saved.update({k: v}))

    rc = xchat_cli.cmd_register(force=True)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Backed up the previous key blob" in out
    backups = list(blob.parent.glob("private_keys.b64.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "b2xk\n"
    assert blob.read_text(encoding="utf-8") == "bmV3YmxvYg==\n"
