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
EVERY_TICK_FROM = 70
TICK_SLACK = timedelta(seconds=30)
METER_GRACE, METER_REPEAT = timedelta(hours=2), timedelta(hours=24)
NO_WEEKLY = 'the reply had no weekly limits'


def _time(value):
    """An aware datetime from an ISO string, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _local(moment, fmt):
    return moment.astimezone().strftime(fmt)


def weekly_limits(rate_limits):
    """``[(key, label, percent, resets_at)]`` for each weekly limit in a ``get_usage`` reply's ``limits``.

    Entries without a numeric percent are skipped, never read as 0%. The session (5-hour) limit is left
    out: it clears within hours, and Hermes already answers a refused turn in its chat."""
    entries = rate_limits.get('limits') if isinstance(rate_limits, dict) else None
    found = []
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get('kind'), str):
            continue
        kind = entry['kind']
        if entry.get('group') != 'weekly' and not kind.startswith('weekly'):
            continue
        percent = entry.get('percent')
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
            continue
        scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
        model = scope.get('model') if isinstance(scope.get('model'), dict) else {}
        name = model.get('display_name') if isinstance(model.get('display_name'), str) else ''
        found.append((f'{kind}:{name}', f'{name} weekly limit' if name else 'weekly limit',
                      float(percent), _time(entry.get('resets_at'))))
    return found


def _period(resets_at):
    """The reset period a notice belongs to: the reset time to the nearest hour, so drift of a few
    seconds (or a fraction) between readings never re-arms a threshold."""
    if resets_at is None:
        return 'unknown'
    rounded = (resets_at + timedelta(minutes=30)).astimezone(timezone.utc)
    return rounded.strftime('%Y-%m-%dT%H')


def _notice(label, percent, threshold, resets_at):
    when = f" (resets {_local(resets_at, '%a %d %b %H:%M')})" if resets_at else ''
    if threshold >= 100:
        return f"eva has used up her Claude {label}{when}. She can't answer on Claude until it resets."
    return (f"Heads-up: eva has used {int(min(percent, 100))}% of her Claude {label}{when}. If it runs out she "
            "can't answer on Claude until then. You can wait, switch her model, or cut her usage.")


def decide(limits, state, now):
    """``(lines, state)`` after a successful reading: one line per limit whose highest crossed threshold
    has not been told in this reset period. Crossing 90% straight from below 75% sends only the 90% line."""
    notified = {}
    for key, until in (state.get('notified') or {}).items():
        expiry = _time(until)
        if expiry is None or expiry > now:
            notified[key] = until
    lines = []
    for key, label, percent, resets_at in limits:
        crossed = [threshold for threshold in THRESHOLDS if percent >= threshold]
        if not crossed:
            continue
        period = _period(resets_at)
        if f'{key}|{crossed[-1]}|{period}' not in notified:
            lines.append(_notice(label, percent, crossed[-1], resets_at))
        until = (resets_at or now + timedelta(days=7)).isoformat()
        for threshold in crossed:
            notified.setdefault(f'{key}|{threshold}|{period}', until)
    state = dict(state, notified=notified, failing_since=None, last_error=None,
                 worst=max(percent for _, _, percent, _ in limits))
    return lines, state


def failed(reason, state, now):
    """``(lines, state)`` after a failed reading: one line once readings have failed for two hours, then
    at most one a day, so a broken meter is never silent for long."""
    since = _time(state.get('failing_since')) or now
    state = dict(state, failing_since=since.isoformat(), last_error=reason)
    told = _time(state.get('meter_notice_at'))
    if now - since < METER_GRACE - TICK_SLACK or (told is not None and now - told < METER_REPEAT - TICK_SLACK):
        return [], state
    state['meter_notice_at'] = now.isoformat()
    return [f"eva's Claude usage check has not worked since {_local(since, '%a %H:%M')} ({reason}), so there "
            "will be no early warning before her weekly limit runs out."], state


def due(state, now):
    """Whether this tick takes a reading: hourly, every 20 minutes from 50%, every tick from 70%."""
    last = _time(state.get('last_attempt_at'))
    if last is None:
        return True
    worst = state.get('worst')
    worst = worst if isinstance(worst, (int, float)) and not isinstance(worst, bool) else 0
    every = timedelta(0) if worst >= EVERY_TICK_FROM else READ_EVERY_BUSY if worst >= BUSY_FROM else READ_EVERY
    return now - last >= every - TICK_SLACK


def plugin_fetch(home):
    """The plugin's ``fetch_usage`` as a no-argument callable, or a callable returning why it is missing."""
    for name in PLUGIN_DIRS:
        plugin = home / 'plugins' / name
        if (plugin / 'directsdk_setup.py').is_file():
            sys.path.insert(0, str(plugin))
            import directsdk_setup
            return lambda: directsdk_setup.fetch_usage(timeout=30)
    return lambda: (None, 'the Claude plugin is not installed in this profile')


def _load(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + '.tmp')
    scratch.write_text(json.dumps(state, sort_keys=True), encoding='utf-8')
    os.replace(scratch, path)


def main(home=None, now=None, fetch=None):
    """One cron tick. The state is saved before anything is printed, so a state file that cannot be
    written means silence, never the same notice on every tick."""
    try:
        # Cron only runs scripts from inside $HERMES_HOME/scripts/, so the profile home is two levels up.
        home = Path(home) if home else Path(__file__).resolve().parent.parent
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
        limits = weekly_limits(rate_limits)
        if limits:
            lines, state = decide(limits, state, now)
        else:
            lines, state = failed(str(reason or NO_WEEKLY)[:160], state, now)
        _save(path, state)
        if lines:
            print('\n'.join(lines))
    except Exception as exc:
        print(f'claude_allowance_watch: {type(exc).__name__}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
