#!/usr/bin/env python3
"""Writes the settings section of README.md and docs/answers.example from subsync/subsync/explain.py, where every
explanation lives: python3 -I installer/docs.py. The installer's tests fail while either file is out of date."""
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import installer  # noqa: E402,F401  (its __init__ puts subsync's folder on the import path)
from subsync import explain  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
START = '<!-- settings: written by python3 -I installer/docs.py from subsync/subsync/explain.py -->'
END = '<!-- end of settings -->'
WIDTH = 116


def comment(text):
    """A text as '# ' comment lines (for docs/answers.example and state/subsync.env)."""
    return ['# ' + line for line in textwrap.wrap(text, WIDTH - 2, break_on_hyphens=False, break_long_words=False)]


def _default(default):
    if default is None:
        return 'required'
    return f'default `{default}`' if default else 'default: empty'


def readme_section():
    out = [START, '',
           'Every setting affects only your own install: nothing is shared between AIOSubtitles installs, so no '
           'setting here helps or costs anyone else.', '',
           '### The questions ./setup asks', '',
           'Each question shows its explanation as it is asked. In an answers file (`./setup --answers FILE`) each one '
           'is a line `KEY=value`; see `docs/answers.example`.', '']
    out += [f'- **`{key}`**: {text}' for key, (_, text) in explain.QUESTIONS.items()]
    out += ['', "### subsync's settings", '',
            './setup writes the first group into `state/subsync.env`, each line with its explanation above it; the '
            'others keep the defaults shown. To change one, add `NAME=value` to `state/subsync.env` and run '
            '`docker compose -f state/compose.yaml up -d`. Running ./setup again rewrites that file, so add it again '
            'afterwards.']
    for title, group in explain.GROUPS:
        out += ['', f'#### {title}', '']
        out += [f'- **`{name}`** ({_default(default)}): {text}' for name, (default, text) in group.items()]
    out += ['', '### Fixed rules', '',
            'These are not settings: AIOSubtitles always works this way.', '']
    out += [f'- **{title}.** {text}' for title, text in explain.RULES]
    out += ['', END]
    return '\n'.join(out)


def answers_example():
    out = ['# Answers for ./setup --answers FILE (add --yes to ask nothing). Leave a line out to be asked, or to keep '
           'the current', '# value on a re-run. A " #" starts a comment. Keep this file private if it holds a password.']
    for key, (example, text) in explain.QUESTIONS.items():
        out += [''] + comment(text) + [f'{key}={example}']
    return '\n'.join(out) + '\n'


def write():
    readme = REPO / 'README.md'
    text = readme.read_text()
    if START in text:
        before, rest = text.split(START, 1)
        text = before + readme_section() + rest.split(END, 1)[1]
    else:
        raise SystemExit(f'README.md has no line "{START}": put it where the settings section goes.')
    readme.write_text(text)
    (REPO / 'docs' / 'answers.example').write_text(answers_example())


if __name__ == '__main__':
    write()
