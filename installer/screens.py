"""The setup page's screens.

Each function returns a whole page, built from subsync's parts (subsync/subsync/ui.py and its one stylesheet) like the
public site, so the installer looks and works like it. Every word is an approved one: subsync's explain.py,
the site's pages.UI_WORDS, and INSTALLER_WORDS below (each with the decision that approved it); values from the
person, from AIOStreams and the engine's rows are escaped by the parts. Every form posts to the page it is on and works
without the script; the installing page loads itself again every 2 seconds while the work runs.
"""
from installer import limits as limit_list
from installer import update
from subsync import explain, ui
from subsync.aiosconfig import LANGUAGES
from subsync.ui import e

TITLE = 'Set up AIOSubtitles'
PERSONAL = ('This machine', 'Your AIOStreams', 'The config your apps use', 'Your choices', 'Limits',
            'Your AIOStreams configs', 'Installing subsync', 'Checking that it works')
HOSTING = ('This machine', 'Its web address', 'Sign-ups', 'Your choices', 'Limits', 'Installing AIOSubtitles',
           'Checking that it works')
PERSONAL_IDS = ('machine', 'aios', 'config', 'choices', 'limits', 'review', 'install', 'check')
HOSTING_IDS = ('machine', 'address', 'signups', 'hostchoices', 'hostlimits', 'hostinstall', 'hostcheck')
RAIL = 'Setup steps'

INTRO = ('AIOSubtitles makes copies of your subtitles timed to the exact file you play, and lists them next to the '
         'originals. It runs on this machine: next to the AIOStreams you already run, or as a public instance with its '
         'own sign-up page.',
         'Each step says what it does and what it changes. Nothing in your AIOStreams changes until you confirm, and a '
         'copy of your config is saved first.')
BEFORE = ('Docker and Docker Compose on this machine.', 'About 4 GB of free disk space.',
          'For yourself: the ID and password of the AIOStreams config your apps use.',
          'For a public instance: a web address with https that reaches this machine.')
WAYS = (('yourself', 'Install it for yourself', 'This adds it to the AIOStreams you already run.'),
        ('host', 'Host a public instance', 'Runs here with its own sign-up page, so other people can use it with '
                                           'their own AIOStreams.'))
QUESTION = 'How do you want to use it?'
RUN_AGAIN = 'Run ./setup again to see its status, update it, change your answers or remove it.'
ENDED = 'This setup page has ended. Run ./setup again for a new address.'
CHECK_AGAIN = 'Check again'
DELETE_RECORDS = "Delete everyone's records and stored copies"
REMOVE_HOST = 'Remove AIOSubtitles'
REMOVE_HOST_TEXT = ("This stops AIOSubtitles and removes its container with everyone's records and stored copies. "
                    "The sync configs it made on people's AIOStreams stay there, because opening a record needs that "
                    "person's own link.")
SECRET_DOTS = '•' * 16
REMOVE_TEXT = ("This takes subsync out of your config (1 entry), deletes subsync's own config, and removes the subsync "
               "container with its stored copies. Your other subtitle sources stay.")

# words the screens use besides explain.py's texts and the site's UI_WORDS, each approved by the owner
INSTALLER_WORDS = (
    TITLE, QUESTION, *[w for way in WAYS for w in way[1:]], *INTRO, *BEFORE,
    *PERSONAL, *HOSTING, RAIL,
    *[lim.label for lim in limit_list.personal(audio=True, usenet=True) + limit_list.hosting()],
    *[heading for heading, _ in limit_list.HOSTING],
    'GB', 'MB', 'files',
    'Save these changes',
    'Web address', 'Trusted proxy', 'Secret key', 'SubSource API key (optional)',
    'Updates', 'Update', 'Your answers', 'Change your answers', 'Remove subsync',
    RUN_AGAIN, ENDED, CHECK_AGAIN, DELETE_RECORDS, REMOVE_HOST, REMOVE_HOST_TEXT,
    REMOVE_TEXT,
    update.ABOUT, update.ABOUT_HOSTING, update.NEWEST, update.NOT_GIT,
    # the terminal setup's questions and lines, as labels and headings
    'What address do you open AIOStreams at?', 'More than one AIOStreams runs here. Which one do your apps use?',
    'Config ID (or paste your Stremio link)', 'Its password', 'Which subtitle languages should be synced?',
    'Also sync files that carry no subtitles of their own, by listening to the audio?', 'SubDL API key (optional)',
    'Wyzie Subs API key (optional)', "subsync's address for your apps",
    'Your config has no subtitle source, so there is nothing to sync yet. Add OpenSubtitles (free, no account needed)?',
    "Turn off AIOStreams' 5-minute subtitle cache for subsync?", 'AIOStreams sign-in (admin): username',
    'AIOStreams sign-in to save configs: username', 'Do the test read now?', 'AIOSubtitles · subsync status',
    'Already set up here: your answers are kept unless you change them.', 'Removing subsync',
    'setup also added OpenSubtitles to your config. Take that out too?',
    'These stay, in case you set it up again; remove them when you no longer need them:',
    'AIOStreams', 'languages', 'audio', 'config', "subsync's config", 'logs',
    # Usenet
    'Sync files you play from Usenet too?', 'Let people sync Usenet files too?', "subsync's Usenet config",
    "subsync's Usenet config (new)", "subsync's Usenet config (update)",
    "subsync's Usenet config answers (no search made, to spare your indexer's daily calls)",
    "subsync's Usenet config doesn't answer. Run ./setup again, or send the output of ./setup --status with a bug report.",
)


# ------------------------------------------------------------------------------------------- the frame

def _page(content, refresh=None, kind='letter'):
    return ui.page(TITLE, f'<div class="wrap">{ui.top(explain.CONTACT, start="./", privacy=False)}{content}</div>', kind,
                   refresh=refresh)


def _explanation(key):
    entry = explain.QUESTIONS.get(key) or explain.SETTINGS.get(key)
    return entry[1] if entry else None


def _error(problem, key):
    return problem[1] if problem and problem[0] == key and problem[1] else None


def _input(key, label, value='', problem=None, kind='text', mono=False, reveal=False, help_text=None, attrs=''):
    a = (f'value="{e(value)}" ' if value else '') + 'autocomplete="off" spellcheck="false"' + (f' {attrs}' if attrs else '')
    return ui.field(key.lower(), label, _explanation(key) if help_text is None else help_text, name=key, kind=kind,
                    mono=mono, error=_error(problem, key), attrs=a, reveal=reveal)


def _choice(key, title, checked, needs=None):
    return ui.choice(key.lower(), title, _explanation(key), name=key, checked=checked, value='yes', needs=needs)


def _continue(enabled=True, label='Continue', arrow=True):
    if enabled:
        return ui.button(label, 'primary', type='submit', attrs='name="go" value="next"', arrow=arrow)
    return ui.button(label, 'primary', type='button', attrs='disabled', arrow=arrow)


def _back(href):
    return ui.link_button('Back', href, 'secondary')


def _rows(rows):
    """The engine's rows: check rows, notes under them, and progress lines."""
    out, checks, progress = [], [], []

    def flush():
        if checks:
            out.append(ui.check_rows(list(checks)))
            checks.clear()
        if progress:
            out.append(ui.progress_list(list(progress)))
            progress.clear()
    for kind, text, more in rows:
        if kind in ('ok', 'warn', 'bad'):
            if progress:
                flush()
            checks.append((kind, text, more))
        elif kind in ('done', 'now', 'next'):
            if checks:
                flush()
            progress.append((kind, text))
        else:
            flush()
            out.append(f'<p class="help">{e(text)}</p>')
    flush()
    return ''.join(out)


def _progress(rows):
    """Work as it goes (installing, updating): the lines done and the one running in one progress list, as installer
    piece 3 draws it; warnings and problems stay check rows under it."""
    return _rows([('done' if kind == 'ok' else kind, text, more) for kind, text, more in rows])


def _stop(stop):
    """A stop: the problem and its way out, as one problem row."""
    return ui.check_rows([('bad', stop[0], stop[1])]) if stop else ''


def _steps(route):
    return (HOSTING, HOSTING_IDS) if route == 'host' else (PERSONAL, PERSONAL_IDS)


def step(route, step_id, body, actions, back=True, refresh=None, done=False, form=True):
    """One step in the installer's frame: its heading, its body and its actions, in one form posting to itself."""
    names, ids = _steps(route)
    i = ids.index(step_id)
    back_href = (ids[i - 1] if i else './') if back else False
    inner = f'<h1 tabindex="-1">{e(names[i])}</h1><div class="body">{body}</div><div class="actions">{actions}</div>'
    content = f'<form method="post" novalidate>{inner}</form>' if form else inner
    return _page(ui.step_frame(names, i, content, RAIL, done=done, back=back_href), refresh=refresh)


def _back_and_continue(route, step_id, enabled=True, label='Continue'):
    _, ids = _steps(route)
    i = ids.index(step_id)
    return _back(ids[i - 1] if i else './') + _continue(enabled, label, arrow=label == 'Continue')


# ------------------------------------------------------------------------------------------- the start

def start(route='yourself'):
    rows = [ui.radio_row('ROUTE', value, title, text, checked=value == route) for value, title, text in WAYS]
    what = ('<section class="what" aria-labelledby="need-h">' + ''.join(f'<p>{e(p)}</p>' for p in INTRO)
            + f'<h2 id="need-h">Before you start</h2>{ui.dots(BEFORE)}</section>')
    form = (f'<form id="ways" method="post">{ui.radio_group(QUESTION, rows)}<div class="go">'
            f'{ui.button("Continue", "primary", type="submit", arrow=True)}</div></form>')
    main = f'<main class="grid"><h1>{e(TITLE)}</h1>{what}{form}<div class="page-end" aria-hidden="true"></div></main>'
    dock = f'<div class="dock">{ui.button("Continue", "primary", type="submit", attrs="form=\"ways\"", arrow=True)}</div>'
    return ui.page(TITLE, f'<div class="wrap">{ui.top(explain.CONTACT, start="./", privacy=False)}{main}</div>{dock}',
                   'start')


# ------------------------------------------------------------------------------------------- both routes

def machine(route, rows, stop=None):
    return step(route, 'machine', _rows(rows) + _stop(stop), _back_and_continue(route, 'machine', enabled=not stop))


# ------------------------------------------------------------------------------------------- install it for yourself

def aios(rows, ask=None, value='', problem=None, containers=(), done=False, stop=None):
    """Your AIOStreams: what was found; the address question when it wasn't found; the choice when several run."""
    body = _rows(rows) + _stop(stop)
    if ask == 'AIOS_CONTAINER':
        body += ui.radio_group('More than one AIOStreams runs here. Which one do your apps use?',
                               [ui.radio_row('AIOS_CONTAINER', name, name, image, checked=name == value)
                                for name, image in containers])
    elif ask == 'AIOS_URL':
        body += _input('AIOS_URL', 'What address do you open AIOStreams at?', value, problem, kind='url', mono=True)
    return step('yourself', 'aios', body, _back_and_continue('yourself', 'aios', enabled=not stop))


def config(rows, config_id='', problem=None, stop=None):
    """The config your apps use: its ID and password (never filled in again), and what was found in it."""
    body = (_input('AIOS_CONFIG_ID', 'Config ID (or paste your Stremio link)', config_id, problem, mono=True)
            + _input('AIOS_CONFIG_PASSWORD', 'Its password', '', problem, kind='password', reveal=True)
            + _rows(rows) + _stop(stop))
    return step('yourself', 'config', body, _back_and_continue('yourself', 'config', enabled=not stop))


def choices(languages, audio, public_address, problem=None, offer_opensubtitles=False, add_opensubtitles=True,
            limit=4, usenet=None, stop=None):
    """Your choices: the languages (the site's slots), the audio route, Usenet when the config has a Usenet service
    subsync reads through (`usenet` not None), the optional keys, OpenSubtitles when the config has no subtitle
    source, and subsync's address for your apps. `stop`: a config without a debrid service answered no to Usenet."""
    error = _error(problem, 'LANGUAGES')
    langs = ('<div class="field"><span class="label" id="languages-l">Which subtitle languages should be synced?</span>'
             + (f'<p class="field-error" id="languages-error">{e(error)}</p>' if error else '')
             + ui.language_slots([(name, code) for name, _, code in LANGUAGES], languages, limit, 'languages-l',
                                 'languages-help', 'languages-error' if error else None, name='LANGUAGES')
             + f'<p class="help" id="languages-help">{e(_explanation("LANGUAGES"))}</p></div>')
    usenet_box = (_choice('USENET', 'Sync files you play from Usenet too?', usenet),) if usenet is not None else ()
    body = (_stop(stop) + langs + ui.choices(_choice('AUDIO', 'Also sync files that carry no subtitles of their own, by '
                                                              'listening to the audio?', audio), *usenet_box)
            + _input('SUBDL_KEY', 'SubDL API key (optional)', '', problem, kind='password', mono=True, reveal=True)
            + _input('WYZIE_KEY', 'Wyzie Subs API key (optional)', '', problem, kind='password', mono=True, reveal=True))
    if offer_opensubtitles:
        body += ui.choices(_choice('ADD_OPENSUBTITLES', 'Your config has no subtitle source, so there is nothing to sync '
                                                        'yet. Add OpenSubtitles (free, no account needed)?',
                                   add_opensubtitles))
    body += _input('PUBLIC_ADDRESS', "subsync's address for your apps", public_address, problem, kind='url', mono=True)
    return step('yourself', 'choices', body, _back_and_continue('yourself', 'choices'))


def _limit(lim, value, problem):
    text = _explanation(lim.setting)
    if lim.kind == 'segmented':
        field = ui.segmented(lim.setting, lim.label, lim.options, value, text, lim.unit)
        error = _error(problem, lim.setting)
        return field.replace('</div>', f'<p class="field-error">{e(error)}</p></div>', 1) if error else field
    field = ui.number_field(lim.setting, lim.label, limit_list.shown(lim, value), text, lim.unit, auto=lim.kind == 'auto')
    error = _error(problem, lim.setting)
    if error:
        field = field.replace(f'<label for="{lim.setting}">{e(lim.label)}</label>',
                              f'<label for="{lim.setting}">{e(lim.label)}</label><p class="field-error" '
                              f'id="{lim.setting}-error">{e(error)}</p>', 1)
    return field


def limits(route, values, problem=None, audio=False, usenet=False):
    """Limits: a personal install asks the daily total and, with the audio route, the audio limit; a public instance
    every limit in its three groups (each
    install only the limits that do something for it)."""
    if route == 'host':
        body = ''.join(f'<section class="limits" aria-labelledby="g{i}"><h2 id="g{i}">{e(heading)}</h2>'
                       + ''.join(_limit(lim, values.get(lim.setting, lim.default), problem) for lim in group)
                       + '</section>' for i, (heading, group) in enumerate(limit_list.hosting_groups(usenet)))
        return step('host', 'hostlimits', body, _back_and_continue('host', 'hostlimits'))
    body = '<div class="limits">' + ''.join(_limit(lim, values.get(lim.setting, lim.default), problem)
                                            for lim in limit_list.personal(audio, usenet)) + '</div>'
    return step('yourself', 'limits', body, _back_and_continue('yourself', 'limits'))


def review(sections, cache=False, sign_in=False, problem=None, username=''):
    """Your AIOStreams configs: the changes to both, the cache setting with its sign-in, and Save these changes."""
    body = ui.config_changes(sections) + ui.choices(_choice('CACHE', "Turn off AIOStreams' 5-minute subtitle cache for "
                                                                     "subsync?", cache))
    if sign_in or cache:
        body += (_input('AIOS_USERNAME', 'AIOStreams sign-in (admin): username', username, problem)
                 + _input('AIOS_PASSWORD', 'Its password', '', problem, kind='password', reveal=True))
    body += f'<p class="help">{e(_explanation("CONFIRM"))}</p>'
    actions = _back('limits') + ui.button('Save these changes', 'primary', type='submit',
                                          attrs='name="CONFIRM" value="yes"')
    return step('yourself', 'review', body, actions)


def installing(route, rows, running, stop=None, rule=None, rule_intro=None):
    """Installing: the progress; no Back and Continue unavailable until the work is done."""
    step_id = 'hostinstall' if route == 'host' else 'install'
    body = _progress(rows) + _stop(stop)
    if rule:
        body += f'<p>{e(rule_intro)}</p>' + ''.join(f'<p class="help">{e(name)}</p>{ui.code_lines(lines)}'
                                                     for name, lines in rule.items())
    if running:
        return step(route, step_id, body, _continue(False), back=False, refresh=2)
    if stop:
        return step(route, step_id, body, _back_and_continue(route, step_id, enabled=False))
    return step(route, step_id, body, _continue(True), back=False)


def check(route, rows, running, offer_test_read=False, stop=None):
    """Checking that it works: the checks, then the small test read only when asked; Check again on a
    public instance."""
    step_id = 'hostcheck' if route == 'host' else 'check'
    body = _rows(rows) + _stop(stop)
    if running:
        return step(route, step_id, body, _continue(False), back=False, refresh=2)
    if offer_test_read:
        body += ui.choices(_choice('TEST_READ', 'Do the test read now?', False))
    actions = _continue(True)
    if route == 'host':
        actions = ui.button(CHECK_AGAIN, 'secondary', type='submit', attrs='name="go" value="again"') + actions
    return step(route, step_id, body, actions, back=False)


def done(route, title, lines, after):
    """The end of setup: what is running and what to do next (the terminal setup's closing text)."""
    names, ids = _steps(route)
    paragraphs = f'<p class="lead">{e(lines[0])}</p>' + ui.dots(lines[1:])
    for kind, text in after:
        paragraphs += ui.code_lines([text]) if kind == 'code' else f'<p>{e(text)}</p>'
    paragraphs += f'<p>{e(RUN_AGAIN)}</p>'
    content = f'<h1 tabindex="-1">{e(title)}</h1><div class="body">{paragraphs}</div>'
    return _page(ui.step_frame(names, len(ids) - 1, content, RAIL, done=True, back=False))


# ------------------------------------------------------------------------------------------- host a public instance

def host_address(address='', proxy='gateway', problem=None, stop=None):
    body = (_input('HOST_ADDRESS', 'Web address', address, problem, kind='url', mono=True)
            + _input('SUBSYNC_TRUSTED_PROXY', 'Trusted proxy', proxy, problem, mono=True) + _stop(stop))
    return step('host', 'address', body, _back_and_continue('host', 'address'))


def host_signups(invite, problem=None):
    """Sign-ups: the invite code, and the secret key as a fixed row of dots: it never reaches the page."""
    body = (_input('SUBSYNC_PUBLIC_INVITE', 'Invite code', invite, problem, mono=True)
            + ui.field('secret', 'Secret key', _explanation('SUBSYNC_SECRET_KEY'), name='secret', kind='text', mono=True,
                       attrs=f'value="{SECRET_DOTS}" readonly tabindex="-1" aria-readonly="true"'))
    return step('host', 'signups', body, _back_and_continue('host', 'signups'))


def host_choices(problem=None, usenet=False):
    body = (_input('SUBDL_KEY', 'SubDL API key (optional)', '', problem, kind='password', mono=True, reveal=True)
            + _input('SUBSYNC_SUBSOURCE_KEY', 'SubSource API key (optional)', '', problem, kind='password', mono=True,
                     reveal=True)
            + ui.choices(_choice('SUBSYNC_USENET', 'Let people sync Usenet files too?', usenet)))
    return step('host', 'hostchoices', body, _back_and_continue('host', 'hostchoices'))


# ------------------------------------------------------------------------------------------- after setup

def after_setup(route, rows, details, running_version, ready, problem=None, offer_opensubtitles=False, config=''):
    """The page after setup: the status, updates, your answers, removing. `ready`: update.Ready, or None
    while nothing was looked for."""
    status = (f'<h1>AIOSubtitles · subsync status</h1><div class="grid12 band">' + ui.check_rows(list(rows))
              + '<dl class="details">' + ''.join(f'<div><dt>{e(label)}</dt><dd>{e(value)}</dd></div>'
                                                 for label, value in details) + '</dl></div>')
    running = e(update.RUNNING.format(version=running_version))
    if ready and ready.how == 'git':
        panel = ui.update_panel('Updates', update.ABOUT_HOSTING if route == 'host' else update.ABOUT, running,
                                update.READY.format(version=ready.version), 'Update')
        panel = f'<form method="post" action="update">{panel}</form>'
    else:
        words = {'newest': update.NEWEST, 'not-git': update.NOT_GIT}.get(getattr(ready, 'how', None))
        if ready and ready.how == 'error':
            words = update.LOOK_FAILED.format(reason=ready.reason)
        panel = ui.update_panel('Updates', update.ABOUT_HOSTING if route == 'host' else update.ABOUT,
                                running + (f'</p><p>{e(words)}' if words else ''), None, 'Update')
    answers = (f'<section class="grid12 row" aria-labelledby="ans-h"><div class="about"><h2 id="ans-h">Your answers</h2>'
               f'<p class="help">{e("Already set up here: your answers are kept unless you change them.")}</p></div>'
               f'<form class="controls" method="post" action="answers"><div class="actions">'
               f'{ui.button("Change your answers", "secondary", type="submit")}</div></form></section>')
    if route == 'host':
        controls = (ui.choices(ui.choice('confirm', DELETE_RECORDS, REMOVE_HOST_TEXT, name='CONFIRM', value='yes'))
                    + (f'<p class="field-error">{e(problem[1])}</p>' if problem and problem[1] else '')
                    + f'<div class="actions">{ui.button(REMOVE_HOST, "danger", type="submit")}</div>')
        about = REMOVE_HOST_TEXT
    else:
        controls = _input('AIOS_CONFIG_PASSWORD', f'Password of your config {config}', '', problem, kind='password',
                          reveal=True)
        if offer_opensubtitles:
            controls += ui.choices(_choice('REMOVE_OPENSUBTITLES', 'setup also added OpenSubtitles to your config. Take '
                                                                   'that out too?', True))
        controls += ('<input type="hidden" name="CONFIRM" value="yes">'
                     f'<div class="actions">{ui.button("Remove subsync", "danger", type="submit")}</div>')
        about = REMOVE_TEXT
    remove = (f'<section class="grid12 row" aria-labelledby="rm-h"><div class="about"><h2 id="rm-h">Removing subsync</h2>'
              f'<p class="help">{e(about)}</p></div><form class="controls" method="post" action="remove">{controls}</form>'
              '</section>')
    return ui.page(TITLE, f'<div class="wrap">{ui.top(explain.CONTACT, start="./", privacy=False)}<main>{status}'
                          f'<div class="rows">{panel}{answers}{remove}</div></main></div>', 'settings')


def working(heading, rows, running, stop=None, back='./'):
    """A piece of work after setup (an update, removing): its progress, then the way back to the page after setup."""
    body = _progress(rows) + _stop(stop)
    actions = '' if running else ui.link_button('Continue', back, 'primary', arrow=True)
    content = (f'<h1 tabindex="-1">{e(heading)}</h1><div class="body">{body}</div>'
               + (f'<div class="actions">{actions}</div>' if actions else ''))
    return _page(f'<main class="letter">{content}</main>', refresh=2 if running else None)


def removed(heading, rows, stays):
    body = _rows(rows)
    if stays:
        body += (f'<p>{e("These stay, in case you set it up again; remove them when you no longer need them:")}</p>'
                 + ui.dots(stays))
    return _page(f'<main class="letter"><h1 tabindex="-1">{e(heading)}</h1><div class="body">{body}</div></main>')


def ended():
    return _page(f'<main class="letter"><h1>{e(TITLE)}</h1><div class="body"><p>{e(ENDED)}</p></div></main>')
