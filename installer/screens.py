"""The setup page's screens.

Each function returns a whole page, built from subsync's parts (subsync/subsync/ui.py and its one stylesheet) like the
public site, so the installer looks and works like it. Every word is an approved one: subsync's explain.py,
the site's pages.UI_WORDS, and INSTALLER_WORDS below (each with the decision that approved it); values from the
person, from AIOStreams and the engine's rows are escaped by the parts. Every form posts to the page it is on and works
without the script; a page whose work runs shows its progress again every REFRESH_S seconds (ui.page's refresh).
"""
from installer import hosting
from installer import limits as limit_list
from installer import plan, update
from subsync import explain, ui
from subsync.aiosconfig import LANGUAGE_OPTIONS, MAX_LANGUAGES
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
# the free disk space AIOSubtitles needs, in GB: its image (update.IMAGE_GB), the one before it kept after an update
# (the same) and the stored copies (up to 2 by default). The README's "What you need" gives the same number; with
# less free, the first step warns
DISK_GB = 4
BEFORE = ('Docker on this machine.', f'About {DISK_GB} GB of free disk space.',
          'For yourself: the ID and password of the AIOStreams config your apps use.',
          'For a public instance: a web address with https that reaches this machine.')
WAYS = (('yourself', 'Install it for yourself', 'This adds it to the AIOStreams you already run.'),
        ('host', 'Host a public instance', 'Runs here with its own sign-up page, so other people can use it with '
                                           'their own AIOStreams.'))
QUESTION = 'How do you want to use it?'
# Each question the page and the engine both ask, by its answers-file key: the page's label is the engine's question
# (the terminal's prompt, and the page's '"…" needs an answer.'), so the two never drift apart.
LABELS = {
    'ROUTE': QUESTION,
    'AIOS_CONTAINER': 'More than one AIOStreams runs here. Which one do your apps use?',
    'AIOS_URL': 'What address do you open AIOStreams at?',
    'AIOS_CONFIG_ID': 'Config ID (or paste your install link)',
    'AIOS_CONFIG_PASSWORD': 'Its password',
    'LANGUAGES': 'Which subtitle languages should be synced?',
    'AUDIO': plan.AUDIO_QUESTION,
    'USENET': 'Sync files you play from Usenet too?',
    'SUBDL_KEY': 'SubDL API key (optional)',
    'WYZIE_KEY': 'Wyzie Subs API key (optional)',
    'TMDB_KEY': 'TMDB API key (optional)',
    'ADD_OPENSUBTITLES': 'Your config has no subtitle source. Add OpenSubtitles (free, no account needed)?',
    'PUBLIC_ADDRESS': "subsync's address for your apps",
    'CACHE': "Turn off AIOStreams' 5-minute subtitle cache for subsync?",
    'AIOS_PASSWORD': 'Its password',
    'TEST_READ': 'Do the test read now?',
    'REMOVE_OPENSUBTITLES': 'setup also added OpenSubtitles to your config. Take that out too?',
    'HOST_ADDRESS': 'Web address',
    'SUBSYNC_TRUSTED_PROXY': 'Trusted proxy',
    'SUBSYNC_PUBLIC_INVITE': 'Invite code',
    'SUBSYNC_SUBSOURCE_KEY': 'SubSource API key (optional)',
    'SUBSYNC_TMDB_PERSONAL': hosting.TMDB_PERSONAL_QUESTION,
    'SUBSYNC_USENET': 'Let people sync Usenet files too?',
    'SUBSYNC_AUDIO_ROUTE': hosting.AUDIO_QUESTION,
}
TMDB_FOR_EVERYONE = 'TMDB API key for everyone (optional)'          # TMDB_KEY's label on a public instance
# under subsync's address question when it needs this machine's LAN address and Docker runs in a virtual machine
# (Docker Desktop, Colima), where setup can't find it (engine.Engine.address_note)
VM_ADDRESS = ("Docker runs in a virtual machine here, so setup can't find this machine's address on your network: type "
              'it, like http://192.168.1.5:8790, and check it from your phone.')
SIGN_IN_ADMIN = 'AIOStreams sign-in (admin): username'              # AIOS_USERNAME's: for the cache setting,
SIGN_IN_TO_SAVE = 'AIOStreams sign-in to save configs: username'    # and for an AIOStreams that asks one to save
CONFIG_PASSWORD = 'Password of your config {config}'    # AIOS_CONFIG_PASSWORD's when removing subsync or restoring
RUN_AGAIN = 'Run ./setup again to see its status, update it, change your answers or remove it.'
ENDED = 'This setup page has ended. Run ./setup again for a new address.'
CHECK_AGAIN = 'Check again'
DELETE_RECORDS = "Delete everyone's records and stored copies"
TICK_TO_REMOVE = f'Tick "{DELETE_RECORDS}" to remove AIOSubtitles.'
REMOVE_HOST = 'Remove AIOSubtitles'
REMOVE_HOST_TEXT = ("This stops AIOSubtitles and removes its container with everyone's records and stored copies. "
                    "The sync configs it made on people's AIOStreams stay there, because opening a record needs that "
                    "person's own link.")
SECRET_DOTS = '•' * 16
# What removing subsync does, naming the configs this install has: its own, its Usenet one, or both (remove_text)
REMOVE_TEXT = ('This takes subsync out of your config{count}, deletes {configs}, and removes the subsync container '
               'with its stored copies. Your other subtitle sources stay.')
REMOVED_CONFIGS = {'own': "subsync's own config", 'both': "subsync's own config and its Usenet config",
                   'usenet': "subsync's Usenet config"}
REMOVE_SAVED_KEY = 'Remove the saved key'       # under an optional key's field while one is saved
# the page after setup's heading (and ./setup --status's), and removing's, by route
STATUS = {'yourself': 'AIOSubtitles · subsync status', 'host': 'AIOSubtitles status'}
REMOVING = {'yourself': 'Removing subsync', 'host': 'Removing AIOSubtitles'}
KEPT_ANSWERS = ('Already set up here: your answers are kept unless you change them. To switch between installing it '
                'for yourself and hosting a public instance, remove it first.')
TRY_AGAIN = 'Try again'         # a stopped install's way on: it runs the install again
# an error setup doesn't know (a full disk, a folder it can't write): the stop and its way out; the details stay in the
# terminal, never on the page
UNEXPECTED = ('Something unexpected went wrong, so setup stopped here.',
              'Run ./setup again. If it happens again, send what the terminal shows with a bug report.')
# a refused answer's summary at the top of its step, as the site's settings page titles one (pages.UI_WORDS)
REFUSED = "That didn't go through"
UPDATES = 'Updates'     # the update panel's heading after setup, and the updating page's (web.after_updating)
# after removing, what stays on the machine: the page's (removed) and the terminal's (engine.say_what_stays)
STAYS = 'These stay, in case you set it up again; remove them when you no longer need them:'
REFRESH_S = 2           # seconds after which a page whose work runs (installing, checking, updating) shows it again

# words the screens use besides explain.py's texts and the site's UI_WORDS, each approved by the owner
INSTALLER_WORDS = (
    TITLE, QUESTION, *[w for way in WAYS for w in way[1:]], *INTRO, *BEFORE,
    *PERSONAL, *HOSTING, RAIL,
    *[lim.label for lim in limit_list.personal(audio=True, usenet=True) + limit_list.hosting(audio=True)],
    *[heading for heading, _ in limit_list.HOSTING],
    'GB', 'MB', 'files',
    'Save these changes',
    # the questions, as labels and headings (the terminal setup's questions too)
    *LABELS.values(), TMDB_FOR_EVERYONE, SIGN_IN_ADMIN, SIGN_IN_TO_SAVE,
    VM_ADDRESS,
    'Secret key',
    # the original-language lookup: TMDB's notice and its logo's name
    explain.TMDB_NOTICE, 'TMDB',
    UPDATES, 'Update', 'Your answers', 'Change your answers', 'Remove subsync',
    RUN_AGAIN, ENDED, CHECK_AGAIN, DELETE_RECORDS, TICK_TO_REMOVE, REMOVE_HOST, REMOVE_HOST_TEXT,
    TRY_AGAIN, *UNEXPECTED,
    # a button row's refused limit, as the page says it
    *[limit_list.choose(lim) for lim in limit_list.hosting(audio=True) if lim.kind == 'segmented'],
    *[REMOVE_TEXT.format(count='', configs=configs) for configs in REMOVED_CONFIGS.values()], REMOVE_SAVED_KEY,
    KEPT_ANSWERS,
    update.ABOUT, update.NEWEST, update.ROLLBACK_HEADING, update.ROLLBACK_ABOUT,
    update.RESTORE_HELP,
    # the terminal setup's lines, as headings
    *STATUS.values(),
    'Already set up here: your answers are kept unless you change them.', *REMOVING.values(),
    STAYS,
    'AIOStreams', 'languages', 'audio', 'config', "subsync's config", 'logs',
    # Usenet
    "subsync's Usenet config",
    # the audio route's answers
    *[answer for _, answer in plan.AUDIO_ANSWERS],
    "subsync's Usenet config (new)", "subsync's Usenet config (update)",
    "subsync's Usenet config (deleted)", "subsync's own config (deleted)",
    "subsync's Usenet config answers (no search made, to spare your indexer's daily calls)",
    "subsync's Usenet config doesn't answer. Run ./setup again, or send the output of ./setup --status with a bug report.",
)


# ------------------------------------------------------------------------------------------- the frame

def _page(content, refresh=None, heading=None):
    """A page in setup's frame, titled by its `heading` (its step's name, or its own heading) before setup's name, so
    tabs, history and a screen reader's announcement tell the pages apart."""
    title = f'{heading} · {TITLE}' if heading and heading != TITLE else TITLE
    return ui.page(title, f'<div class="wrap">{ui.top(explain.CONTACT, start="./", site=False)}{content}</div>',
                   'letter', refresh=refresh)


def _explanation(key, settings=None):
    """A question's explanation (a limit's: its setting's); one that states a setting with `settings`' value, this
    install's (explain.setup_help). None for a key without one."""
    return explain.setup_help(key, settings) if key in explain.QUESTIONS or key in explain.SETTINGS else None


def _error(problem, key):
    return problem[1] if problem and problem[0] == key and problem[1] else None


def _field_id(key):
    """A question's field id on the setup page: its answers-file key in small letters (_input, _choice, the audio
    answers); the languages' is their first box (ui.language_slots: their group can't take focus). A limit's is
    its setting (_limit_ids). A refused answer's summary links to it (_summary)."""
    return ui.slot_id(1, key) if key == 'LANGUAGES' else key.lower()


def _ids(*keys):
    """The fields a step drew for these questions, as `step` takes them: each one's id by its key."""
    return {key: _field_id(key) for key in keys}


def _input(key, label, value='', problem=None, kind='text', mono=False, reveal=False, help_text=None, attrs=''):
    a = (f'value="{e(value)}" ' if value else '') + 'autocomplete="off" spellcheck="false"' + (f' {attrs}' if attrs else '')
    return ui.field(_field_id(key), label, _explanation(key) if help_text is None else help_text, name=key, kind=kind,
                    mono=mono, error=_error(problem, key), attrs=a, reveal=reveal)


def _choice(key, title, checked, settings=None):
    return ui.choice(_field_id(key), title, _explanation(key, settings), name=key, checked=checked, value='yes')


def _continue(enabled=True):
    if enabled:
        return ui.button('Continue', 'primary', type='submit', attrs='name="go" value="next"', arrow=True)
    return ui.button('Continue', 'primary', type='button', attrs='disabled', arrow=True)


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
            progress.append((kind, text, more))
        else:
            flush()
            out.append(f'<p class="help">{e(text)}</p>')
    flush()
    return ''.join(out)


def _progress(rows):
    """Work as it goes (installing, updating): the lines done and the one running in one progress list, as installer
    piece 3 draws it, each with its notes under it; warnings and problems stay check rows under it."""
    return _rows([('done' if kind == 'ok' else kind, text, more) for kind, text, more in rows])


def _stop(stop):
    """A stop: the problem and its way out, as one problem row."""
    return ui.check_rows([('bad', stop[0], stop[1])]) if stop else ''


def _steps(route):
    return (HOSTING, HOSTING_IDS) if route == 'host' else (PERSONAL, PERSONAL_IDS)


def _summary(problem, fields):
    """A refused answer's summary for the top of its step, as the site's pages show one: its message word for word,
    and a link to its field when the step drew it (`fields`: the id of each field drawn, by its key)."""
    if not problem or not problem[1]:
        return ''
    return ui.problem_summary(problem[1], REFUSED, fields.get(problem[0]))


def step(route, step_id, body, actions, back=True, refresh=None, problem=None, fields=None):
    """One step in the installer's frame: its heading, its body and its actions, in one form posting to itself.
    `problem`: a refused answer (key, message), summed up at the top of the body (the script focuses it), linking to
    its field among `fields` (the id of each field the body drew, by its key: _ids)."""
    names, ids = _steps(route)
    i = ids.index(step_id)
    back_href = (ids[i - 1] if i else './') if back else False
    content = (f'<form method="post" novalidate><h1>{e(names[i])}</h1>'
               f'<div class="body">{_summary(problem, fields or {})}{body}</div>'
               f'<div class="actions">{actions}</div></form>')
    return _page(ui.step_frame(names, i, content, RAIL, back=back_href), refresh=refresh, heading=names[i])


def _back_and_continue(route, step_id, enabled=True):
    _, ids = _steps(route)
    i = ids.index(step_id)
    return _back(ids[i - 1] if i else './') + _continue(enabled)


# ------------------------------------------------------------------------------------------- the start

def start(route='yourself'):
    rows = [ui.radio_row('ROUTE', value, title, text, checked=value == route) for value, title, text in WAYS]
    # the list under the introduction in the left column (the form takes the right one), named on its own
    what = ('<section class="what">' + ''.join(f'<p>{e(p)}</p>' for p in INTRO)
            + f'<section aria-labelledby="need-h"><h2 id="need-h">Before you start</h2>{ui.dots(BEFORE)}</section>'
            '</section>')
    form = (f'<form id="ways" method="post">{ui.radio_group(QUESTION, rows)}<div class="go">'
            f'{ui.button("Continue", "primary", type="submit", arrow=True)}</div></form>')
    main = f'<main class="grid"><h1>{e(TITLE)}</h1>{what}{form}<div class="page-end" aria-hidden="true"></div></main>'
    dock = f'<div class="dock">{ui.button("Continue", "primary", type="submit", attrs="form=\"ways\"", arrow=True)}</div>'
    return ui.page(TITLE, f'<div class="wrap">{ui.top(explain.CONTACT, start="./", site=False)}{main}</div>{dock}',
                   'start')


# ------------------------------------------------------------------------------------------- both routes

def machine(route, rows, stop=None):
    return step(route, 'machine', _rows(rows) + _stop(stop), _back_and_continue(route, 'machine', enabled=not stop))


# ------------------------------------------------------------------------------------------- install it for yourself

def aios(rows, ask=None, value='', problem=None, containers=(), stop=None):
    """Your AIOStreams: what was found; the address question when it wasn't found; the choice when several run
    (`containers`: their names, each with its image under it)."""
    body, fields = _rows(rows) + _stop(stop), {}
    if ask == 'AIOS_CONTAINER':
        body += ui.radio_group(LABELS['AIOS_CONTAINER'],
                               [ui.radio_row('AIOS_CONTAINER', name, name, image, checked=name == value)
                                for name, image in containers])
    elif ask == 'AIOS_URL':
        body += _input('AIOS_URL', LABELS['AIOS_URL'], value, problem, kind='url', mono=True)
        fields = _ids('AIOS_URL')
    return step('yourself', 'aios', body, _back_and_continue('yourself', 'aios', enabled=not stop), problem=problem,
                fields=fields)


def config(rows, config_id='', problem=None, stop=None):
    """The config your apps use: its ID and password (never filled in again), and what was found in it. After a stop
    about the config, Continue reads it again."""
    body = (_input('AIOS_CONFIG_ID', LABELS['AIOS_CONFIG_ID'], config_id, problem, mono=True)
            + _input('AIOS_CONFIG_PASSWORD', LABELS['AIOS_CONFIG_PASSWORD'], '', problem, kind='password', reveal=True)
            + _rows(rows) + _stop(stop))
    return step('yourself', 'config', body, _back_and_continue('yourself', 'config'), problem=problem,
                fields=_ids('AIOS_CONFIG_ID', 'AIOS_CONFIG_PASSWORD'))


def choices(languages, audio, public_address, problem=None, offer_opensubtitles=False, add_opensubtitles=True,
            usenet=None, stop=None, saved=(), rows=(), settings=None, address_note=None):
    """Your choices: what the step reported (`rows`: why Usenet stays off, a saved key removed), the languages (the
    site's slots), the audio route (`audio`: the answer picked, no, pick or open), Usenet when the config has a
    Usenet service subsync reads through (`usenet` not None), the optional keys (`saved`: those with a saved
    key), OpenSubtitles when the config has no subtitle source, and subsync's address for your apps, with
    `address_note` under it (VM_ADDRESS). `stop`: a config without a debrid service answered no to Usenet.
    `settings`: subsync's on this install, whose values the audio and Usenet explanations state. As many language
    slots as the service takes (MAX_LANGUAGES)."""
    error = _error(problem, 'LANGUAGES')
    langs = (f'<div class="field"><span class="label" id="languages-l">{e(LABELS["LANGUAGES"])}</span>'
             + (f'<p class="field-error" id="languages-error">{e(error)}</p>' if error else '')
             + ui.language_slots(LANGUAGE_OPTIONS, languages, MAX_LANGUAGES, 'languages-l',
                                 'languages-help', 'languages-error' if error else None, name='LANGUAGES')
             + f'<p class="help" id="languages-help">{e(_explanation("LANGUAGES"))}</p></div>')
    usenet_box = (_choice('USENET', LABELS['USENET'], usenet, settings=settings),) if usenet is not None else ()
    audio_rows = [ui.radio_row('AUDIO', value, answer, '', checked=value == audio)
                  for value, answer in plan.AUDIO_ANSWERS]
    body = (_rows(rows) + _stop(stop) + langs + ui.radio_group(LABELS['AUDIO'], audio_rows,
                                                                help_text=_explanation('AUDIO', settings),
                                                                id=_field_id('AUDIO'))
            + (ui.choices(*usenet_box) if usenet_box else '')
            + _key('SUBDL_KEY', LABELS['SUBDL_KEY'], problem, saved)
            + _key('WYZIE_KEY', LABELS['WYZIE_KEY'], problem, saved)
            + _tmdb(LABELS['TMDB_KEY'], problem, saved))
    if offer_opensubtitles:
        body += ui.choices(_choice('ADD_OPENSUBTITLES', LABELS['ADD_OPENSUBTITLES'], add_opensubtitles))
    body += _input('PUBLIC_ADDRESS', LABELS['PUBLIC_ADDRESS'], public_address, problem, kind='url', mono=True)
    if address_note:
        body += f'<p class="help">{e(address_note)}</p>'
    fields = _ids('LANGUAGES', 'AUDIO', *(('USENET',) if usenet_box else ()), 'SUBDL_KEY', 'WYZIE_KEY', 'TMDB_KEY',
                  *(('ADD_OPENSUBTITLES',) if offer_opensubtitles else ()), 'PUBLIC_ADDRESS')
    return step('yourself', 'choices', body, _back_and_continue('yourself', 'choices'), problem=problem, fields=fields)


# the keyboard phones show for a limit's box, by its kind: one with a decimal point for a decimal, the normal
# one where the word auto is taken too, else digits
KEYPADS = {'decimal': 'decimal', 'auto': 'text'}


def _limit(lim, value, problem, text=None):
    """A limit's field, with its problem under its label, tied to the box or the row of buttons; `text`: its
    explanation, when it isn't its setting's."""
    text, error = text or _explanation(lim.setting), _error(problem, lim.setting)
    if lim.kind == 'segmented':
        return ui.segmented(lim.setting, lim.label, lim.options, value, text, lim.unit, error=error)
    return ui.number_field(lim.setting, lim.label, limit_list.shown(lim, value), text, lim.unit,
                           keypad=KEYPADS.get(lim.kind, 'numeric'), error=error)


def _limit_ids(asked):
    """The limits' fields, as `step` takes them: each one's id is its setting (ui.number_field's, ui.segmented's)."""
    return {lim.setting: lim.setting for lim in asked}


def _choose(problem, asked):
    """A refused limit as the page says it: a row of buttons' problem says what to choose (the engine's words, which
    the terminal shows, say what to type)."""
    lim = next((lim for lim in asked if problem and problem[1] and lim.setting == problem[0]), None)
    return (lim.setting, limit_list.choose(lim)) if lim and lim.kind == 'segmented' else problem


def limits(route, values, problem=None, audio=False, usenet=False, stop=None, settings=None):
    """Limits: a personal install asks the daily total and, with the audio route, the audio limit; a public instance
    every limit in its three groups (each
    install only the limits that do something for it). On a personal install the audio limit is ./setup's question
    (AUDIO_BUDGET), explained as the terminal asks it, with the wait of this install's `settings`; a public
    instance's is its setting."""
    if route == 'host':
        asked = limit_list.hosting(usenet, audio)
        problem = _choose(problem, asked)
        groups = ''.join(f'<section class="limits" aria-labelledby="g{i}"><h2 id="g{i}">{e(heading)}</h2>'
                         + ''.join(_limit(lim, values.get(lim.setting, lim.default), problem) for lim in group)
                         + '</section>' for i, (heading, group) in enumerate(limit_list.hosting_groups(usenet, audio)))
        return step('host', 'hostlimits', _stop(stop) + groups, _back_and_continue('host', 'hostlimits'),
                    problem=problem, fields=_limit_ids(asked))
    asked = limit_list.personal(audio, usenet)
    problem = _choose(problem, asked)
    audio_text = _explanation('AUDIO_BUDGET', settings)
    body = '<div class="limits">' + ''.join(_limit(lim, values.get(lim.setting, lim.default), problem,
                                                   audio_text if lim is limit_list.AUDIO else None)
                                            for lim in asked) + '</div>'
    return step('yourself', 'limits', _stop(stop) + body, _back_and_continue('yourself', 'limits'), problem=problem,
                fields=_limit_ids(asked))


def review(sections, cache=False, sign_in=False, problem=None, username='', rows=(), stop=None):
    """Your AIOStreams configs: what saving them wrote so far and how it stopped, the changes to both, the cache setting
    with its sign-in (an admin's for the cache setting, else the one AIOStreams asks to save configs), and Save these
    changes."""
    body = (_rows(rows) + _stop(stop) + ui.config_changes(sections)
            + ui.choices(_choice('CACHE', LABELS['CACHE'], cache)))
    fields = _ids('CACHE')
    if sign_in or cache:
        body += (_input('AIOS_USERNAME', SIGN_IN_ADMIN if cache else SIGN_IN_TO_SAVE, username, problem)
                 + _input('AIOS_PASSWORD', LABELS['AIOS_PASSWORD'], '', problem, kind='password', reveal=True))
        fields.update(_ids('AIOS_USERNAME', 'AIOS_PASSWORD'))
    body += f'<p class="help">{e(_explanation("CONFIRM"))}</p>'
    actions = _back('limits') + ui.button('Save these changes', 'primary', type='submit',
                                          attrs='name="CONFIRM" value="yes"')
    return step('yourself', 'review', body, actions, problem=problem, fields=fields)


def installing(route, rows, running, stop=None, rule=None, rule_intro=None, rule_note=None, warnings=()):
    """Installing: the warnings of saving the configs before it (`warnings`: the cache setting's, say), the progress,
    and the web server's rule with its note under it; no Back and Continue unavailable until the work is done. A stopped
    install has Back and Try again, which runs it again."""
    step_id = 'hostinstall' if route == 'host' else 'install'
    body = _rows(warnings) + _progress(rows) + _stop(stop)
    if rule:
        body += f'<p>{e(rule_intro)}</p>' + ''.join(f'<p class="help">{e(name)}</p>{ui.code_lines(lines)}'
                                                     for name, lines in rule.items())
        if rule_note:
            body += f'<p class="help">{e(rule_note)}</p>'
    if running:
        return step(route, step_id, body, _continue(False), back=False, refresh=REFRESH_S)
    if stop:
        _, ids = _steps(route)
        again = ui.button(TRY_AGAIN, 'primary', type='submit', attrs='name="go" value="again"')
        return step(route, step_id, body, _back(ids[ids.index(step_id) - 1]) + again)
    return step(route, step_id, body, _continue(True), back=False)


def check(route, rows, running, offer_test_read=False, stop=None):
    """Checking that it works: the checks, then the small test read only when asked; Check again on a
    public instance."""
    step_id = 'hostcheck' if route == 'host' else 'check'
    body = _rows(rows) + _stop(stop)
    if running:
        return step(route, step_id, body, _continue(False), back=False, refresh=REFRESH_S)
    if offer_test_read:
        body += ui.choices(_choice('TEST_READ', LABELS['TEST_READ'], False))
    actions = _continue(True)
    if route == 'host':
        actions = ui.button(CHECK_AGAIN, 'secondary', type='submit', attrs='name="go" value="again"') + actions
    return step(route, step_id, body, actions, back=False)


def done(route, title, lines, after, warnings=()):
    """The end of setup: the check's warnings under the title when it warned, what is running and what to do next (the
    terminal setup's closing text)."""
    names, ids = _steps(route)
    paragraphs = _rows(warnings) + f'<p class="lead">{e(lines[0])}</p>' + ui.dots(lines[1:])
    for kind, text in after:
        paragraphs += ui.code_lines([text]) if kind == 'code' else f'<p>{e(text)}</p>'
    paragraphs += f'<p>{e(RUN_AGAIN)}</p>'
    content = f'<h1>{e(title)}</h1><div class="body">{paragraphs}</div>'
    return _page(ui.step_frame(names, len(ids) - 1, content, RAIL, done=True, back=False), heading=title)


# ------------------------------------------------------------------------------------------- host a public instance

def host_address(address='', proxy=hosting.TRUSTED_PROXY, problem=None, stop=None):
    body = (_input('HOST_ADDRESS', LABELS['HOST_ADDRESS'], address, problem, kind='url', mono=True)
            + _input('SUBSYNC_TRUSTED_PROXY', LABELS['SUBSYNC_TRUSTED_PROXY'], proxy, problem, mono=True) + _stop(stop))
    return step('host', 'address', body, _back_and_continue('host', 'address'), problem=problem,
                fields=_ids('HOST_ADDRESS', 'SUBSYNC_TRUSTED_PROXY'))


def host_signups(invite, problem=None, stop=None):
    """Sign-ups: the invite code, and the secret key as a fixed row of dots: it never reaches the page."""
    body = (_stop(stop) + _input('SUBSYNC_PUBLIC_INVITE', LABELS['SUBSYNC_PUBLIC_INVITE'], invite, problem, mono=True)
            + ui.field('secret', 'Secret key', _explanation('SUBSYNC_SECRET_KEY'), name='secret', kind='text', mono=True,
                       attrs=f'value="{SECRET_DOTS}" readonly tabindex="-1" aria-readonly="true"'))
    return step('host', 'signups', body, _back_and_continue('host', 'signups'), problem=problem,
                fields=_ids('SUBSYNC_PUBLIC_INVITE'))


def _key(key, label, problem, saved):
    """An optional key's field, never filled in, with the box that removes the saved key under it while one is saved
    (`saved`: the keys saved)."""
    field = _input(key, label, '', problem, kind='password', mono=True, reveal=True)
    if key in saved:
        field += ui.choices(ui.choice(f'remove-{key.lower()}', REMOVE_SAVED_KEY, '', name=f'REMOVE_{key}', value='yes'))
    return field


def _tmdb(label, problem, saved):
    """The original-language lookup's TMDB key, with TMDB's logo and notice under it (its terms ask for them)."""
    return _key('TMDB_KEY', label, problem, saved) + ui.tmdb_credit(explain.TMDB_NOTICE)


def host_choices(problem=None, usenet=False, audio=False, tmdb_personal=False, saved=(), stop=None):
    body = (_stop(stop) + _key('SUBDL_KEY', LABELS['SUBDL_KEY'], problem, saved)
            + _key('SUBSYNC_SUBSOURCE_KEY', LABELS['SUBSYNC_SUBSOURCE_KEY'], problem, saved)
            + _tmdb(TMDB_FOR_EVERYONE, problem, saved)
            + ui.choices(_choice('SUBSYNC_TMDB_PERSONAL', LABELS['SUBSYNC_TMDB_PERSONAL'], tmdb_personal),
                         _choice('SUBSYNC_USENET', LABELS['SUBSYNC_USENET'], usenet),
                         _choice('SUBSYNC_AUDIO_ROUTE', LABELS['SUBSYNC_AUDIO_ROUTE'], audio)))
    return step('host', 'hostchoices', body, _back_and_continue('host', 'hostchoices'), problem=problem,
                fields=_ids('SUBDL_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'TMDB_KEY', 'SUBSYNC_TMDB_PERSONAL', 'SUBSYNC_USENET',
                            'SUBSYNC_AUDIO_ROUTE'))


# ------------------------------------------------------------------------------------------- after setup

def remove_text(keys, count=''):
    """REMOVE_TEXT for the configs this install has (`keys`: state/secrets.env's). `count`: the terminal's " (1
    entry)"; the page asks the config's password with the text, so it can't count them."""
    if keys.get('USENET_UUID'):
        configs = 'both' if keys.get('SYNC_UUID') else 'usenet'
    else:
        configs = 'own'
    return REMOVE_TEXT.format(count=count, configs=REMOVED_CONFIGS[configs])


def after_setup(route, rows, details, running_version, ready, problem=None, offer_opensubtitles=False, config='',
                rollback=None, removing=None, sign_in=False, username='', written=()):
    """The page after setup: the status, updates, your answers, removing. `ready`: update.Ready, from comparing this
    setup's version with the service's. `rollback`: (the earlier version, its database copy's name or None) while one
    is recorded. `removing`: what removing does (remove_text). `sign_in`: removing asks AIOStreams' sign-in to save
    configs, under the lines it wrote before asking (`written`). `problem`: a refused removal (key, message), summed
    up under the heading as on the steps (the script focuses it), linking to its field in the Remove row."""
    status = ('<div class="grid12 band">' + ui.check_rows(list(rows))
              + '<dl class="details">' + ''.join(f'<div><dt>{e(label)}</dt><dd>{e(value)}</dd></div>'
                                                 for label, value in details) + '</dl></div>')
    running = update.RUNNING.format(version=running_version)
    if ready and ready.how == 'ready':
        panel = ui.update_panel(UPDATES, update.ABOUT, running, update.READY.format(version=ready.version), 'Update')
        panel = f'<form method="post" action="update">{panel}</form>'
    else:
        note = update.NEWEST
        if ready and ready.how == 'newer-here':
            note = update.NEWER_HERE.format(running=running_version, setup=ready.version)
        panel = ui.update_panel(UPDATES, update.ABOUT, running, None, 'Update', note=note)
    if rollback:
        version, copy = rollback
        controls = ''
        if copy:
            controls = ui.choices(ui.choice('restore', update.RESTORE_QUESTION.format(version=running_version, name=copy),
                                            update.RESTORE_HELP, name='RESTORE_DATABASES', value='yes'))
        controls += (f'<div class="actions">{ui.button(update.ROLLBACK.format(version=version), "secondary", type="submit")}'
                     '</div>')
        panel += ui.row('rb-h', update.ROLLBACK_HEADING, f'<p class="help">{e(update.ROLLBACK_ABOUT)}</p>',
                        f'<form class="controls" method="post" action="rollback">{controls}</form>')
    answers = ui.row('ans-h', 'Your answers',
                     f'<p class="help">{e(KEPT_ANSWERS)}</p>',
                     f'<form class="controls" method="post" action="answers"><div class="actions">'
                     f'{ui.button("Change your answers", "secondary", type="submit")}</div></form>')
    if route == 'host':
        error = _error(problem, 'REMOVE')
        box = ui.choice('confirm', DELETE_RECORDS, '', name='REMOVE', value='yes',
                        error_id='confirm-error' if error else None)
        controls = ((f'<p class="field-error" id="confirm-error">{e(error)}</p>' if error else '') + ui.choices(box)
                    + f'<div class="actions">{ui.button(REMOVE_HOST, "danger", type="submit")}</div>')
        about, fields = REMOVE_HOST_TEXT, {'REMOVE': 'confirm'}
    else:
        controls = _input('AIOS_CONFIG_PASSWORD', CONFIG_PASSWORD.format(config=config), '', problem,
                          kind='password', reveal=True)
        if sign_in:
            controls += (_rows(written) + _input('AIOS_USERNAME', SIGN_IN_TO_SAVE, username, problem)
                         + _input('AIOS_PASSWORD', LABELS['AIOS_PASSWORD'], '', problem, kind='password', reveal=True))
        if offer_opensubtitles:
            controls += ui.choices(_choice('REMOVE_OPENSUBTITLES', LABELS['REMOVE_OPENSUBTITLES'], True))
        controls += ('<input type="hidden" name="REMOVE" value="yes">'
                     f'<div class="actions">{ui.button("Remove subsync", "danger", type="submit")}</div>')
        about = removing or remove_text({})
        fields = _ids('AIOS_CONFIG_PASSWORD', *(('AIOS_USERNAME', 'AIOS_PASSWORD') if sign_in else ()),
                      *(('REMOVE_OPENSUBTITLES',) if offer_opensubtitles else ()))
    remove = ui.row('rm-h', REMOVING[route], f'<p class="help">{e(about)}</p>',
                    f'<form class="controls" method="post" action="remove">{controls}</form>')
    summary = _summary(problem, fields)
    notes = f'<div class="notes">{summary}</div>' if summary else ''      # as the site's settings page has them
    return ui.page(STATUS[route],         # titled by its heading
                   f'<div class="wrap">{ui.top(explain.CONTACT, start="./", site=False)}<main>'
                   f'<h1>{e(STATUS[route])}</h1>{notes}{status}<div class="rows">{panel}{answers}{remove}</div></main>'
                   '</div>', 'settings')


def working(heading, rows, running, stop=None):
    """A piece of work after setup (an update, removing): its progress, then the way back to the page after setup."""
    body = _progress(rows) + _stop(stop)
    actions = '' if running else ui.link_button('Continue', './', 'primary', arrow=True)
    content = (f'<h1>{e(heading)}</h1><div class="body">{body}</div>'
               + (f'<div class="actions">{actions}</div>' if actions else ''))
    return _page(f'<main class="letter">{content}</main>', refresh=REFRESH_S if running else None, heading=heading)


def removed(heading, rows, stays):
    body = _rows(rows)
    if stays:
        body += f'<p>{e(STAYS)}</p>' + ui.dots(stays)
    return _page(f'<main class="letter"><h1>{e(heading)}</h1><div class="body">{body}</div></main>',
                 heading=heading)


def ended():
    return _page(f'<main class="letter"><h1>{e(TITLE)}</h1><div class="body"><p>{e(ENDED)}</p></div></main>')
