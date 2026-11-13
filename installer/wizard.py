#!/usr/bin/env python3
"""AIOSubtitles setup: adds subsync to the AIOStreams you already run. Start it with ./setup.

The work of each step is in installer/engine.py; this is the command line.

  ./setup                  the setup page: install, or change your answers, update, remove (safe to run again)
  ./setup --status         is it running, and what has it done
  ./setup --update         update to the newest version, if there is one
  ./setup --uninstall      take subsync out of your config and remove it
  ./setup --restore FILE   put a saved copy of your config back
  ./setup --answers FILE [--yes]   answers from KEY=value lines (see docs/answers.example)
"""
import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from installer import dockerenv, engine, fonts, plan, web  # noqa: E402
from installer.aios import Aios, AiosError  # noqa: E402
from installer.ui import Asker, Stop, bad, note, ok, paint, say, stop, warn, wrap  # noqa: E402
from installer.engine import backup, load_settings, read_kv, short, subsync_status  # noqa: E402,F401

CONTAINER = engine.CONTAINER


# ------------------------------------------------------------------------------------------- other commands

def cmd_status():
    rows, details = engine.Engine({}).status_parts()
    say(paint('1', '  AIOSubtitles · subsync status'))
    for kind, text, more in rows:
        {'ok': ok, 'warn': warn, 'bad': bad}[kind](text)
        if more:
            note(more)
    d = dict(details)
    if 'AIOStreams' in d:
        say(f"  AIOStreams: {d['AIOStreams']}  ·  languages: {d['languages']}  ·  audio: {d['audio']}")
        say(f"  config: {d['config']}  ·  subsync's config: " + d["subsync's config"])
    say(f"  logs: {d['logs']}")
    return 0


def cmd_restore(args, path):
    data = json.loads(Path(path).read_text())
    s = load_settings()
    aios = Aios(s.get('aios_url') or 'http://127.0.0.1:3000')
    ask = Asker(read_kv(Path(args.answers)) if args.answers else {}, args.yes)
    uuid = data['uuid']
    password = ask.text('AIOS_CONFIG_PASSWORD', f'Password of config {short(uuid)}', secret=True)
    if not ask.yesno('CONFIRM', f'Put the copy from {Path(path).name} back into config {short(uuid)}?', False):
        stop('Nothing was changed.')
    current, _ = aios.get_config(uuid, password)
    backup(current, uuid, 'before-restore')
    aios.save_config(uuid, password, data['config'])
    ok('Restored.')
    return 0


def cmd_uninstall(args):
    answers = plan.parse_env(Path(args.answers).read_text()) if args.answers else {}
    e = engine.Engine(answers)
    e.remove_host() if e.prev.get('route') == 'host' else e.remove()
    return 0


# ------------------------------------------------------------------------------------------- main

def open_page(args):
    """The setup page. After an update's restart the same address goes on with the update's second half."""
    resume = os.environ.pop('AIOSUBTITLES_SETUP_RESUME', None)
    port = int(os.environ.pop('AIOSUBTITLES_SETUP_PORT', '0') or 0)
    key = os.environ.pop('AIOSUBTITLES_SETUP_KEY', None)
    app = web.App(port=port, fonts_dir=fonts.ensure(engine.STATE), resume=resume, no_browser=args.no_browser)
    if key:
        app.key = key
    if resume:
        app.start_resumed()
    return web.serve(app, say=print, opener=None if args.no_browser else webbrowser.open)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='./setup', description='Add subsync (synced subtitles) to your AIOStreams.')
    parser.add_argument('--answers', help='answers from a KEY=value file')
    parser.add_argument('--yes', action='store_true', help='ask nothing: answers file, current values and defaults')
    parser.add_argument('--status', action='store_true', help='is it running, and what has it done')
    parser.add_argument('--uninstall', action='store_true', help='take subsync out of your config and remove it')
    parser.add_argument('--restore', metavar='FILE', help='put a saved copy of your config back')
    parser.add_argument('--update', action='store_true', help='update to the newest version, if there is one')
    parser.add_argument('--update-finish', metavar='COMMIT', help=argparse.SUPPRESS)   # an update's second half
    parser.add_argument('--no-browser', action='store_true', help="don't open the browser; just print the address")
    args = parser.parse_args(argv)
    try:
        if args.status:
            return cmd_status()
        if args.restore:
            return cmd_restore(args, args.restore)
        if args.uninstall:
            return cmd_uninstall(args)
        if args.update:
            engine.Engine({}).update()
            return 0
        if args.update_finish:
            return 0 if engine.Engine({}).finish_update(args.update_finish) else 3
        if not (args.answers or args.yes):
            return open_page(args)
        say()
        say(paint('1', '  AIOSubtitles · subsync setup'))
        answers = plan.parse_env(Path(args.answers).read_text()) if args.answers else {}   # KEY= is an answer too
        e = engine.Engine(answers)
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
    except engine.Problem as e:     # an answer the answers file gave wrong, or didn't give
        say()
        bad(e.message or f'{e.field} needs an answer.')
        say(f'      Add {e.field}=... to the answers file.')
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
