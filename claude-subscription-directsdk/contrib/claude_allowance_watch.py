#!/usr/bin/env python3
"""Hermes cron script (no_agent): tell the owner when the agent's Claude weekly allowance runs low.

Install: copy into ``$HERMES_HOME/scripts/`` and add a no_agent cron job that runs it every 10 minutes,
delivers to the owner's private chat and sets ``attach_to_session: false``. Hermes sends whatever this
prints, and a run that prints nothing is silent.

It reads the plan limits through this plugin's ``fetch_usage()``: Claude Code's own ``get_usage`` request,
which makes no model call and keeps the login inside the CLI. It prints one line the first time a weekly
limit reaches 75%, 90% or 100% in a reset period, and one line a day once readings have failed for two
hours. It always exits 0, because Hermes alerts on every tick of a failing script.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_DIRS = ('claude-subscription-directsdk-experimental', 'claude-subscription-directsdk')
THRESHOLDS = (75, 90, 100)
# Cron fires every 10 minutes; a reading is taken only as often as the worst weekly limit warrants.
READ_EVERY = timedelta(minutes=60)
READ_EVERY_BUSY, BUSY_FROM = timedelta(minutes=20), 50
EVERY_TICK_FROM, USED_UP = 70, 100
TICK_SLACK = timedelta(seconds=30)
METER_GRACE, METER_REPEAT = timedelta(hours=2), timedelta(hours=24)
# Reset times within an hour of each other are one period: weekly periods are a week apart.
SAME_PERIOD = timedelta(hours=1)
NO_WEEKLY = 'the reply had no current weekly limits'


def _time(value):
    """An aware datetime from an ISO string, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _past(value, now):
    """A stored time, or None when it is missing or later than now (the clock went back)."""
    moment = _time(value)
    return moment if moment is not None and moment <= now else None


def _local(moment, fmt):
    return moment.astimezone().strftime(fmt)


def _scope_name(scope, part):
    value = scope.get(part) if isinstance(scope.get(part), dict) else {}
    return value.get('display_name') if isinstance(value.get('display_name'), str) else ''


def weekly_limits(rate_limits, now):
    """``(current, unsure)`` for the weekly limits in a ``get_usage`` reply. ``current`` lists
    ``(key, label, percent, resets_at)`` for each limit whose reset time is still ahead; ``unsure`` holds the
    percents of limits whose reset time is missing or past (a period that is over), which cannot be told
    once per period. An entry without a numeric percent is skipped, never read as 0%. A limit scoped to a
    model or a surface keeps that name. The session (5-hour) limit is left out: it clears within hours,
    and Hermes already answers a refused turn in its chat."""
    entries = rate_limits.get('limits') if isinstance(rate_limits, dict) else None
    current, unsure = [], []
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get('kind'), str):
            continue
        kind = entry['kind']
        if entry.get('group') != 'weekly' and not kind.startswith('weekly'):
            continue
        percent, resets_at = entry.get('percent'), _time(entry.get('resets_at'))
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
            continue
        if resets_at is None or resets_at <= now:
            unsure.append(float(percent))
            continue
        scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
        model, surface = _scope_name(scope, 'model'), _scope_name(scope, 'surface')
        name = ' '.join(part.removeprefix('Claude ') for part in (model, surface) if part)  # "her Claude …"
        current.append((f'{kind}:{model}:{surface}', f'{name} weekly limit' if name else 'weekly limit',
                        float(percent), resets_at))
    return current, unsure


def _told(stored, resets_at):
    """Whether a notice stored with reset time ``stored`` belongs to the period ending at ``resets_at``."""
    prior = _time(stored)
    return prior is not None and abs(prior - resets_at) <= SAME_PERIOD


def _notice(label, percent, threshold, resets_at):
    when = _local(resets_at, '%a %d %b %H:%M')
    if threshold >= USED_UP:
        return f"eva has used up her Claude {label} (resets {when}). She can't answer on Claude until it resets."
    return (f"Heads-up: eva has used {int(min(percent, 100))}% of her Claude {label} (resets {when}). If it runs "
            "out she can't answer on Claude until then. You can wait, switch her model, or cut her usage.")


def decide(limits, state, now, worst):
    """``(lines, state)`` after a reading: one line per current limit whose highest crossed threshold has
    not been told in this period. Crossing 90% straight from below 75% sends only the 90% line.
    ``told`` keeps one reset time per limit and threshold, replaced each period."""
    told = dict(state.get('told') or {})
    lines = []
    for key, label, percent, resets_at in limits:
        crossed = [threshold for threshold in THRESHOLDS if percent >= threshold]
        if crossed and not _told(told.get(f'{key}|{crossed[-1]}'), resets_at):
            lines.append(_notice(label, percent, crossed[-1], resets_at))
        for threshold in crossed:
            told[f'{key}|{threshold}'] = resets_at.isoformat()
    reading = [{'limit': key, 'percent': percent, 'resets_at': resets_at.isoformat()}
               for key, _, percent, resets_at in limits]
    return lines, dict(state, told=told, last_reading=reading, worst=worst)


def failed(reason, state, now):
    """``(lines, state)`` after a failed reading: one line once readings have failed for two hours, then
    at most one a day, so a broken meter is never silent for long."""
    since = _past(state.get('failing_since'), now) or now
    state = dict(state, failing_since=since.isoformat(), last_error=reason)
    told = _past(state.get('meter_notice_at'), now)
    if now - since < METER_GRACE - TICK_SLACK or (told is not None and now - told < METER_REPEAT - TICK_SLACK):
        return [], state
    state['meter_notice_at'] = now.isoformat()
    return [f"eva's Claude usage check has not worked since {_local(since, '%a %H:%M')} ({reason}), so there "
            "will be no early warning before her weekly limit runs out."], state


def due(state, now):
    """Whether this tick takes a reading: hourly, every 20 minutes from 50%, every tick from 70%, and back to
    every 20 minutes once a limit is used up (nothing is left to tell until it resets)."""
    last = _past(state.get('last_attempt_at'), now)
    if last is None:
        return True
    worst = state.get('worst', 0)
    if EVERY_TICK_FROM <= worst < USED_UP:
        return True
    every = READ_EVERY_BUSY if worst >= BUSY_FROM else READ_EVERY
    return now - last >= every - TICK_SLACK


def reader_env(environ=None):
    """The environment the agent's own Claude calls see. Hermes can point a cron script's HOME at the
    profile home while the gateway, and so every model call, keeps the real one; the login is looked up
    under HOME unless a Claude config dir is set."""
    env = dict(os.environ if environ is None else environ)
    real = env.get('HERMES_REAL_HOME')
    if real and env.get('HOME') != real and not (env.get('CLAUDE_CONFIG_DIR')
                                                 or env.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR')):
        env['HOME'] = real
    return env


def plugin_fetch(home):
    """The plugin's ``fetch_usage`` as a no-argument callable, or a callable returning why it is missing."""
    for name in PLUGIN_DIRS:
        plugin = home / 'plugins' / name
        if (plugin / 'directsdk_setup.py').is_file():
            sys.path.insert(0, str(plugin))
            import directsdk_setup
            return lambda: directsdk_setup.fetch_usage(env=reader_env(), timeout=30)
    return lambda: (None, 'the Claude plugin is not installed in this profile')


def profile_home(script=None):
    """Cron runs scripts from ``$HERMES_HOME/scripts/``, so the profile home is that folder's parent; a copy
    kept anywhere else uses HERMES_HOME."""
    here = Path(script or __file__).resolve().parent
    if here.name == 'scripts' or not os.environ.get('HERMES_HOME'):
        return here.parent
    return Path(os.environ['HERMES_HOME'])


def _clean(data):
    """The saved state with each field in the type the script relies on; anything else is dropped, so a
    damaged file costs at most one repeated notice, never a silent watcher."""
    state = {field: data[field] for field in ('last_attempt_at', 'failing_since', 'meter_notice_at', 'last_error')
             if isinstance(data.get(field), str)}
    worst = data.get('worst')
    if isinstance(worst, (int, float)) and not isinstance(worst, bool) and worst == worst:
        state['worst'] = float(worst)
    if isinstance(data.get('told'), dict):
        state['told'] = {key: value for key, value in data['told'].items()
                         if isinstance(key, str) and isinstance(value, str)}
    if isinstance(data.get('last_reading'), list):  # for status checks only; the script never reads it back
        state['last_reading'] = [row for row in data['last_reading'] if isinstance(row, dict)]
    return state


def _load(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return _clean(data) if isinstance(data, dict) else {}


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + '.tmp')
    scratch.write_text(json.dumps(state, sort_keys=True), encoding='utf-8')
    os.replace(scratch, path)


def main(home=None, now=None, fetch=None):
    """One cron tick. The state is saved before anything is printed, so a state file that cannot be
    written means silence, never the same notice on every tick."""
    try:
        home = Path(home) if home else profile_home()
        path = home / 'cache' / 'claude_allowance_watch' / 'state.json'
        state = _load(path)
        now = now or datetime.now(timezone.utc)
        if not due(state, now):
            return 0
        state['last_attempt_at'] = now.isoformat()
        try:
            rate_limits, reason = (fetch or plugin_fetch(home))()
        except Exception as exc:  # the reader itself broke: report its type only, never its message
            rate_limits, reason = None, f'{type(exc).__name__} while reading'
        try:
            current, unsure = weekly_limits(rate_limits, now)
            if current or unsure:
                worst = max([percent for _, _, percent, _ in current] + unsure)
                lines, state = decide(current, state, now, worst)
                high = [percent for percent in unsure if percent >= THRESHOLDS[0]]
                if high:  # a high limit we cannot place in a period must not pass as a good reading
                    more, state = failed(f'a weekly limit at {int(min(max(high), 100))}% had no current reset time',
                                         state, now)
                    lines += more
                else:
                    state = dict(state, failing_since=None, last_error=None)
            else:
                lines, state = failed(str(reason or NO_WEEKLY)[:160], state, now)
        except Exception as exc:  # a reply the watcher cannot read counts as a failed reading
            lines, state = failed(f'{type(exc).__name__} in the watcher', state, now)
        _save(path, state)
        if lines:
            print('\n'.join(lines))
    except Exception as exc:
        print(f'claude_allowance_watch: {type(exc).__name__}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
