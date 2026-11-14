"""The limits each install asks, with the defaults and explanations of subsync/subsync/explain.py.

A personal install asks the daily total and, with the audio route on, the audio limit, and with Usenet on its daily
number of Usenet searches; a public instance asks every limit, in three groups (the audio limit only with the audio
route on,, and the Usenet number only when it lets people turn Usenet on). Left out because it would do nothing:
the language limit on a personal install (you choose your own languages there).

A limit is written to state/subsync.env only when it differs from subsync's default, so a later default still
applies to everyone who kept it.
"""
import math
from collections import namedtuple

from subsync import explain

Limit = namedtuple('Limit', 'setting label unit kind options')
Limit.__new__.__defaults__ = ((),)
Limit.default = property(lambda self: explain.SETTINGS[self.setting][0])

DAILY = Limit('SUBSYNC_DAILY_BUDGET_GB', 'Data a day in total', 'GB', 'decimal')
AUDIO = Limit('SUBSYNC_AUDIO_BUDGET_GB', 'Audio per file', 'GB', 'segmented', ('0.5', '1', '2', '4'))
# The Usenet searches a day (one setting, per person on a public instance)
USENET = Limit('SUBSYNC_USENET_LISTINGS_PER_DAY', 'Usenet searches a day', None, 'number')
USENET_HOSTED = Limit('SUBSYNC_USENET_LISTINGS_PER_DAY', 'Usenet searches per person a day', None, 'number')

HOSTING = [
    ('Each person', [
        Limit('SUBSYNC_USER_FILES_PER_DAY', 'New files per person a day', 'files', 'number'),
        Limit('SUBSYNC_USER_DAILY_MB', 'Data per person a day', 'MB', 'number'),
        Limit('SUBSYNC_MAX_LANGUAGES', 'Languages per person', None, 'segmented', ('1', '2', '3', '4')),
        USENET_HOSTED,
    ]),
    ('The whole service', [
        DAILY,
        Limit('SUBSYNC_MAX_USERS', 'People signed up', None, 'number'),
        Limit('SUBSYNC_SIGNUPS_PER_HOUR', 'Sign-up attempts per address an hour', None, 'number'),
    ]),
    ('This server', [
        Limit('SUBSYNC_SYNCS_AT_ONCE', 'Files synced at a time', None, 'auto'),
        Limit('SUBSYNC_ACTIVE_WORKERS', 'Workers at once', None, 'number'),
        Limit('SUBSYNC_MAX_CONNECTIONS', 'Requests at once', None, 'number'),
    ]),
]

# the words for a value that doesn't fit (the two segmented ones are the terminal setup's)
WHOLE = 'A whole number from 1, like {default}.'
WHOLE_OR_AUTO = 'A whole number from 1, or auto.'
ABOVE_ZERO = 'A number above 0, like {default}.'
NOT_AN_AUDIO_LIMIT = 'Type 0.5, 1, 2 or 4 (GB per file).'
NOT_A_LANGUAGE_LIMIT = 'SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to 4.'


def personal(audio, usenet=False):
    return [DAILY] + ([AUDIO] if audio else []) + ([USENET] if usenet else [])


def hosting_groups(usenet=True, audio=False):
    """The hosting route's groups, without the Usenet number when the instance doesn't let people turn Usenet on; with
    the audio route on, the audio limit in "Each person", before the Usenet number."""
    groups = []
    for heading, group in HOSTING:
        kept = [lim for lim in group if usenet or lim is not USENET_HOSTED]
        if audio and heading == 'Each person':
            at = kept.index(USENET_HOSTED) if USENET_HOSTED in kept else len(kept)
            kept = kept[:at] + [AUDIO] + kept[at:]
        groups.append((heading, kept))
    return groups


def hosting(usenet=True, audio=False):
    return [lim for _, group in hosting_groups(usenet, audio) for lim in group]


def names(asked):
    return tuple(lim.setting for lim in asked)


def _whole(text):
    value = text.strip()
    parts = value.split(',')
    if len(parts) > 1 and not (1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:])):
        return None
    value = ''.join(parts)
    return str(int(value)) if value.isdigit() and int(value) >= 1 else None


def _above_zero(text):
    try:
        number = float(text.strip().replace(',', ''))
    except ValueError:
        return None
    if not (math.isfinite(number) and number > 0):
        return None
    return str(int(number)) if number == int(number) else str(number)


def check(lim, text, current=None):
    """(value as written in subsync.env, None) or (None, the words for the problem). `current`: a value set by hand
    before, kept when it is given again (the audio limit, as ./setup did)."""
    text = '' if text is None else str(text)
    if lim.kind == 'number':
        value = _whole(text)
        return (value, None) if value else (None, WHOLE.format(default=shown(lim, lim.default)))
    if lim.kind == 'auto':
        if text.strip().lower() == 'auto':
            return 'auto', None
        value = _whole(text)
        return (value, None) if value else (None, WHOLE_OR_AUTO)
    if lim.kind == 'decimal':
        value = _above_zero(text)
        return (value, None) if value else (None, ABOVE_ZERO.format(default=shown(lim, lim.default)))
    if lim is AUDIO or lim.setting == AUDIO.setting:
        from installer import plan
        value = plan.audio_budget(text, allowed_too=current)
        return (value, None) if value else (None, NOT_AN_AUDIO_LIMIT)
    value = text.strip()
    return (value, None) if value in lim.options else (None, NOT_A_LANGUAGE_LIMIT)


def shown(lim, value):
    """A value as the page shows it: thousands marked in whole numbers."""
    value = str(value)
    return f'{int(value):,}' if lim.kind == 'number' and value.isdigit() else value


def current(lim, env):
    """The value to show: the one in state/subsync.env (set by hand or by an earlier answer), else subsync's default."""
    value = (env or {}).get(lim.setting)
    return value if value not in (None, '') else lim.default


def env_lines(asked, values):
    """The lines of state/subsync.env for these answers: only those that differ from subsync's default."""
    return {lim.setting: values[lim.setting] for lim in asked
            if values.get(lim.setting) not in (None, '') and values[lim.setting] != lim.default}
