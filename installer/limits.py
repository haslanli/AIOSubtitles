"""The limits each install asks, with the defaults and explanations of subsync/subsync/explain.py.

A personal install asks the daily total and, with the audio route on, the audio limit, and with Usenet on its daily
number of Usenet searches; a public instance asks every limit, in three groups (the audio route's only with it on: the
audio limit, the audio syncs a person may start a day, those started as soon as a file is opened, and the audio
syncs at a time; the Usenet number only when it lets people turn Usenet on). Left out because it would do
nothing: the language limit on a personal install (you choose your own languages there).

A limit is written to state/subsync.env only when it differs from subsync's default, so a later default still
applies to everyone who kept it.
"""
import decimal
import re
from collections import namedtuple

from installer import plan
from subsync import digits, explain
from subsync.aiosconfig import MAX_LANGUAGES
from subsync.settings import GB

Limit = namedtuple('Limit', 'setting label unit kind options')
Limit.__new__.__defaults__ = ((),)
Limit.default = property(lambda self: explain.SETTINGS[self.setting][0])

DAILY = Limit('SUBSYNC_DAILY_BUDGET_GB', 'Data a day in total', 'GB', 'decimal')
AUDIO = Limit('SUBSYNC_AUDIO_BUDGET_GB', 'Audio per file', 'GB', 'segmented', plan.AUDIO_BUDGETS)
# The Usenet searches a day (one setting, per person on a public instance): 0 turns Usenet off, as its explanation says
#, except where Usenet is your own install's only source (check's usenet_only)
USENET = Limit('SUBSYNC_USENET_LISTINGS_PER_DAY', 'Usenet searches a day', None, 'count')
USENET_HOSTED = Limit('SUBSYNC_USENET_LISTINGS_PER_DAY', 'Usenet searches per person a day', None, 'count')
# A public instance's audio route, asked only with it on (0 stops new audio syncs, or removes the opened answer)
AUDIO_PER_DAY = Limit('SUBSYNC_USER_AUDIO_PER_DAY', 'Audio syncs per person a day', None, 'count')
OPENED_PER_DAY = Limit('SUBSYNC_USER_OPENED_PER_DAY', 'Of those, as soon as a file is opened', None, 'count')
AUDIO_AT_ONCE = Limit('SUBSYNC_AUDIO_AT_ONCE', 'Audio syncs at a time', None, 'auto')

HOSTING = [
    ('Each person', [
        Limit('SUBSYNC_USER_FILES_PER_DAY', 'New files per person a day', 'files', 'number'),
        Limit('SUBSYNC_USER_DAILY_MB', 'Data per person a day', 'MB', 'number'),
        Limit('SUBSYNC_MAX_LANGUAGES', 'Languages per person', None, 'segmented',
              tuple(str(n) for n in range(1, MAX_LANGUAGES + 1))),
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

# the words for a value that doesn't fit (the two segmented ones are the terminal setup's; the page's are choose())
WHOLE = 'A whole number from 1, like {default}.'
COUNT = 'A whole number from 0, like {default}.'
# the Usenet searches a day where Usenet is your own install's only source: subsync refuses 0 there (settings.py)
USENET_ONLY = ("A whole number from 1, like {default}: Usenet is this install's only source, so 0 would leave subsync "
               "nothing to read.")
WHOLE_OR_AUTO = 'A whole number from 1, or auto.'
NOT_ABOVE_PLACES = 'A whole number from 1 to {places} (the files synced at a time), or auto.'
ABOVE_ZERO = 'A number above 0, like {default}.'
# a comma in a GB value: a decimal comma once multiplied it ("1,5" became 15 GB); the page never writes one there
NO_COMMA = 'A number above 0, with a dot for decimals and no commas, like 1.5.'
NOT_AN_AUDIO_LIMIT = f'Type {plan.AUDIO_BUDGET_WORDS} (GB per file).'
NOT_A_LANGUAGE_LIMIT = f'SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to {MAX_LANGUAGES}.'
# what subsync can hold: a longer answer, or a GB value whose bytes (subsync counts settings.GB, 10^9, to a GB) are no
# finite number, crashed it at start
LONGEST = 15
# a GB value: digits with one dot at most ("1.5", ".5", "2"); no exponent, sign or underscore
PLAIN_DECIMAL = re.compile(r'[0-9]+\.?[0-9]*|\.[0-9]+')


def personal(audio, usenet=False):
    return [DAILY] + ([AUDIO] if audio else []) + ([USENET] if usenet else [])


def hosting_groups(usenet=True, audio=False):
    """The hosting route's groups, without the Usenet number when the instance doesn't let people turn Usenet on; with
    the audio route on, the audio limit in "Each person", before the Usenet number, then the audio syncs a
    person may start a day and those started as soon as a file is opened, and the audio syncs at a time in "This
    server", after the files synced at a time."""
    groups = []
    for heading, group in HOSTING:
        kept = [lim for lim in group if usenet or lim is not USENET_HOSTED]
        if audio and heading == 'Each person':
            at = kept.index(USENET_HOSTED) if USENET_HOSTED in kept else len(kept)
            kept = kept[:at] + [AUDIO, AUDIO_PER_DAY, OPENED_PER_DAY] + kept[at:]
        if audio and heading == 'This server':
            at = [lim.setting for lim in kept].index('SUBSYNC_SYNCS_AT_ONCE') + 1
            kept = kept[:at] + [AUDIO_AT_ONCE] + kept[at:]
        groups.append((heading, kept))
    return groups


def hosting(usenet=True, audio=False):
    return [lim for _, group in hosting_groups(usenet, audio) for lim in group]


def names(asked):
    return tuple(lim.setting for lim in asked)


def _whole(text, least=1):
    value = text.strip()
    if len(value) > LONGEST:
        return None
    parts = value.split(',')
    if len(parts) > 1 and not (1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:])):
        return None
    number = digits.whole(''.join(parts))     # the digits 0 to 9 only: never "²" nor "１２"
    return str(number) if number is not None and number >= least else None


def _above_zero(text):
    """A GB value of at least one byte (subsync counts whole bytes), written in digits with a dot for decimals (a comma
    is refused before, with NO_COMMA), and as state/subsync.env gets it: in the same plain digits, no longer than
    LONGEST, so the next run takes it again ("1e15" was written as 16 digits setup then refused, "1e-10" as a
    value subsync reads as 0 bytes a day)."""
    value = text.strip()
    if len(value) > LONGEST or not PLAIN_DECIMAL.fullmatch(value) or int(float(value) * GB) < 1:
        return None
    written = format(decimal.Decimal(value).normalize(), 'f')       # "1.50" -> 1.5, "20.0" -> 20, ".5" -> 0.5
    return written if len(written) <= LONGEST else None


def check(lim, text, current=None, places=None, usenet_only=False):
    """(value as written in subsync.env, None) or (None, the words for the problem). `current`: a value set by hand
    before, kept when it is given again (the audio limit, as ./setup did). `places`: the files synced at a time, which
    the audio syncs at a time can't be more than (subsync refuses to start otherwise). `usenet_only`: Usenet is
    your own install's only source, so its searches a day start at 1 (subsync refuses to start on 0)."""
    text = '' if text is None else str(text)
    if lim.kind == 'number':
        value = _whole(text)
        return (value, None) if value else (None, WHOLE.format(default=shown(lim, lim.default)))
    if lim.kind == 'count':
        value = _whole(text, least=1 if usenet_only else 0)
        words = USENET_ONLY if usenet_only else COUNT
        return (value, None) if value else (None, words.format(default=shown(lim, lim.default)))
    if lim.kind == 'auto':
        if text.strip().lower() == 'auto':
            return 'auto', None
        value = _whole(text)
        if value and lim is AUDIO_AT_ONCE and places and int(value) > places:
            return None, NOT_ABOVE_PLACES.format(places=places)
        return (value, None) if value else (None, WHOLE_OR_AUTO)
    if lim.kind == 'decimal':
        if ',' in text:
            return None, NO_COMMA
        value = _above_zero(text)
        return (value, None) if value else (None, ABOVE_ZERO.format(default=shown(lim, lim.default)))
    if lim is AUDIO or lim.setting == AUDIO.setting:
        value = plan.audio_budget(text, allowed_too=current)
        return (value, None) if value else (None, NOT_AN_AUDIO_LIMIT)
    value = text.strip()
    return (value, None) if value in lim.options else (None, NOT_A_LANGUAGE_LIMIT)


def choose(lim):
    """A row of buttons' problem as the page says it: the values to choose from, with the unit ("Choose 0.5, 1, 2 or 4
    GB."); the terminal's words above say what to type."""
    *first, last = lim.options
    return f"Choose {', '.join(first)} or {last}{' ' + lim.unit if lim.unit else ''}."


def shown(lim, value):
    """A value as the page shows it: thousands marked in whole numbers (one too long to be a limit as it was typed)."""
    value = str(value)
    number = digits.whole(value) if lim.kind in ('number', 'count') and len(value) <= LONGEST else None
    return f'{number:,}' if number is not None else value


def current(lim, env):
    """The value to show: the one in state/subsync.env (set by hand or by an earlier answer), else subsync's default."""
    value = (env or {}).get(lim.setting)
    return value if value not in (None, '') else lim.default


def env_lines(asked, values):
    """The lines of state/subsync.env for these answers: only those that differ from subsync's default."""
    return {lim.setting: values[lim.setting] for lim in asked
            if values.get(lim.setting) not in (None, '') and values[lim.setting] != lim.default}
