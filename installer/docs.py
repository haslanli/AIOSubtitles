#!/usr/bin/env python3
"""Writes the settings section of README.md and docs/answers.example from subsync/subsync/explain.py, where every
explanation lives: python3 -I installer/docs.py. The installer's tests fail while either file is out of date."""
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import installer  # noqa: E402,F401  (its __init__ puts subsync's folder on the import path)
from installer import hosting, limits  # noqa: E402
from subsync import explain  # noqa: E402
from subsync.version import PROJECT  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
START = '<!-- settings: written by python3 -I installer/docs.py from subsync/subsync/explain.py -->'
END = '<!-- end of settings -->'
WIDTH = 116

# The keys of an answers file, in four parts (a test checks they are every key setup reads from one), each part's
# mostly in the order ./setup asks them: your own install's limits (AUDIO_BUDGET and the two below), which setup asks
# after PUBLIC_ADDRESS, follow the audio and Usenet questions they belong to.
# Your own install: every question but the route's, the public instance's, removing's and restoring's, with the limits
# it asks under their setting names after the audio limit's question.
YOURSELF_LIMITS = limits.names(limits.personal(audio=False, usenet=True))
# A public instance: its address, the settings it asks under their own names (its trusted proxy, its invite code, its
# choices, its audio question), then its limits. The keys it shares with your own install (SUBDL_KEY, TMDB_KEY and the
# two limits above) are written once, in that part, and its heading names them: setup takes a key's last line, so a
# second line would silently win over the first. The audio question keeps its own words on setup's screen and
# in the example (explain.QUESTIONS); README explains it once, under the settings, as it does the others.
HOSTING_SETTINGS = ('SUBSYNC_TRUSTED_PROXY', 'SUBSYNC_PUBLIC_INVITE', 'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_TMDB_PERSONAL',
                    'SUBSYNC_USENET', 'SUBSYNC_AUDIO_ROUTE')
HOSTING = ('HOST_ADDRESS', *HOSTING_SETTINGS) + tuple(
    name for name in limits.names(limits.hosting(usenet=True, audio=True)) if name not in YOURSELF_LIMITS)
# Removing and restoring each ask their own yes (REMOVE, RESTORE), never CONFIRM's
REMOVING = ('REMOVE', 'REMOVE_OPENSUBTITLES', 'REMOVE_HERE_ONLY')
RESTORING = ('RESTORE',)
PARTS = ('Install it for yourself (ROUTE=yourself)',
         'Host a public instance (ROUTE=host). SUBDL_KEY, TMDB_KEY, SUBSYNC_USENET_LISTINGS_PER_DAY and '
         'SUBSYNC_DAILY_BUDGET_GB above are asked here too.',
         'Removing it (./setup --uninstall --answers FILE). AIOS_CONFIG_PASSWORD above is asked here too, and '
         'AIOS_USERNAME and AIOS_PASSWORD when your AIOStreams wants a sign-in to save configs; on a public instance '
         'only REMOVE.',
         'Restoring a saved copy (./setup --restore FILE --answers FILE). AIOS_CONFIG_PASSWORD above is asked here '
         'too, AIOS_USERNAME and AIOS_PASSWORD when your AIOStreams wants a sign-in to save configs, and after '
         './setup --uninstall AIOS_CONTAINER or AIOS_URL, as when installing.')
EXAMPLE_ON_GITHUB = f'{PROJECT}/blob/main/docs/answers.example'   # the image has no docs/
# Comment lines under a key's explanation in docs/answers.example, for what only an answers file needs; the question's
# own text stays as setup shows it
ANSWERS_NOTES = {'AUDIO': 'In an answers file: no, pick or open.',
                 'SUBSYNC_PUBLIC_INVITE': 'In an answers file: leave this line out to keep the current code (on a '
                                          'first setup, the one setup makes); an empty line lets anyone sign up.'}
# The keys whose empty line removes what setup keeps (an optional answer: a saved key, the invite code), written
# commented out: the example used as it is then keeps them, as a line left out does
COMMENTED_OUT = ('SUBDL_KEY', 'WYZIE_KEY', 'TMDB_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_PUBLIC_INVITE')


def yourself():
    """Your own install's keys, in the questions' order."""
    keys = []
    for key in explain.QUESTIONS:
        if key != 'ROUTE' and key not in HOSTING + REMOVING + RESTORING:
            keys += [key] + (list(YOURSELF_LIMITS) if key == 'AUDIO_BUDGET' else [])
    return keys


def comment(text):
    """A text as '# ' comment lines (for docs/answers.example and state/subsync.env)."""
    return ['# ' + line for line in textwrap.wrap(text, WIDTH - 2, break_on_hyphens=False, break_long_words=False)]


def markdown(text):
    """A text as README's Markdown shows it as written: each "<" escaped, so a placeholder like "<Language>" is not
    taken for an HTML tag, which GitHub drops. docs/answers.example is plain text and keeps it as written."""
    return text.replace('<', '\\<')


def _default(default):
    if default is None:
        return 'required'
    return f'default `{default}`' if default else 'default: empty'


def readme_section():
    out = [START, '',
           'Every setting affects only your own install: nothing is shared between AIOSubtitles installs, so no '
           'setting here helps or costs anyone else. The last group, public mode, is for running one service for '
           'many people; there how each copy was synced (its timing, never its text) is shared between everyone who '
           'plays the same file.', '',
           '### The questions ./setup asks', '',
           'Each question shows its explanation as it is asked. In an answers file (`./setup --answers FILE`) each one '
           f'is a line `KEY=value`; see [docs/answers.example]({EXAMPLE_ON_GITHUB}). Also asked, under their setting '
           f'names explained below: for your own install {limits.DAILY.setting} and, with Usenet on, '
           f'{limits.USENET.setting}; for a public instance {", ".join(HOSTING_SETTINGS)} and its limits.', '']
    out += [f'- **`{key}`**: {markdown(text)}' for key, (_, text) in explain.QUESTIONS.items()
            if key not in explain.SETTINGS]
    out += ['', "### subsync's settings", '',
            './setup writes the first group into `state/subsync.env` from your answers, each line with its explanation '
            'above it, and the limits it asks; for a public instance, also the public mode settings it asks. A limit '
            'it stops asking (its Usenet or audio route turned off) goes back to its default. The others keep the '
            'defaults shown. To change one, add `NAME=value` to `state/subsync.env` and run ./setup again: Change your '
            'answers, then go through to the end, and subsync restarts with it (where Docker Compose is installed on '
            'this machine, `docker compose -f state/compose.yaml up -d` does the same). Running ./setup again keeps '
            "every line it doesn't write."]
    for title, group in explain.GROUPS:
        out += ['', f'#### {title}', '']
        out += [f'- **`{name}`** ({_default(default)}): {markdown(text)}' for name, (default, text) in group.items()]
    out += ['', '### Fixed rules', '',
            'These are not settings: AIOSubtitles always works this way.', '']
    out += [f'- **{markdown(title)}.** {markdown(text)}' for title, text in explain.RULES]
    out += ['', END]
    return '\n'.join(out)


def example(key):
    """A key's example line, under its explanation (and its note for answers files, ANSWERS_NOTES): a question's own
    example, else (a setting's name) the setting's default, or the answer setup takes when the line is left out where
    that differs (the trusted proxy); commented out for a key whose empty line removes what setup keeps (COMMENTED_OUT:
    the invite code's left-out answer differs too)."""
    if key in explain.QUESTIONS:
        value, text = explain.QUESTIONS[key]
    else:
        default, text = explain.SETTINGS[key]
        value = hosting.TRUSTED_PROXY if key == 'SUBSYNC_TRUSTED_PROXY' else default or ''
    note = comment(ANSWERS_NOTES[key]) if key in ANSWERS_NOTES else []
    line = f'{key}={value}'
    return [''] + comment(text) + note + ['# ' + line if key in COMMENTED_OUT else line]


def answers_example():
    """docs/answers.example: the header, the route's question, then the four parts (PARTS), each under its heading."""
    out = comment('Answers for ./setup --answers state/FILE (add --yes to ask nothing). Leave a line out to be asked, '
                  "or to keep the current value on a re-run; an optional key's empty line (TMDB_KEY=) removes the "
                  'saved key. A " #" starts a comment, except inside quotes. Keep this file private if it holds a '
                  'password.')
    out += example('ROUTE')
    for heading, keys in zip(PARTS, (yourself(), HOSTING, REMOVING, RESTORING)):
        out += ['', '', '# ' + '=' * (WIDTH - 2)] + comment(heading)
        for key in keys:
            out += example(key)
    return '\n'.join(out) + '\n'


def write_whole(path, text):
    """`path` in UTF-8 (whatever the locale's encoding), written whole or not at all: a temporary file beside it,
    renamed over it once written, so a failure leaves the last one as it was."""
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write():
    """README.md's settings section and docs/answers.example, both built before either file is written."""
    readme = REPO / 'README.md'
    text = readme.read_text(encoding='utf-8')
    if START not in text:
        raise SystemExit(f'README.md has no line "{START}": put it where the settings section goes.')
    before, rest = text.split(START, 1)
    if END not in rest:
        raise SystemExit(f'README.md has no line "{END}" after "{START}": put it where the settings section ends.')
    files = {readme: before + readme_section() + rest.split(END, 1)[1],
             REPO / 'docs' / 'answers.example': answers_example()}
    for path, content in files.items():
        write_whole(path, content)


if __name__ == '__main__':
    write()
