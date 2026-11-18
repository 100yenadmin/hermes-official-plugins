"""A multiplexed gateway runs each profile's Claude Code on that profile's own login.

Multiplexing never loads a served profile's ``.env`` into ``os.environ``, and Claude Code prefers
``CLAUDE_CODE_OAUTH_TOKEN`` over the login in ``CLAUDE_CONFIG_DIR``. So the child gets the profile's
token from core's secret scope, or loses the inherited token when ``claude_config_dir`` picks the login.
"""
import json
import logging
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from conftest import popen_from_run

from test_directsdk import FAKE
from test_directsdk_config_dir import _set_config_dir
from test_directsdk_setup import PRO

TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
AMBIENT = "sk-ant-oat01-ambient-gateway-token"
PROFILE = "sk-ant-oat01-served-profile-token"
REQUEST = dict(model="sonnet", messages=[{"role": "user", "content": "go"}],
               tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                        "parameters": {"type": "object", "properties": {}}}}])


SERVED_HOME = os.path.join(os.sep, "hermes-test", "profiles", "served")


@contextmanager
def multiplexed(scope, home=SERVED_HOME):
    """One served turn: multiplex semantics, with ``scope`` bound (``None``: no scope bound). Core stamps a served
    profile's scope with its home; ``home=None`` binds it unstamped, which core uses for the launch profile's own."""
    from agent.secret_scope import (reset_multiplex_context, reset_secret_scope, set_multiplex_context,
                                    set_secret_scope)
    mode = set_multiplex_context(True)
    bound = set_secret_scope(scope, profile_home=home) if scope is not None else None
    try:
        yield
    finally:
        if bound is not None:
            reset_secret_scope(bound)
        reset_multiplex_context(mode)


@pytest.fixture
def ambient(profile, monkeypatch):
    """The gateway process carries its own token, as a systemd unit or launch .env would."""
    monkeypatch.setenv(TOKEN, AMBIENT)
    return profile


def _apply(env):
    import directsdk_setup
    before = dict(os.environ)
    assert directsdk_setup.apply_profile_credential(env) is env
    assert dict(os.environ) == before
    return env


def test_the_profile_token_wins_over_the_gateways(ambient, tmp_path):
    with multiplexed({TOKEN: PROFILE}):
        assert _apply({TOKEN: AMBIENT, "UNRELATED": "kept"}) == {TOKEN: PROFILE, "UNRELATED": "kept"}
        # It wins over a selected login directory too: Claude Code would prefer it anyway.
        _set_config_dir(str(tmp_path / "login"))
        assert _apply({TOKEN: AMBIENT}) == {TOKEN: PROFILE}
    assert os.environ[TOKEN] == AMBIENT


@pytest.mark.parametrize("scope", [{}, {TOKEN: ""}, {TOKEN: "  "}], ids=["miss", "empty", "blank"])
def test_a_served_profile_never_runs_on_the_gateways_login(ambient, tmp_path, monkeypatch, scope):
    """#313: without a token of its own a served profile loses the launch profile's token, gateway headers and
    config directory. With no directory of its own either, Claude Code would fall back to ~/.claude under the
    inherited HOME (the launch profile's login), so the turn is refused before a spawn, naming both ways to give
    the profile a login. With a claude_config_dir it runs on that login."""
    import directsdk
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed(scope), pytest.raises(directsdk.ClaudeCodeLoggedOut) as raised:
            client.create(**REQUEST)
    finally:
        client.close()
    assert not (tmp_path / "tokens.jsonl").exists()
    assert "claude_config_dir" in str(raised.value) and TOKEN in str(raised.value)
    assert AMBIENT not in str(raised.value)
    setting = str(tmp_path / "login")
    _set_config_dir(setting)
    inherited = {TOKEN: AMBIENT, "ANTHROPIC_CUSTOM_HEADERS": "X-Gateway: launch", "CLAUDE_CONFIG_DIR": setting,
                 "UNRELATED": "kept"}
    with multiplexed(scope):
        env = _apply(dict(inherited))
    assert env == {"UNRELATED": "kept", "CLAUDE_CONFIG_DIR": setting}


def test_a_single_profile_install_keeps_its_own_token(ambient, tmp_path):
    """Not multiplexed: get_secret reads os.environ, the token the child inherited anyway."""
    _set_config_dir(str(tmp_path / "login"))
    assert _apply({TOKEN: AMBIENT}) == {TOKEN: AMBIENT}


def test_an_older_core_without_secret_scope_is_no_profile_token(ambient, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import raises ImportError
    assert _apply({TOKEN: AMBIENT}) == {TOKEN: AMBIENT}
    _set_config_dir(str(tmp_path / "login"))
    assert _apply({TOKEN: AMBIENT}) == {}


def test_the_token_is_never_logged_or_printed(ambient, tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with multiplexed({TOKEN: PROFILE}):
        _apply({})
    _set_config_dir(str(tmp_path / "login"))
    with multiplexed({}):
        _apply({TOKEN: AMBIENT})
    out, err = capsys.readouterr()
    for text in (caplog.text, out, err):
        assert PROFILE not in text and AMBIENT not in text


def _native(tmp_path, monkeypatch, **extra):
    """A fake native that records the token it was started with, run from Hermes' own environment."""
    import directsdk
    capture = tmp_path / "tokens.jsonl"
    probe = ("open(os.environ['TOKEN_CAPTURE'], 'a').write(json.dumps(os.environ.get(%r)) + '\\n')\n" % TOKEN)
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", probe + "rows=[]"), encoding="utf-8")
    for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CONFIG_DIR", "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOKEN_CAPTURE", str(capture))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    client = directsdk.Client(command=[sys.executable, str(script)])
    return client, lambda: [json.loads(line) for line in capture.read_text().splitlines()]


def test_native_spawns_run_on_the_serving_profiles_login(ambient, tmp_path, monkeypatch):
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed({TOKEN: PROFILE}):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
        _set_config_dir(str(tmp_path / "login"))
        with multiplexed({}):
            client.create(**REQUEST)
    finally:
        client.close()
    assert seen() == [PROFILE, None]
    assert os.environ[TOKEN] == AMBIENT


def test_an_explicit_client_env_keeps_its_login(ambient, tmp_path):
    import directsdk
    capture = tmp_path / "tokens.jsonl"
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace("rows=[]", "open(os.environ['TOKEN_CAPTURE'], 'a').write(json.dumps("
                                   "os.environ.get(%r)) + '\\n')\nrows=[]" % TOKEN), encoding="utf-8")
    _set_config_dir(str(tmp_path / "login"))
    client = directsdk.Client(command=[sys.executable, str(script)],
                              env={"PATH": os.defpath, "HOME": str(tmp_path), "TOKEN_CAPTURE": str(capture), TOKEN: "fixture"})
    try:
        with multiplexed({TOKEN: PROFILE}):
            client.create(**REQUEST)
    finally:
        client.close()
    assert [json.loads(line) for line in capture.read_text().splitlines()] == ["fixture"]


def test_the_logged_out_error_does_not_carry_the_token(ambient, tmp_path, monkeypatch):
    import directsdk
    client, _ = _native(tmp_path, monkeypatch, NATIVE_ERROR="authentication_failed:Not logged in")
    try:
        with multiplexed({TOKEN: PROFILE}), pytest.raises(directsdk.ClaudeCodeLoggedOut) as raised:
            client.create(**REQUEST)
    finally:
        client.close()
    assert PROFILE not in str(raised.value) and PROFILE not in repr(raised.value)


@pytest.mark.parametrize("probe", ["setup_status", "discover_models"])
def test_setup_probes_use_the_serving_profiles_login(ambient, tmp_path, monkeypatch, probe):
    import directsdk_setup
    captured = []
    def run(argv, **kwargs):
        captured.append(kwargs["env"].get(TOKEN))
        if argv[1:3] == ["auth", "status"]:
            output = PRO["auth"]
        else:
            output = {"type": "control_response", "response": {"response": {"models": [], "account": PRO["account"]}}}
        return SimpleNamespace(stdout=json.dumps(output), returncode=0)
    monkeypatch.setattr(directsdk_setup.subprocess, "run", run)
    monkeypatch.setattr(directsdk_setup.subprocess, "Popen", popen_from_run(run))
    env = {"PATH": os.defpath, "HOME": str(tmp_path), TOKEN: AMBIENT}
    with multiplexed({TOKEN: PROFILE}):
        getattr(directsdk_setup, probe)(command=[sys.executable], env=env)
    _set_config_dir(str(tmp_path / "login"))
    with multiplexed({}):
        getattr(directsdk_setup, probe)(command=[sys.executable], env=env)
    # The session fork settles the login first (its own /usage probe), so each probe spawns once more than on main.
    per_call = 2 if probe == "setup_status" else 3
    assert captured == [PROFILE] * per_call + [None] * per_call
    assert env[TOKEN] == AMBIENT


def test_the_launch_profiles_own_turn_keeps_its_login_under_multiplexing(ambient, tmp_path, monkeypatch):
    """eva fork: a Desktop backend serving several profiles multiplexes, so core counts every task as routed, the
    launch profile's own turns too. Core binds that profile's scope unstamped, and its login is the process's own:
    its turns keep the inherited login instead of being refused, as on one profile."""
    home = None
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with multiplexed({}, home=home):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        client.close()
    assert seen() == [AMBIENT]
    with multiplexed({}, home=home):
        assert _apply({TOKEN: AMBIENT, "UNRELATED": "kept"}) == {TOKEN: AMBIENT, "UNRELATED": "kept"}
    # A served profile in the same process is still refused without a login of its own.
    with multiplexed({}), pytest.raises(Exception) as raised:
        _apply({TOKEN: AMBIENT})
    assert type(raised.value).__name__ == "ClaudeCodeLoggedOut"


def test_cores_own_launch_turn_bind_keeps_the_launch_login(profile, tmp_path, monkeypatch):
    """eva's shape, through core's own binder for a launch-profile turn on a multi-profile host
    (tui_gateway.launch_profile_policy.launch_profile_runtime_scope): no token anywhere and no claude_config_dir,
    the login lives in ~/.claude. The turn runs on it instead of being refused."""
    monkeypatch.delenv(TOKEN, raising=False)
    try:
        from tui_gateway.launch_profile_policy import launch_profile_runtime_scope
    except ImportError:
        pytest.skip("core without launch_profile_runtime_scope")
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_constants import get_routing_process_hermes_home
    client, seen = _native(tmp_path, monkeypatch)
    mode = set_multiplex_context(True)
    try:
        with launch_profile_runtime_scope(get_routing_process_hermes_home()):
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        reset_multiplex_context(mode)
        client.close()
    assert seen() == [None]


def _settings(home, **settings):
    """Write ``settings`` as this plugin's settings in ``home``'s config.yaml."""
    from test_directsdk_config_dir import PLUGIN_NAME
    lines = "".join("        %s: %s\n" % (key, json.dumps(value)) for key, value in settings.items())
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n%s"
                                      % (PLUGIN_NAME, lines), encoding="utf-8")


@contextmanager
def _own_cron_fire():
    """The launch profile's own job fired in process by a multiplexing gateway, through core's binder
    (cron/scheduler.py _fire_secret_scope): its scope is stamped with the process home."""
    try:
        from cron.scheduler import _fire_secret_scope
    except ImportError:
        pytest.skip("core without the in-process cron fire scope")
    from agent.secret_scope import current_secret_scope_home, reset_multiplex_context, set_multiplex_context
    mode = set_multiplex_context(True)
    try:
        with _fire_secret_scope():
            assert current_secret_scope_home() is not None  # stamped: session.10's unstamped exemption misses it
            yield
    finally:
        reset_multiplex_context(mode)


def test_the_launch_profiles_own_cron_fire_keeps_its_login_when_it_says_so(profile, tmp_path, monkeypatch):
    """eva's daily cron job, 2026-11-18: core stamps the launch profile's own in-process fire with the process home,
    so session.10 counted it as served and refused it ("This profile has no Claude login of its own"). A profile whose
    login is the inherited one says so with inherit_launch_login: true; its fires then run on that login. Without the
    setting the refusal stands (claude_config_dir=~/.claude is no way out: Claude Code keys its Keychain login by
    config directory)."""
    import directsdk_setup
    from hermes_constants import get_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    home = get_hermes_home()
    with _own_cron_fire(), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})
    _settings(home, inherit_launch_login=True)
    client, seen = _native(tmp_path, monkeypatch)
    try:
        with _own_cron_fire():
            assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}
            assert client.create(**REQUEST).choices[0].message.content == "hello\n"
    finally:
        client.close()
    assert seen() == [None]


@pytest.mark.parametrize("value", ["true", "yes", 1, "on"])
def test_only_a_yaml_boolean_true_inherits_the_login(profile, monkeypatch, value):
    """A credential switch: a string or number that merely looks true does not open it."""
    import directsdk_setup
    from hermes_constants import get_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    _settings(get_hermes_home(), inherit_launch_login=value)
    with _own_cron_fire(), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})


@contextmanager
def _process_started_for(home):
    """This process as one started for ``home`` (a restart-safe cron worker, or a Desktop backend for that profile):
    core pins it as the routing process home."""
    import hermes_constants
    previous = hermes_constants._PINNED_PROCESS_HERMES_HOME
    hermes_constants.pin_process_hermes_home(home)
    try:
        yield
    finally:
        hermes_constants.pin_process_hermes_home(previous)


@contextmanager
def _served_turn(home):
    """A task of the profile at ``home``, bound as core binds it: home override plus a scope stamped with it."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        with multiplexed({}, home=str(home)):
            yield
    finally:
        reset_hermes_home_override(token)


def _profile_home(tmp_path, name, **settings):
    home = tmp_path / name
    home.mkdir()
    if settings:
        _settings(home, **settings)
    else:
        (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    return home


def test_another_profile_never_inherits_the_launch_login_through_the_setting(profile, tmp_path, monkeypatch):
    """The setting is read from the served profile's own config: the launch profile opting in gives a served profile
    nothing. A served profile opting in gets nothing either while this process was started for another profile:
    what it inherited is that profile's login."""
    import directsdk_setup
    from hermes_constants import get_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    _settings(get_hermes_home(), inherit_launch_login=True)
    for served in (_profile_home(tmp_path, "served-plain"), _profile_home(tmp_path, "served-opted", inherit_launch_login=True)):
        with _served_turn(served), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
            _apply({"UNRELATED": "kept"})


def test_a_backend_started_for_another_profile_never_lends_its_login_to_an_opted_in_profiles_job(profile, tmp_path,
                                                                                                monkeypatch):
    """Astra v2 finding: a Desktop backend started for profile X runs eva's job (Run now). Core binds eva's home, so
    the plugin reads eva's true, but the login this process inherited is X's. The opt-in counts only when the process
    was started for the profile that set it."""
    import directsdk_setup
    monkeypatch.delenv(TOKEN, raising=False)
    eva = _profile_home(tmp_path, "eva-home", inherit_launch_login=True)
    other = _profile_home(tmp_path, "other-home")
    with _process_started_for(other), _served_turn(eva), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})


def test_an_opted_in_profiles_own_cron_worker_keeps_the_inherited_login(profile, tmp_path, monkeypatch):
    """A restart-safe cron worker for eva's job: started with HERMES_HOME=eva (her process home) and her scope stamped
    with it. Opted in, the job runs on the login the worker inherited; not opted in, it is refused as before."""
    import directsdk_setup
    monkeypatch.delenv(TOKEN, raising=False)
    plain = _profile_home(tmp_path, "plain-home")
    with _process_started_for(plain), _served_turn(plain), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})
    eva = _profile_home(tmp_path, "eva-home", inherit_launch_login=True)
    with _process_started_for(eva), _served_turn(eva):
        assert _apply({"UNRELATED": "kept"}) == {"UNRELATED": "kept"}


def test_a_cron_worker_for_a_served_profile_is_still_refused(profile, tmp_path, monkeypatch):
    """A cron worker started with HERMES_HOME=<served profile> pins that home as its process home and stamps the
    profile's scope with it (cron/scheduler.py), while HOME and ~/.claude are still the launch profile's. A stamp equal
    to the process home is therefore not "the launch profile": with no login of its own the turn is refused."""
    import directsdk_setup
    from hermes_constants import get_routing_process_hermes_home
    monkeypatch.delenv(TOKEN, raising=False)
    with multiplexed({}, home=str(get_routing_process_hermes_home())), pytest.raises(directsdk_setup.ClaudeCodeLoggedOut):
        _apply({"UNRELATED": "kept"})
