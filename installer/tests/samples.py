"""Every screen and state of the setup page with made-up values, for the tests and the screen checks. The rows are the
engine's own words with sample values. every_state() returns {name: (html, the values put in)}: the values are the only
words a screen may show besides the approved ones."""
import functools
import io
import json
import types
import urllib.error

from installer import dockerenv, engine, hosting, limits, plan, screens, update
from installer.aios import Aios, AiosError
from subsync import explain, realdebrid

ADDRESS = 'https://subs.example.com'
PUBLIC = 'https://aio.example.com'
CONFIG_ID = '1a2b3c4d-1234-4abc-8def-0123456789ab'


class Offline:
    """An opener that answers every request itself, so none is made: with `status` and AIOStreams' JSON `body`, or,
    without a status, as an address nothing answers at (the connection refused, with Linux's errno)."""

    def __init__(self, status=None, body=None):
        self.status, self.body = status, body

    def open(self, request, timeout=None):
        if self.status is None:
            raise urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused'))
        raise urllib.error.HTTPError(request.full_url, self.status, 'answered offline', {},
                                     io.BytesIO(json.dumps(self.body).encode()))


def aios_error(address, call, answer):
    """The AIOStreams client's own error for `call(client)` when its requests get `answer`: what setup's checks show.
    Both of its openers answer here (calls that carry a password have their own), so no request leaves this machine."""
    aios = Aios(address)
    aios.opener = aios.keyed_opener = answer
    try:
        call(aios)
    except AiosError as e:
        return e


@functools.cache
def every_state():
    """Built once (rendering every screen takes a while, and the tests ask for it again and again), so read only."""
    out = {}

    def add(name, html, *values):
        out[name] = (html, frozenset(values))

    found = [('ok', 'Docker and Docker Compose work', None), ('ok', '38 GB of disk space free', None)]
    add('start', screens.start())
    add('start-host', screens.start('host'))
    add('machine', screens.machine('yourself', found), *[r[1] for r in found])
    no_docker = dockerenv.NO_DOCKER
    add('machine-stop', screens.machine('yourself', [], stop=no_docker), *no_docker)
    aios_rows = [('ok', 'Found AIOStreams 2.37.0 in the container "aiostreams"', None),
                 ('ok', f'Your apps reach it at {PUBLIC}', 'subsync will join its Docker network: aiostreams_default')]
    add('aios', screens.aios(aios_rows), *[r[1] for r in aios_rows], aios_rows[1][2])
    missing = [('warn', 'The container "aiostreams" looks like AIOStreams, but setup can\'t reach it from here.', None)]
    add('aios-missing', screens.aios(missing, ask='AIOS_URL', value='http://127.0.0.1:3000'), missing[0][1],
        'http://127.0.0.1:3000')
    # the address check's problem outside setup's container (engine.Engine._check_aios): the client's words and fix
    refused = aios_error('http://127.0.0.1:3001', Aios.status, Offline())
    problem = ('AIOS_URL', f'{refused.message} {refused.fix}')
    add('aios-problem', screens.aios(missing, ask='AIOS_URL', value='http://127.0.0.1:3001', problem=problem),
        missing[0][1], problem[1], 'http://127.0.0.1:3001')
    add('aios-several', screens.aios([], ask='AIOS_CONTAINER', value='aiostreams',
                                     containers=[('aiostreams', 'ghcr.io/viren070/aiostreams:latest'),
                                                 ('aiostreams-test', 'ghcr.io/viren070/aiostreams:2.37')]),
        'aiostreams', 'aiostreams-test', 'ghcr.io/viren070/aiostreams:latest', 'ghcr.io/viren070/aiostreams:2.37')
    add('config', screens.config([], CONFIG_ID), CONFIG_ID)
    config_rows = [('ok', 'Config found', None), ('ok', 'Debrid services: TorBox, Real-Debrid', None),
                   ('ok', 'Subtitle sources: OpenSubtitles V3+', None),
                   ('ok', 'Real-Debrid is never used: subsync reads through TorBox only', None)]
    add('config-found', screens.config(config_rows, CONFIG_ID), CONFIG_ID, *[r[1] for r in config_rows])
    # the password check's problem (engine.Engine.read_viewer): the client's words for AIOStreams' refusal
    refusal = Offline(401, {'error': {'code': 'USER_INVALID_PASSWORD'}})
    wrong = ('AIOS_CONFIG_PASSWORD', aios_error(PUBLIC, lambda aios: aios.get_config(CONFIG_ID, 'x'), refusal).message)
    add('config-wrong-password', screens.config([], CONFIG_ID, problem=wrong), CONFIG_ID, wrong[1])
    rd = [('ok', 'Config found', None), ('ok', 'Debrid services: Real-Debrid', None)]
    add('config-rd-only', screens.config(rd, 'x', stop=(realdebrid.REAL_DEBRID_ONLY, engine.RD_WAY_OUT_PAGE)),
        'x', *[r[1] for r in rd], realdebrid.REAL_DEBRID_ONLY, engine.RD_WAY_OUT_PAGE)
    add('choices', screens.choices(['eng'], 'no', f'{PUBLIC}/subsync'), f'{PUBLIC}/subsync')
    add('choices-audio-open', screens.choices(['eng'], 'open', f'{PUBLIC}/subsync'), f'{PUBLIC}/subsync')
    add('choices-four', screens.choices(['eng', 'spa', 'rus', 'fre'], 'pick', f'{PUBLIC}/subsync',
                                        offer_opensubtitles=True), f'{PUBLIC}/subsync')
    lang_problem = ('LANGUAGES', plan.parse_languages('Klingon')[1])
    add('choices-problem', screens.choices(['eng'], 'no', f'{PUBLIC}/subsync', problem=lang_problem),
        f'{PUBLIC}/subsync', lang_problem[1])
    add('choices-saved-keys', screens.choices(['eng'], 'no', f'{PUBLIC}/subsync', saved={'SUBDL_KEY', 'TMDB_KEY'}),
        f'{PUBLIC}/subsync')
    # Docker in a virtual machine, AIOStreams' address local: no address suggested, the note under the box
    add('choices-vm', screens.choices(['eng'], 'no', None, address_note=screens.VM_ADDRESS))
    add('limits', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50'}))
    add('limits-audio', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50', limits.AUDIO.setting: '2'},
                                       audio=True))
    daily = ('SUBSYNC_DAILY_BUDGET_GB', limits.check(limits.DAILY, '0')[1])
    add('limits-problem', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '0'}, problem=daily), daily[1])
    add('limits-stopped', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50'}, stop=screens.UNEXPECTED))
    audio_limit = (limits.AUDIO.setting, limits.check(limits.AUDIO, '3')[1])                    # web.py's key for it
    add('limits-audio-problem', screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': '50', limits.AUDIO.setting: '3'},
                                               problem=audio_limit, audio=True))
    sections = [('Your config 1a2b3c4d…', [('+', 'adds 1 subtitle source: subsync (synced copies of the subtitles you '
                                                 'already get)')],
                 'Nothing else in it changes. A copy of it as it is now is saved first.'),
                ("subsync's own config (new)", [], 'a copy of yours without Real-Debrid, Usenet and subsync, with '
                                                   'nothing running in the background, so subsync can find the file '
                                                   'you are watching. It never appears in your apps.')]
    words = [w for heading, lines, after in sections for w in [heading, after] + [line for _, line in lines]] + ['+']
    add('review', screens.review(sections), *words)
    # another config than the last install's: the earlier one named under yours, a paragraph of its own
    earlier = engine.EARLIER_CONFIG.format(old='9c8d7e6f…')
    changed = [(sections[0][0], sections[0][1], f'{sections[0][2]}\n{earlier}')] + sections[1:]
    add('review-earlier-config', screens.review(changed), *words, earlier)
    add('review-sign-in', screens.review(sections, cache=True, sign_in=True), *words)
    asks = [('warn', 'Creating the sync config: This AIOStreams asks you to sign in before configs can be saved.', None)]
    add('review-sign-in-to-save', screens.review(sections, sign_in=True, rows=asks), *words, asks[0][1])
    limited = ('Creating the sync config: AIOStreams is limiting requests from this machine (it allows 10 new configs '
               'an hour); try again later.', None)
    add('review-stopped', screens.review(sections, cache=True, username='admin', rows=[('ok', 'Signed in', None)],
                                         stop=limited), *words, 'Signed in', limited[0])
    # Usenet: the question, its stop, its number, the third config, a Usenet-only config, the check line
    add('choices-usenet', screens.choices(['eng'], 'no', f'{PUBLIC}/subsync', usenet=True), f'{PUBLIC}/subsync')
    no_usenet = (engine.NO_USENET, engine.NO_USENET_WAY_OUT_PAGE)
    add('choices-usenet-stop', screens.choices(['eng'], 'no', f'{PUBLIC}/subsync', usenet=False, stop=no_usenet),
        f'{PUBLIC}/subsync', *no_usenet)
    # the step's own lines: why Usenet stays off (answered yes before, no service of the config reads it any more)
    why = [('note', explain.SETUP_USENET_WHY['proxy'], None)]     # setup's own reason
    not_an_address = ('PUBLIC_ADDRESS', plan.check_public('ftp://x')[1])
    add('choices-usenet-why', screens.choices(['eng'], 'no', 'ftp://x', problem=not_an_address, rows=why),
        'ftp://x', not_an_address[1], why[0][1])
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
                ('now', engine.STARTING.format(name='subsync'), None)]
    add('install-running', screens.installing('yourself', progress, running=True), *[r[1] for r in progress])
    finished = [('done', "Created subsync's own config (5e6f7a8b…)", None),
                ('ok', 'subsync is running (it restarts by itself after a reboot)', None),
                ('ok', 'Your config now lists subsync', None)]
    add('install-done', screens.installing('yourself', finished, running=False), *[r[1] for r in finished])
    failed = ('Starting subsync failed.', 'Error response from daemon: driver failed programming external connectivity')
    add('install-failed', screens.installing('yourself', progress[:1], running=False, stop=failed),
        progress[0][1], *failed)
    add('install-unexpected', screens.installing('yourself', progress[:1], running=False, stop=screens.UNEXPECTED),
        progress[0][1])
    # what saving the configs warned, over the install's progress
    cache_warnings = [
        ('warn', "The setting is fixed by AIOStreams' environment (SUBTITLE_CACHE_TTL); change it there:",
         'SUBTITLE_CACHE_TTL=*:300,aiosubtitles-subsync:-1'),
        ('warn', 'That account is not an admin; the setting stays as it is.', None)]
    add('install-review-warned', screens.installing('yourself', finished, running=False, warnings=cache_warnings),
        *[r[1] for r in finished], *cache_warnings[0][1:], cache_warnings[1][1])
    personal_rule = plan.proxy_snippets(f'{PUBLIC}/subsync', '127.0.0.1:8790')
    personal_intro = (f'Your web server needs one rule so {PUBLIC}/subsync/... reaches subsync. Add it next to your '
                      "AIOStreams rule (keep the path as it is; don't strip it):")
    rule_note = ("Web server running in Docker on AIOStreams' network: use aiosubtitles-subsync:8790 instead of "
                 '127.0.0.1:8790.')
    add('install-rule-note', screens.installing('yourself', finished, running=False, rule=personal_rule,
                                                rule_intro=personal_intro, rule_note=rule_note),
        *[r[1] for r in finished], personal_intro, *personal_rule, rule_note)
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
    unreached = [('bad', 'AIOStreams did not reach subsync.', 'It calls http://aiosubtitles-subsync:8790/…; check that '
                                                               'address works from AIOStreams.')]
    add('done-warned', screens.done('yourself', engine.WARNED, lines, after, warnings=unreached), engine.WARNED,
        *lines, *[t for _, t in after], *unreached[0][1:])
    # hosting
    add('host-machine', screens.machine('host', found), *[r[1] for r in found])
    add('host-address', screens.host_address(ADDRESS), ADDRESS, 'gateway')
    bad_address = ('HOST_ADDRESS', hosting.ADDRESS_REFUSED)
    add('host-address-problem', screens.host_address('http://subs.example.com', problem=bad_address),
        'http://subs.example.com', 'gateway', hosting.ADDRESS_REFUSED)
    add('host-signups', screens.host_signups('k7pm-2qra-9xwd'), 'k7pm-2qra-9xwd')
    add('host-signups-stopped', screens.host_signups('k7pm-2qra-9xwd', stop=screens.UNEXPECTED), 'k7pm-2qra-9xwd')
    refused_invite = ('SUBSYNC_PUBLIC_INVITE', hosting.check_invite('join$us')[1])
    add('host-signups-problem', screens.host_signups('join$us', problem=refused_invite), 'join$us', refused_invite[1])
    add('host-choices', screens.host_choices())
    add('host-choices-stopped', screens.host_choices(stop=screens.UNEXPECTED))
    add('host-choices-usenet', screens.host_choices(usenet=True))
    add('host-choices-audio', screens.host_choices(usenet=True, audio=True))
    add('host-choices-saved-keys', screens.host_choices(saved={'SUBDL_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'TMDB_KEY'}))
    add('host-limits', screens.limits('host', {}))
    add('host-limits-usenet', screens.limits('host', {}, usenet=True))
    add('host-limits-audio', screens.limits('host', {}, usenet=True, audio=True))
    places = ('SUBSYNC_AUDIO_AT_ONCE', limits.check(limits.AUDIO_AT_ONCE, '5', places=4)[1])   # 4 files at a time
    add('host-limits-audio-problem', screens.limits('host', {'SUBSYNC_AUDIO_AT_ONCE': '5'}, problem=places, audio=True),
        '5', places[1])
    syncs = next(lim for lim in limits.hosting() if lim.setting == 'SUBSYNC_SYNCS_AT_ONCE')
    at_once = ('SUBSYNC_SYNCS_AT_ONCE', limits.check(syncs, 'many')[1])
    add('host-limits-problem', screens.limits('host', {'SUBSYNC_SYNCS_AT_ONCE': 'many'}, problem=at_once),
        at_once[1])
    add('host-limits-stopped', screens.limits('host', {}, stop=screens.UNEXPECTED))
    # a value set by hand in state/subsync.env that no button offers: the engine refuses it in the terminal's words
    per_person = next(lim for lim in limits.hosting() if lim.setting == 'SUBSYNC_MAX_LANGUAGES')
    languages = ('SUBSYNC_MAX_LANGUAGES', limits.check(per_person, '6')[1])
    add('host-limits-languages-problem', screens.limits('host', {'SUBSYNC_MAX_LANGUAGES': '6'}, problem=languages))
    host_progress = [('done', 'Made the secret key and kept it in state/subsync.env, readable only by your user.', None),
                     ('ok', 'AIOSubtitles is running (it restarts by itself after a reboot)', None)]
    add('host-install', screens.installing('host', host_progress, running=False, rule=rule, rule_intro=intro),
        *[r[1] for r in host_progress], intro, *rule, *[line for lines in rule.values() for line in lines])
    host_checks = [('ok', f'People can reach AIOSubtitles at {ADDRESS}', None),
                   ('ok', f'The sign-up page loads at {ADDRESS}, with its security headers', None)]
    add('host-check', screens.check('host', host_checks, running=False), *[r[1] for r in host_checks])
    host_lines = [f'AIOSubtitles is running on this machine as a public instance at {ADDRESS}.',
                  'Share the address and the invite code, k7pm-2qra-9xwd, with the people you want to use it: each of '
                  'them signs up there with their own AIOStreams link.']
    add('host-done', screens.done('host', 'Done', host_lines, []), 'Done', *host_lines)
    # after setup
    status = [('ok', 'container aiosubtitles-subsync: running healthy', None),
              ('ok', 'subtitle requests since start: 128; waiting jobs: 0', 'read today: 34.2 MB')]
    details = [('AIOStreams', PUBLIC), ('languages', 'eng, spa'), ('audio', 'off'), ('config', '1a2b3c4d…'),
               ("subsync's config", '5e6f7a8b…'), ('logs', 'docker logs aiosubtitles-subsync')]
    shown = [r[1] for r in status] + ['read today: 34.2 MB'] + [v for _, v in details]
    ready = update.Ready('1.2.0', 'ready')
    add('after', screens.after_setup('yourself', status, details, '1.1.0', ready, offer_opensubtitles=True,
                                     config='1a2b3c4d…'),
        *shown, update.RUNNING.format(version='1.1.0'), update.READY.format(version='1.2.0'),
        'Password of your config 1a2b3c4d…')
    add('after-newest', screens.after_setup('yourself', status, details, '1.1.0', update.Ready('1.1.0', 'newest'),
                                            config='1a2b3c4d…'),
        *shown, update.RUNNING.format(version='1.1.0'), 'Password of your config 1a2b3c4d…')
    newer_here = update.NEWER_HERE.format(running='1.2.0', setup='1.1.0')
    add('after-newer-here', screens.after_setup('yourself', status, details, '1.2.0', update.Ready('1.1.0', 'newer-here'),
                                                config='1a2b3c4d…', rollback=('1.1.0', None)),
        *shown, update.RUNNING.format(version='1.2.0'), newer_here, update.ROLLBACK.format(version='1.1.0'),
        'Password of your config 1a2b3c4d…')
    restore = update.RESTORE_QUESTION.format(version='1.1.0', name='databases-20261113-000000')
    add('after-rollback', screens.after_setup('yourself', status, details, '1.1.0', update.Ready('1.1.0', 'newest'),
                                              config='1a2b3c4d…', rollback=('1.0.0', 'databases-20261113-000000')),
        *shown, update.RUNNING.format(version='1.1.0'), update.ROLLBACK.format(version='1.0.0'), restore,
        'Password of your config 1a2b3c4d…')
    host_status = [('ok', 'container aiosubtitles-subsync: running healthy', None),
                   ('ok', 'people signed up: 3 of 200', 'read today: 0.0 GB of 50 GB')]
    add('after-host', screens.after_setup('host', host_status, [('logs', 'docker logs aiosubtitles-subsync')], '1.1.0',
                                          ready),
        *[r[1] for r in host_status], 'read today: 0.0 GB of 50 GB', 'docker logs aiosubtitles-subsync',
        update.RUNNING.format(version='1.1.0'), update.READY.format(version='1.2.0'))
    add('after-host-not-ticked', screens.after_setup('host', host_status, [('logs', 'docker logs aiosubtitles-subsync')],
                                                     '1.1.0', ready, problem=('REMOVE', screens.TICK_TO_REMOVE)),
        *[r[1] for r in host_status], 'read today: 0.0 GB of 50 GB', 'docker logs aiosubtitles-subsync',
        update.RUNNING.format(version='1.1.0'), update.READY.format(version='1.2.0'))
    add('after-usenet', screens.after_setup('yourself', status, details, '1.1.0', update.Ready('1.1.0', 'newest'),
                                            config='1a2b3c4d…', removing=screens.remove_text({'SYNC_UUID': 'x',
                                                                                              'USENET_UUID': 'y'})),
        *shown, update.RUNNING.format(version='1.1.0'), 'Password of your config 1a2b3c4d…')
    saving = [('warn', 'Saving the config: This AIOStreams asks you to sign in before configs can be saved.', None)]
    add('after-sign-in', screens.after_setup('yourself', status, details, '1.1.0', update.Ready('1.1.0', 'newest'),
                                             problem=('AIOS_USERNAME', ''), config='1a2b3c4d…', sign_in=True,
                                             username='admin', written=saving),
        *shown, update.RUNNING.format(version='1.1.0'), 'Password of your config 1a2b3c4d…', saving[0][1])
    saved = update.SAVED_DATABASES.format(path='state/backups/databases-20261114-061500')
    updating = [('ok', saved, None), ('done', update.CHECKING.format(version='1.2.0', old='1.1.0'), None),
                ('ok', update.CHECKED, None), ('now', update.SWITCHING.format(version='1.2.0'), None)]
    add('updating', screens.working('Updates', updating, running=True), *[r[1] for r in updating])
    switched = [('ok', saved, None), ('ok', update.CHECKED, None),
                ('ok', update.SWITCHED.format(version='1.2.0'),
                 update.KEPT.format(version='1.1.0') + ' ' + update.OLD_IMAGES.format(
                     image='3320f59e4ec8')),
                ('ok', f'Your apps can reach subsync at {PUBLIC}/subsync', None)]
    add('updated', screens.working('Updates', switched, running=False), *[r[1] for r in switched], switched[2][2])
    gone = update.FAILED.format(reason='no answer within 90 seconds', version='1.1.0')
    add('update-failed', screens.working('Updates', updating[:2], running=False, stop=(gone, None)),
        *[r[1] for r in updating[:2]], gone)
    back = update.ROLLED_BACK.format(new='1.2.0', old='1.1.0') + update.WITH_DATABASES
    add('update-rolled-back', screens.working('Updates', updating[:3], running=False, stop=(back, None)),
        *[r[1] for r in updating[:3]], back)
    gone_rows = [('ok', 'Your config no longer lists subsync (copy before: state/backups/viewer-1a2b3c4d-x.json)', None),
                 ('ok', "Deleted subsync's own config", None), ('ok', 'Removed the subsync container and its stored copies', None),
                 ('ok', 'Done.', None)]
    stays = ['the subsync image (about 1 GB): docker image rm 3320f59e4ec8',
             'this folder, /home/me/aiosubtitles, with copies of your config in state/backups/ (./setup --restore FILE puts one back)']
    add('removed', screens.removed('Removing subsync', gone_rows, stays), *[r[1] for r in gone_rows], *stays)
    timeout = (engine.REMOVE_FAILED, 'Error response from daemon: timeout')
    add('removing-failed', screens.working('Removing subsync', gone_rows[:2], running=False, stop=timeout),
        *[r[1] for r in gone_rows[:2]], *timeout)
    host_gone = [('ok', 'Removed the subsync container and its stored copies', None), ('ok', 'Done.', None)]
    host_stays = ['the subsync image (about 1 GB): docker image rm 3320f59e4ec8',
                  f'the rule in your web server for {ADDRESS}: remove it there and reload the web server',
                  'this folder, /home/me/aiosubtitles, with the database copies made before updates in state/backups/, '
                  "which hold everyone's records"]
    add('removed-host', screens.removed('Removing AIOSubtitles', host_gone, host_stays), *[r[1] for r in host_gone],
        *host_stays)
    add('ended', screens.ended())
    add('stopped-unexpectedly', screens.working(screens.TITLE, [], False, stop=screens.UNEXPECTED))   # web.App.handle
    return types.MappingProxyType(out)
