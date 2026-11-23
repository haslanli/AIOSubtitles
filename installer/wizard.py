#!/usr/bin/env python3
"""AIOSubtitles setup: adds subsync to the AIOStreams you already run, or hosts a public instance. Start it with
./setup, which runs it inside the AIOSubtitles image (`docker run ... setup`); the work of each step is in
installer/engine.py, this is the command line.

  ./setup                  the setup page: install, or change your answers, update, remove (safe to run again)
  ./setup --status         is it running, and what has it done
  ./setup --update         update to this setup's version, if it is newer
  ./setup --rollback       back to the version before the last update
  ./setup --uninstall      take subsync out of your config and remove it; a public instance: remove it with
                           everyone's records
  ./setup --restore FILE   put a saved copy of your config back
  ./setup --answers FILE [--yes]   answers from KEY=value lines (see docs/answers.example)
  ./setup --version        this setup's version
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from installer import dockerenv, engine, plan, screens, update, web, where  # noqa: E402
from installer.aios import AiosError  # noqa: E402
from installer.ui import Stop, answers_advice, bad, note, ok, paint, say, stop, warn  # noqa: E402
from installer.engine import backup, short  # noqa: E402


def answers_from(args):
    """The answers file's lines (KEY= is an answer too), or None when no answers file was given. When it can't be read,
    a Stop that says to put it in state/: setup sees that folder on both routes (the source route mounts no other)."""
    if not args.answers:
        return None
    try:
        return plan.parse_env(Path(args.answers).read_text())
    except (OSError, ValueError):       # missing, a folder, unreadable, or not text
        raise Stop(CANT_OPEN.format(file=args.answers), GIVE_ANSWERS) from None


# ------------------------------------------------------------------------------------------- other commands

def cmd_status():
    e = engine.Engine({})
    rows, details = e.status_parts()
    say(paint('1', f"  {screens.STATUS['host' if e.prev.get('route') == 'host' else 'yourself']}"))
    for kind, text, more in rows:
        {'ok': ok, 'warn': warn, 'bad': bad}[kind](text)
        if more:
            note(more)
    d = dict(details)
    if 'AIOStreams' in d:
        say(f"  AIOStreams: {d['AIOStreams']}  ·  languages: {d['languages']}  ·  audio: {d['audio']}")
        # the configs, the Usenet one when there is one, as the page after setup lists them
        say('  ' + '  ·  '.join(f'{label}: {d[label]}' for label in ('config', "subsync's config",
                                                                     "subsync's Usenet config") if label in d))
    say(f"  logs: {d['logs']}")
    return 0


# --restore FILE when FILE can't be read, or isn't a saved copy of a config (engine.backup's {"uuid", "config"});
# --answers FILE when FILE can't be read
CANT_OPEN = "Setup can't open {file}."
NOT_A_COPY = "{file} isn't a saved copy of a config."
GIVE_A_COPY = ('Give a saved copy from state/backups/, like '
               './setup --restore state/backups/viewer-1a2b3c4d-20261117-120000.json.')
GIVE_ANSWERS = 'Give an answers file in state/, like ./setup --answers state/answers.txt --yes.'
# --restore-databases given without --rollback, and the way out
RESTORE_DATABASES_ALONE = ('--restore-databases works only with --rollback.',
                           'Run ./setup --rollback --restore-databases.')


def saved_copy(path):
    """(config ID, config) of a saved copy of a config; a Stop that says what to give when `path` can't be read or
    isn't one (a databases-… folder, say)."""
    try:
        data = json.loads(Path(path).read_text())
    except IsADirectoryError:
        data = None
    except OSError:
        raise Stop(CANT_OPEN.format(file=path), GIVE_A_COPY) from None
    except ValueError:                  # not JSON, or not text
        data = None
    if not (isinstance(data, dict) and isinstance(data.get('uuid'), str) and data['uuid']
            and isinstance(data.get('config'), dict)):
        raise Stop(NOT_A_COPY.format(file=path), GIVE_A_COPY)
    return data['uuid'], data['config']


def cmd_restore(args, path):
    uuid, config = saved_copy(path)         # before anything is asked or changed
    e = engine.Engine(answers_from(args), yes=args.yes)
    if e.prev.get('aios_url'):
        e.aios = engine.saved_aios(e.prev)
    else:           # no settings (./setup --uninstall deletes them): AIOStreams found as installing finds it
        e.reach_aios()
    read = {}

    def check_password(value):      # tried at the question, as removing does: a wrong one is asked again
        try:
            read['config'], _ = e.aios.get_config(uuid, value)
        except AiosError as error:
            return False, error.message, value
        return True, '', value
    password = e.ask.text('AIOS_CONFIG_PASSWORD', screens.CONFIG_PASSWORD.format(config=short(uuid)), secret=True,
                          check=check_password)
    # its own yes, never CONFIRM's: an answers file made for installing never restores anything
    if not e.ask.yesno('RESTORE', f'Put the copy from {Path(path).name} back into config {short(uuid)}?', False):
        stop('Nothing was changed.')
    backup(read['config'], uuid, 'before-restore')
    e.with_sign_in(lambda: e.aios.save_config(uuid, password, config))    # asks a sign-in AIOStreams wants
    ok('Restored.')
    return 0


def cmd_uninstall(args):
    e = engine.Engine(answers_from(args), yes=args.yes)
    e.remove_host() if e.prev.get('route') == 'host' else e.remove()
    return 0


# ------------------------------------------------------------------------------------------- main

def open_page():
    """The setup page. Inside a container nothing opens a browser: the address is printed. On Docker's bridge network
    the page listens inside the container, and the machine's port given in the docker run command reaches it: a port
    on this machine only (127.0.0.1 or ::1), never one other computers reach."""
    place = engine.PLACE
    if place and place.network == 'bridge':
        if not place.page_port or place.page_exposed:
            say()
            bad(where.PAGE_PORT_NOT_LOCAL if place.page_port else where.NO_PAGE_PORT)
            return 1
        app = web.App(port=where.PAGE_PORT, bind='0.0.0.0', public_port=place.page_port, public_host=place.page_host)
    else:
        app = web.App(port=0)
    return web.serve(app, say=print)


def in_image():
    """Inside the image: the socket is there, and where setup runs is known (engine.PLACE). A message and 1 if not."""
    if not os.path.exists(dockerenv.SOCKET):
        say()
        bad(where.NO_SOCKET)
        return 1
    try:
        engine.PLACE = where.discover(engine.setup_version())
    except RuntimeError as e:
        say()
        bad(str(e))
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='./setup', description='Set up AIOSubtitles: add subsync (synced subtitles) '
                                                                 'to your AIOStreams, or host a public instance.')
    parser.add_argument('--answers', help='answers from a KEY=value file')
    parser.add_argument('--yes', action='store_true', help='ask nothing: answers file, current values and defaults')
    parser.add_argument('--status', action='store_true', help='is it running, and what has it done')
    parser.add_argument('--uninstall', action='store_true', help="take subsync out of your config and remove it; a "
                                                                 "public instance: remove it with everyone's records")
    parser.add_argument('--restore', metavar='FILE', help='put a saved copy of your config back')
    parser.add_argument('--update', action='store_true', help="update to this setup's version, if it is newer")
    parser.add_argument('--rollback', action='store_true', help='back to the version before the last update')
    parser.add_argument('--restore-databases', action='store_true',
                        help='with --rollback: also put back the databases from before that update')
    parser.add_argument('--version', action='store_true', help="this setup's version")
    parser.add_argument('--no-browser', action='store_true', help=argparse.SUPPRESS)   # kept: no browser opens now
    # setup 1.3.0's own ./setup --update, once it has these files, runs them with this on the machine's Python
    parser.add_argument('--update-finish', metavar='COMMIT', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.version:
        print(engine.setup_version())
        return 0
    if args.update_finish:      # the folder's new ./setup finishes it, in the image: nothing to do here
        say()
        warn(update.UPDATE_FINISH)
        say()
        return 1
    if os.environ.get('AIOSUBTITLES_IN_IMAGE') and in_image():
        return 1
    try:
        if args.restore_databases and not args.rollback:
            stop(*RESTORE_DATABASES_ALONE)
        if args.status:
            return cmd_status()
        if args.restore:
            return cmd_restore(args, args.restore)
        if args.uninstall:
            return cmd_uninstall(args)
        if args.update:
            return 0 if engine.Engine({}).update() else 3
        if args.rollback:
            return 0 if engine.Engine({}).rollback(restore=args.restore_databases) else 3
        if not (args.answers or args.yes):
            return open_page()
        say()
        say(paint('1', '  AIOSubtitles · subsync setup'))
        e = engine.Engine(answers_from(args), yes=args.yes)
        if e.prev:
            say(paint('2', '  Already set up here: your answers are kept unless you change them.'))
        return 0 if e.run_answers() else 3
    except Stop as e:
        say()
        bad(e.message)
        for line in (e.fix or '').strip().splitlines():
            say(f'      {line}')
        say()
        return 1
    except engine.Problem as e:     # an answer refused (a port taken, say), or not given
        say()
        bad(e.message or f'{e.field} needs an answer.')
        say(f'      {answers_advice(e.field, answers_from(args))}')
        say()
        return 1
    except AiosError as e:
        say()
        bad(e.message)
        if e.fix:
            say(f'      {e.fix}')
        say()
        return 1


if __name__ == '__main__':
    sys.exit(main())
