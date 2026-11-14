"""Every screen and state of the setup page with made-up values, for the tests and the screen checks (the installer
plan's §8). The rows are the engine's own words with sample values. Returns {name: (html, the values put in)}: the
values are the only words a screen may show besides the approved ones."""
from installer import engine, hosting, limits, screens, update
from subsync import realdebrid

ADDRESS = 'https://subs.example.com'
PUBLIC = 'https://aio.example.com'


def every_state():
    out = {}

    def add(name, html, *values):
        out[name] = (html, set(values))

    found = [('ok', 'Docker and Docker Compose work', None), ('ok', '38 GB of disk space free', None)]
    add('start', screens.start())
    add('start-host', screens.start('host'))
    add('machine', screens.machine('yourself', found), *[r[1] for r in found])
    no_docker = ('Docker is not installed here.', 'Install Docker and Docker Compose, then run ./setup again.')
    add('machine-stop', screens.machine('yourself', [], stop=no_docker), *no_docker)
    aios_rows = [('ok', 'Found AIOStreams 2.37.0 in the container "aiostreams"', None),
                 ('ok', f'Viewers reach it at {PUBLIC}', 'subsync will join its Docker network: aiostreams_default')]
    add('aios', screens.aios(aios_rows), *[r[1] for r in aios_rows], aios_rows[1][2])
    missing = [('warn', 'The container "aiostreams" looks like AIOStreams, but I can\'t reach it from here.', None)]
    add('aios-missing', screens.aios(missing, ask='AIOS_URL', value='http://127.0.0.1:3000'), missing[0][1],
        'http://127.0.0.1:3000')
    problem = ('AIOS_URL', 'Nothing answers at http://127.0.0.1:3001. Check the address and that AIOStreams runs.')
    add('aios-problem', screens.aios(missing, ask='AIOS_URL', value='http://127.0.0.1:3001', problem=problem),
        missing[0][1], problem[1], 'http://127.0.0.1:3001')
    add('aios-several', screens.aios([], ask='AIOS_CONTAINER', value='aiostreams',
                                     containers=[('aiostreams', 'aiostreams  (ghcr.io/viren070/aiostreams:latest)'),
                                                 ('aiostreams-test', 'aiostreams-test  (ghcr.io/viren070/aiostreams:2.37)')]),
        'aiostreams', 'aiostreams-test', 'aiostreams  (ghcr.io/viren070/aiostreams:latest)',
        'aiostreams-test  (ghcr.io/viren070/aiostreams:2.37)')
    add('config', screens.config([], '1a2b3c4d-1234-4abc-8def-0123456789ab'), '1a2b3c4d-1234-4abc-8def-0123456789ab')
    config_rows = [('ok', 'Config found', None), ('ok', 'Debrid services: TorBox, Real-Debrid', None),
                   ('ok', 'Subtitle sources: OpenSubtitles V3+', None),
                   ('ok', 'Real-Debrid is never used: subsync reads through TorBox only', None)]
    add('config-found', screens.config(config_rows, '1a2b3c4d-1234-4abc-8def-0123456789ab'),
        '1a2b3c4d-1234-4abc-8def-0123456789ab', *[r[1] for r in config_rows])
    wrong = ('AIOS_CONFIG_PASSWORD', "The config ID and password don't match.")
    add('config-wrong-password', screens.config([], '1a2b3c4d-1234-4abc-8def-0123456789ab', problem=wrong),
        '1a2b3c4d-1234-4abc-8def-0123456789ab', wrong[1])
    rd = [('ok', 'Config found', None), ('ok', 'Debrid services: Real-Debrid', None)]
    add('config-rd-only', screens.config(rd, 'x', stop=(realdebrid.REAL_DEBRID_ONLY, engine.RD_WAY_OUT_PAGE)),
        'x', *[r[1] for r in rd], realdebrid.REAL_DEBRID_ONLY, engine.RD_WAY_OUT_PAGE)
    add('choices', screens.choices(['eng'], False, f'{PUBLIC}/subsync'), f'{PUBLIC}/subsync')
    add('choices-four', screens.choices(['eng', 'spa', 'rus', 'fre'], True, f'{PUBLIC}/subsync',
                                        offer_opensubtitles=True), f'{PUBLIC}/subsync')
    lang_problem = ('LANGUAGES', '"Klingon" is not a language AIOSubtitles knows.')
    add('choices-problem', screens.choices(['eng'], False, f'{PUBLIC}/subsync', problem=lang_problem),
        f'{PUBLIC}/subsync', lang_problem[1])
    add('limits', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50'}))
    add('limits-audio', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'AUDIO_BUDGET': '1'}, audio=True))
    add('limits-problem', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '0'},
                                         problem=('SUBSYNC_DAILY_BUDGET_GB', 'A number above 0, like 50.')),
        'A number above 0, like 50.')
    sections = [('Your config 1a2b3c4d…', [('+', 'adds 1 subtitle source: subsync (synced copies of the subtitles you '
                                                 'already get)')],
                 'Nothing else in it changes. A copy of it as it is now is saved first.'),
                ("subsync's own config (new)", [], 'a copy of yours without Real-Debrid and subsync, and without Usenet '
                                                   'unless you turn it on, with nothing running in the background, so '
                                                   'subsync can find the file you are watching. It never appears in your '
                                                   'apps.')]
    words = [w for heading, lines, after in sections for w in [heading, after] + [line for _, line in lines]] + ['+']
    add('review', screens.review(sections), *words)
    add('review-sign-in', screens.review(sections, cache=True, sign_in=True), *words)
    # Usenet: the question, its stop, its number, the third config, a Usenet-only config, the check line
    add('choices-usenet', screens.choices(['eng'], False, f'{PUBLIC}/subsync', usenet=True), f'{PUBLIC}/subsync')
    no_usenet = (engine.NO_USENET, engine.NO_USENET_WAY_OUT_PAGE)
    add('choices-usenet-stop', screens.choices(['eng'], False, f'{PUBLIC}/subsync', usenet=False, stop=no_usenet),
        f'{PUBLIC}/subsync', *no_usenet)
    add('limits-usenet', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50',
                                                     'SUBSYNC_USENET_LISTINGS_PER_DAY': '10'}, usenet=True))
    usenet_section = ("subsync's Usenet config (new)", [], 'only your Usenet searches and services, with nothing running '
                      'in the background, so subsync can find a file you play from Usenet when your debrid service '
                      "doesn't have it. It never appears in your apps.")
    add('review-usenet', screens.review(sections + [usenet_section]), *words, usenet_section[2])
    add('review-usenet-only', screens.review([sections[0], usenet_section]), *words, usenet_section[2])
    usenet_checks = [('ok', f'Your apps can reach subsync at {PUBLIC}/subsync', None),
                                     ('ok', 'AIOStreams asks subsync for subtitles', None),
                                     ('ok', "subsync's Usenet config answers (no search made, to spare your indexer's "
                                            "daily calls)", None)]
    add('check-usenet', screens.check('yourself', usenet_checks, running=False), *[r[1] for r in usenet_checks[:2]])
    add('check-usenet-warned', screens.check('yourself', [('warn', "subsync's Usenet config doesn't answer. Run "
                                                                   "./setup again, or send the output of ./setup "
                                                                   "--status with a bug report.", None)],
                                             running=False))
    progress = [('done', "Created subsync's own config (5e6f7a8b…)", None),
                ('now', 'Building subsync (the first time takes a few minutes) ...', None)]
    add('install-running', screens.installing('yourself', progress, running=True), *[r[1] for r in progress])
    finished = [('done', "Created subsync's own config (5e6f7a8b…)", None),
                ('ok', 'Built (its own tests passed during the build)', None),
                ('ok', 'subsync is running (it restarts by itself after a reboot)', None),
                ('ok', 'Your config now lists subsync', None)]
    add('install-done', screens.installing('yourself', finished, running=False), *[r[1] for r in finished])
    failed = ('Building subsync failed.', 'error: no space left on device\n\nRun ./setup again after fixing the problem above.')
    add('install-failed', screens.installing('yourself', progress[:1], running=False, stop=failed),
        progress[0][1], *failed)
    rule = hosting.web_server_rule(ADDRESS, '127.0.0.1:8790')
    intro = (f'Your web server needs one rule so {ADDRESS} reaches AIOSubtitles on this machine. Add it with the https '
             'certificate your web server has for that name, and reload the web server:')
    checks = [('ok', f'Your apps can reach subsync at {PUBLIC}/subsync', None),
              ('ok', 'AIOStreams asks subsync for subtitles', None),
              ('ok', "subsync's config finds 12 ready debrid files for Interstellar (a search only)", None)]
    add('check-running', screens.check('yourself', checks[:1], running=True), checks[0][1])
    add('check', screens.check('yourself', checks, running=False, offer_test_read=True), *[r[1] for r in checks])
    warned = [('warn', f"{PUBLIC}/subsync/health doesn't answer from this machine.",
               'If you just added the web server rule, reload the web server; some routers also block a machine from '
               'reaching its own public address, so check it from your phone too: it should say "ok".')]
    add('check-warned', screens.check('yourself', warned, running=False), warned[0][1], warned[0][2])
    title, lines, after = 'Done', ['subsync is running on this machine and is in your AIOStreams config.',
                                   'AIOStreams\' own Jellyfin server: the same, shown as "(synced)".'], \
        [('p', 'Your apps keep the same links: nothing to change in them.'), ('code', 'docker restart aiometadata')]
    add('done', screens.done('yourself', title, lines, after), title, *lines, *[t for _, t in after])
    # hosting
    add('host-machine', screens.machine('host', found), *[r[1] for r in found])
    add('host-address', screens.host_address(ADDRESS), ADDRESS, 'gateway')
    bad_address = ('HOST_ADDRESS', hosting.ADDRESS_WORDS)
    add('host-address-problem', screens.host_address('http://subs.example.com', problem=bad_address),
        'http://subs.example.com', 'gateway', hosting.ADDRESS_WORDS)
    add('host-signups', screens.host_signups('k7pm-2qra-9xwd'), 'k7pm-2qra-9xwd')
    add('host-choices', screens.host_choices())
    add('host-choices-usenet', screens.host_choices(usenet=True))
    add('host-limits', screens.limits('host', {}))
    add('host-limits-usenet', screens.limits('host', {}, usenet=True))
    add('host-limits-problem', screens.limits('host', {'SUBSYNC_SYNCS_AT_ONCE': 'many'},
                                              problem=('SUBSYNC_SYNCS_AT_ONCE', 'A whole number from 1, or auto.')),
        'A whole number from 1, or auto.')
    host_progress = [('done', 'Made the secret key and kept it in state/subsync.env, readable only by your user.', None),
                     ('ok', 'AIOSubtitles is running (it restarts by itself after a reboot)', None)]
    add('host-install', screens.installing('host', host_progress, running=False, rule=rule, rule_intro=intro),
        *[r[1] for r in host_progress], intro, *rule, *[line for lines in rule.values() for line in lines])
    host_checks = [('ok', f'People can reach AIOSubtitles at {ADDRESS}', None),
                   ('ok', f'The sign-up page loads at {ADDRESS}, with its security headers', None)]
    add('host-check', screens.check('host', host_checks, running=False), *[r[1] for r in host_checks])
    host_lines = [f'AIOSubtitles is running on this machine as a public instance at {ADDRESS}.',
                  'Share the address and the invite code with the people you want to use it: each of them signs up '
                  'there with their own AIOStreams link.']
    add('host-done', screens.done('host', 'Done', host_lines, []), 'Done', *host_lines)
    # after setup
    status = [('ok', 'container aiosubtitles-subsync: running healthy', None),
              ('ok', 'subtitle requests since start: 128; waiting jobs: 0', 'read today: 34.2 MB')]
    details = [('AIOStreams', PUBLIC), ('languages', 'eng, spa'), ('audio', 'off'), ('config', '1a2b3c4d…'),
               ("subsync's config", '5e6f7a8b…'), ('logs', 'docker logs aiosubtitles-subsync')]
    shown = [r[1] for r in status] + ['read today: 34.2 MB'] + [v for _, v in details]
    ready = update.Ready('1.2.0', 'git', None)
    add('after', screens.after_setup('yourself', status, details, '1.1.0', ready, offer_opensubtitles=True,
                                     config='1a2b3c4d…'),
        *shown, update.RUNNING.format(version='1.1.0'), update.READY.format(version='1.2.0'),
        'Password of your config 1a2b3c4d…')
    for how in ('newest', 'not-git'):
        add(f'after-{how}', screens.after_setup('yourself', status, details, '1.1.0', update.Ready(None, how, None),
                                                config='1a2b3c4d…'),
            *shown, update.RUNNING.format(version='1.1.0'), 'Password of your config 1a2b3c4d…')
    look_failed = update.LOOK_FAILED.format(reason='Could not resolve host: github.com')
    add('after-error', screens.after_setup('yourself', status, details, '1.1.0',
                                           update.Ready(None, 'error', 'Could not resolve host: github.com'),
                                           config='1a2b3c4d…'),
        *shown, update.RUNNING.format(version='1.1.0'), look_failed, 'Password of your config 1a2b3c4d…')
    host_status = [('ok', 'container aiosubtitles-subsync: running healthy', None),
                   ('ok', 'people signed up: 3 of 200', 'read today: 0.0 GB of 50 GB')]
    add('after-host', screens.after_setup('host', host_status, [('logs', 'docker logs aiosubtitles-subsync')], '1.1.0',
                                          ready),
        *[r[1] for r in host_status], 'read today: 0.0 GB of 50 GB', 'docker logs aiosubtitles-subsync',
        update.RUNNING.format(version='1.1.0'), update.READY.format(version='1.2.0'))
    updating = [('done', update.FETCHING.format(version='1.2.0'), None), ('now', update.RESTARTING, None)]
    add('updating', screens.working('Updates', updating, running=True), *[r[1] for r in updating])
    gone = update.FAILED.format(reason='Building subsync failed', version='1.1.0')
    add('update-failed', screens.working('Updates', updating[:1], running=False, stop=(gone, None)), updating[0][1], gone)
    gone_rows = [('ok', 'Your config no longer lists subsync (copy before: state/backups/viewer-1a2b3c4d-x.json)', None),
                 ('ok', "Deleted subsync's own config", None), ('ok', 'Removed the subsync container and its stored copies', None),
                 ('ok', 'Done.', None)]
    stays = ['the subsync image (about 1 GB): docker image rm aiosubtitles/subsync:1.1.0',
             'this folder, /home/me/aiosubtitles, with copies of your config in state/backups/ (./setup --restore FILE puts one back)']
    add('removed', screens.removed('Removing subsync', gone_rows, stays), *[r[1] for r in gone_rows], *stays)
    add('ended', screens.ended())
    return out
