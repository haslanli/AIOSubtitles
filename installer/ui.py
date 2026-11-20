"""Terminal output and questions for ./setup: plain text, a little colour when the output is a terminal."""
import getpass
import os
import re
import sys
import textwrap

from subsync import digits, explain

COLOR = sys.stdout.isatty() and os.environ.get('NO_COLOR') is None
WIDTH = 72


def paint(code, text):
    return f'\033[{code}m{text}\033[0m' if COLOR else text


# What a terminal acts on instead of showing: the codes that set how text looks (ESC [ … m), every C0 control but the
# line break, DEL and the C1 controls. Text from AIOStreams (a version, an error message, a name in a config) is printed
# in setup's lines, so these are taken out of every line: nothing printed can move the cursor, clear lines or set the
# window title or the clipboard. While colour is on, a code made only of what setup paints with (PAINTED) stays, and
# any other goes whole (conceal, reverse, blink, other colours:); a line that leaves one in force ends with a
# reset, so none outlives its line. With colour off, setup paints nothing, so every one goes.
UNSHOWN = re.compile(r'\033\[([0-9;]*)m|[\x00-\x09\x0b-\x1f\x7f-\x9f]')
PAINTED = frozenset(('0', '1', '2', '31', '32', '33'))     # paint's and box's codes: reset, bold, dim, red, green, yellow
RESET = '\033[0m'


def _as_printed(text):
    """`text` as say() prints it."""
    kept = []

    def keep(m):
        if COLOR and m.group(1) is not None and set(m.group(1).split(';')) <= PAINTED:
            kept.append(m.group(1))
            return m.group(0)
        return ''
    line = UNSHOWN.sub(keep, str(text))
    return line + RESET if kept and kept[-1].split(';')[-1] != '0' else line


def say(text=''):
    print(_as_printed(text), flush=True)


def wrap(text, indent='  '):
    for paragraph in str(text).split('\n'):
        say(textwrap.fill(paragraph, WIDTH, initial_indent=indent, subsequent_indent=indent) if paragraph else '')


def ok(text):
    say(f"  {paint('32', '✓')}  {text}")


def warn(text):
    say(f"  {paint('33', '!')}  {text}")


def bad(text):
    say(f"  {paint('31', '✗')}  {text}")


def note(text):
    say(paint('2', f'     {text}'))


class Stop(Exception):
    """Ends the run with a message and how to fix it; nothing after this point is changed."""

    def __init__(self, message, fix=None, page_fix=None):
        super().__init__(message)
        self.message, self.fix = message, fix
        self.page_fix = page_fix or fix      # the way out as the setup page says it, when it differs


def stop(message, fix=None):
    raise Stop(message, fix)


def step(number, title, total):
    say()
    say(paint('1', f'Step {number} of {total} · {title}'))
    say(paint('2', '─' * 60))


def box(title, paragraphs, colour='1;32', width=76):
    """A boxed block. Each paragraph is wrapped to the box; one starting with '• ' keeps its hanging indent."""
    inner = width - 4
    say()
    say('  ┌' + '─' * (width - 2) + '┐')
    say('  │ ' + paint(colour, title) + ' ' * (inner - len(title)) + ' │')
    say('  ├' + '─' * (width - 2) + '┤')
    for text in paragraphs:
        hang = '  ' if text.startswith('• ') else ''
        for part in textwrap.wrap(text, inner, subsequent_indent=hang) or ['']:
            say('  │ ' + part + ' ' * max(0, inner - len(strip_ansi(part))) + ' │')
    say('  └' + '─' * (width - 2) + '┘')


def strip_ansi(text):
    """The text as it takes room on the terminal: without colour codes and what say() takes out."""
    return UNSHOWN.sub('', text)


def masked(value):
    return f'set ({len(value)} characters)' if value else 'not set'


REMOVE = 'remove'      # typed at an optional question: no answer is kept (its current one is removed)


def answers_advice(key, given):
    """The way out after an answer was refused or not given: `given`, the keys the answers file has (None: no file)."""
    if given is None:
        return f'Give {key}=... in an answers file: ./setup --answers FILE --yes.'
    if key in given:
        return f'Change {key} in the answers file.'
    return f'Add {key}=... to the answers file.'


class Asker:
    """Questions with defaults, each shown with its one explanation (subsync/subsync/explain.py, by the question's
    answers-file key; a key without one is refused). Enter keeps the current answer (or takes the default); '?' shows
    the question and its explanation again. Answers can come from a KEY=value file (--answers) and, with --yes, nothing
    is asked. An optional question's current answer is removed by typing remove, or by its empty line in the file:
    '' is returned (with no current answer too: the word is never kept as the answer)."""

    def __init__(self, answers=None, yes=False, settings=None):
        """`answers`: the answers file's lines; None when no file was given. `settings`: subsync's on this install, for
        the explanations that state one (explain.setup_help)."""
        self.answers, self.yes, self.settings = dict(answers or {}), yes, settings
        self.file_keys = None if answers is None else set(answers)

    def _input(self, prompt, secret):
        try:
            if secret and sys.stdin.isatty():
                return getpass.getpass(prompt)
            return input(prompt)
        except EOFError:
            stop('No more input.', 'Run ./setup in a terminal, or give every answer with --answers FILE --yes.')
        except KeyboardInterrupt:
            say()
            stop('Stopped by you.', 'Nothing after this point was changed. Run ./setup again any time.')

    def text(self, key, question, *, current=None, default=None, secret=False, optional=False, check=None,
             shown=None, skip_hint='Enter skips'):
        """One question. `check(value)` returns (good, message, value): a message is shown either way. `shown(value)`:
        how the current answer and one from the answers file are shown (a secret's: masked)."""
        def show(value):
            return masked(value) if secret else shown(value) if shown else value
        explanation = explain.setup_help(key, self.settings)    # a limit: its setting's text
        preset = self.answers.pop(key, None)
        while True:
            say()
            say(f'  {paint("1", question)}')
            for line in textwrap.wrap(explanation, WIDTH):
                say(paint('2', f'  {line}'))
            hint = []
            if current:
                hint.append(f'Enter keeps: {show(current)}')
                if optional:
                    hint.append(f'type {REMOVE} to delete it')
            elif default:
                hint.append(f'Enter = {default}')
            elif optional:
                hint.append(skip_hint)
            if hint:
                say(paint('2', '  ' + '; '.join(hint)))
            if preset is not None:
                value = preset
                say(f'  > {show(value) if value else "(empty)"}   {paint("2", "(from the answers file)")}')
                preset = None
                removing = not value
            elif self.yes:
                value = ''
                say(f'  > {paint("2", "(kept)" if current else "(default)" if default else "(skipped)" if optional else "(no answer)")}')
                removing = False
            else:
                value = self._input('  > ', secret).strip()
                removing = value.lower() == REMOVE
            if value == '?':
                continue
            if removing and optional:
                return ''
            if not value:
                value = current or default or ''
            if not value:
                if optional:
                    return ''
                if self.yes:
                    stop(f'"{question}" needs an answer.', answers_advice(key, self.file_keys))
                warn('This one is needed.')
                continue
            if check:
                good, message, value = check(value)
                if not good:
                    warn(message)
                    if self.yes:
                        stop(f'{key}: {message}')
                    continue
                if message:
                    ok(message)
            return value

    def choose(self, key, question, options, current=None):
        """options: [(value, label)]. Returns the chosen value."""
        values = [v for v, _ in options]
        default = current if current in values else values[0]
        say()
        say(f'  {paint("1", question)}')
        for i, (_, label) in enumerate(options, 1):
            say(f'    {i}) {label}')

        def check(answer):
            if answer in values:
                return True, '', answer
            number = digits.whole(answer)      # never "²" (int refuses it) nor "１"
            if number is not None and 1 <= number <= len(options):
                return True, '', values[number - 1]
            return False, f'Type a number from 1 to {len(options)}.', answer
        return self.text(key, 'Your choice (number)', default=str(values.index(default) + 1), check=check)

    def yesno(self, key, question, default):
        def check(answer):
            if answer.lower() in ('y', 'yes', 'on', 'true', '1'):
                return True, '', 'yes'
            if answer.lower() in ('n', 'no', 'off', 'false', '0'):
                return True, '', 'no'
            return False, 'Type y or n.', answer
        return self.text(key, f'{question} [y/n]', default='yes' if default else 'no', check=check) == 'yes'

    def pause(self, question):
        if self.yes:
            return
        say()
        self._input(f'  {paint("1", question)} ', False)
