"""Terminal output and questions for ./setup: plain text, a little colour when the output is a terminal."""
import getpass
import os
import re
import sys
import textwrap

from subsync import explain

COLOR = sys.stdout.isatty() and os.environ.get('NO_COLOR') is None
WIDTH = 72


def paint(code, text):
    return f'\033[{code}m{text}\033[0m' if COLOR else text


def say(text=''):
    print(text, flush=True)


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

    def __init__(self, message, fix=None):
        super().__init__(message)
        self.message, self.fix = message, fix


def stop(message, fix=None):
    raise Stop(message, fix)


STEPS = 7


def step(number, title):
    say()
    say(paint('1', f'Step {number} of {STEPS} · {title}'))
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
    return re.sub(r'\033\[[0-9;]*m', '', text)


def masked(value):
    return f'set ({len(value)} characters)' if value else 'not set'


class Asker:
    """Questions with defaults, each shown with its one explanation (subsync/subsync/explain.py, by the question's
    answers-file key; a key without one is refused). Enter keeps the current answer (or takes the default); '?' shows
    the question and its explanation again. Answers can come from a KEY=value file (--answers) and, with --yes, nothing
    is asked."""

    def __init__(self, answers=None, yes=False):
        self.answers, self.yes = dict(answers or {}), yes

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
        """One question. `check(value)` returns (good, message, value): a message is shown either way."""
        explanation = explain.QUESTIONS[key][1]
        preset = self.answers.pop(key, None)
        while True:
            say()
            say(f'  {paint("1", question)}')
            for line in textwrap.wrap(explanation, WIDTH):
                say(paint('2', f'  {line}'))
            hint = []
            if current:
                hint.append(f'Enter keeps: {masked(current) if secret else (shown or current)}')
            elif default:
                hint.append(f'Enter = {default}')
            elif optional:
                hint.append(skip_hint)
            if hint:
                say(paint('2', '  ' + '; '.join(hint)))
            if preset is not None:
                value = preset
                say(f'  > {masked(value) if secret and value else (value or "(empty)")}   {paint("2", "(from the answers file)")}')
                preset = None
            elif self.yes:
                value = ''
                say(f'  > {paint("2", "(kept)" if current else "(default)" if default else "(skipped)" if optional else "(no answer)")}')
            else:
                value = self._input('  > ', secret).strip()
            if value == '?':
                continue
            if not value:
                value = current or default or ''
            if not value:
                if optional:
                    return ''
                if self.yes:
                    stop(f'"{question}" needs an answer.', f'Add {key}=... to the answers file.')
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
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return True, '', values[int(answer) - 1]
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
