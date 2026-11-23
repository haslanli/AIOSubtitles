"""The hosting route: a public instance of AIOSubtitles on this machine.

Setup writes public mode's lines of state/subsync.env, makes the service's secret once and never shows it, and
runs subsync on 127.0.0.1 only: the hoster's own web server sends their https address to it; setup shows the one rule
that needs and checks the address before it says done. No Wyzie Subs key (public mode refuses it); the audio route
only when the host chooses it, and then each person chooses on their settings page when it starts, with
its limits asked next.
"""
import http.client
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from installer import limits, plan
from subsync import realdebrid
from subsync.settings import unknown_proxies
from subsync.version import user_agent

LIMITS = limits.hosting(audio=True)
PORT = plan.SUBSYNC_PORT
# the audio route's question on this route: allow it or remove it completely; its explanation is
# explain.QUESTIONS['SUBSYNC_AUDIO_ROUTE'], under the setting's own name as the answers file has it
AUDIO_QUESTION = 'Offer syncing files that have no subtitles of their own, from their audio?'
# asked when no TMDB key is given for everyone: let each person add their own (explain.SETTINGS['SUBSYNC_TMDB_PERSONAL'])
TMDB_PERSONAL_QUESTION = 'Let each person add their own TMDB key?'
# a refused web address: what it must be (the personal route takes a path; public mode's pages start at the root)
ADDRESS_REFUSED = ('Type an https address with no path after the name, like https://subtitles.example.com: the pages '
                   'and links start at its root.')
# an address that doesn't answer from this machine, on both routes: the warning and the notes under it
NO_ANSWER = "{address}/health doesn't answer from this machine."
NO_ANSWER_NOTES = ('If you just added the web server rule, reload the web server; some routers also block a machine',
                   'from reaching its own public address, so check it from your phone too: it should say "ok".')
# setup's answer to SUBSYNC_TRUSTED_PROXY when none is given: the web server reaches the instance through the port
# Docker publishes, from its network's gateway
TRUSTED_PROXY = 'gateway'
# an answer with an entry subsync never matches (a network range, a name), so the sign-up limit would count every
# visitor as the web server's one address
TRUSTED_PROXY_REFUSED = "Type gateway or IP addresses separated by commas, like 172.18.0.1: {entry} isn't one."
# the lines of state/subsync.env this route writes; every other line there is kept as the hoster wrote it
OWNED = ('SUBSYNC_PUBLIC', 'SUBSYNC_PUBLIC_BASE', 'SUBSYNC_SECRET_KEY', 'SUBSYNC_PUBLIC_INVITE', 'SUBSYNC_TRUSTED_PROXY',
         'SUBSYNC_SUBDL_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_USENET', 'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_TMDB_KEY',
         'SUBSYNC_TMDB_PERSONAL') \
    + limits.names(LIMITS)
_INVITE_LETTERS = 'abcdefghjkmnpqrstuvwxyz23456789'     # no 0/o, 1/l/i: easy to read out and type
# The invite code's characters: setup writes state/subsync.env's values unquoted, where Docker Compose substitutes $name
# and cuts a value at " #", so a code with either would reach subsync changed and nobody could sign up with it
INVITE_CHARACTERS = re.compile(r'[\w .-]*')
INVITE_REFUSED = 'Letters, digits, spaces, - _ and . only, like k7mp-x4rt-9nwe.'


def new_secret():
    """32 random bytes as base64url, as subsync's SUBSYNC_SECRET_KEY wants."""
    return secrets.token_urlsafe(32)


def suggested_invite():
    return '-'.join(''.join(secrets.choice(_INVITE_LETTERS) for _ in range(4)) for _ in range(3))


def check_invite(value):
    """(good, message, value) for the invite code question: letters, digits, spaces, - _ and . only."""
    code = (value or '').strip()
    return (True, '', code) if INVITE_CHARACTERS.fullmatch(code) else (False, INVITE_REFUSED, code)


def check_trusted_proxy(value):
    """(good, message, value) for the trusted proxy question: gateway or IP addresses, as subsync takes them."""
    text = (value or '').strip()
    unknown = unknown_proxies(text)
    return (False, TRUSTED_PROXY_REFUSED.format(entry=unknown[0]), text) if unknown else (True, '', text)


def _named(host):
    """Whether `host` is a name the resolver can look up as written: IDNA refuses an empty label (as in
    subtitles..example.com) and one longer than 63 characters, and every request to it would stop there."""
    try:
        host.encode('idna')
    except UnicodeError:
        return False
    return True


def check_address(value):
    """(good, message, value) for the web address question: https and a whole host (public mode's pages and links
    start at the address's root), written the same way every time."""
    text = (value or '').strip()
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != 'https' or not parts.hostname or not _named(parts.hostname) or not plan.has_usable_port(parts) \
            or parts.path not in ('', '/') or parts.query or parts.fragment or parts.username or parts.password:
        return False, ADDRESS_REFUSED, text
    if plan.has_space(text):
        return False, plan.NO_SPACES, text
    return True, '', f'https://{parts.netloc.lower()}'


def public_env(answers, secret, limit_values, usenet=False, audio=False, tmdb_personal=False):
    """Public mode's lines: the address, the secret, the invite code, the trusted proxy, the source keys, the host's
    three choices (`usenet`, `audio`, `tmdb_personal`: True or False) and every limit that differs from subsync's
    default. Empty values are left out (subsync's default is empty)."""
    env = {
        'SUBSYNC_PUBLIC': 'on',
        'SUBSYNC_PUBLIC_BASE': answers['HOST_ADDRESS'],
        'SUBSYNC_SECRET_KEY': secret,
        'SUBSYNC_PUBLIC_INVITE': answers.get('SUBSYNC_PUBLIC_INVITE', ''),
        'SUBSYNC_TRUSTED_PROXY': answers.get('SUBSYNC_TRUSTED_PROXY') or TRUSTED_PROXY,
        'SUBSYNC_SUBDL_KEY': answers.get('SUBDL_KEY', ''),
        'SUBSYNC_SUBSOURCE_KEY': answers.get('SUBSYNC_SUBSOURCE_KEY', ''),
        # the original-language lookup's TMDB key for everyone; without one, whether each person may add their own
        'SUBSYNC_TMDB_KEY': answers.get('TMDB_KEY', ''),
        'SUBSYNC_TMDB_PERSONAL': 'on' if tmdb_personal and not answers.get('TMDB_KEY') else 'off',
        # whether people may turn Usenet on for themselves (with their own services, through their AIOStreams)
        'SUBSYNC_USENET': 'on' if usenet else 'off',
        # whether files with no subtitle track of their own may be synced from their sound, as each person chooses
        'SUBSYNC_AUDIO_ROUTE': 'on' if audio else 'off',
    }
    env.update(limits.env_lines(limits.hosting(bool(usenet), bool(audio)), limit_values))
    return {k: v for k, v in env.items() if v not in (None, '')}


def compose_yaml(image, install_id, port, local=False, external_volume=None):
    """subsync alone, listening on 127.0.0.1 only, on no AIOStreams network (`external_volume`: plan.compose_yaml)."""
    return plan.compose_yaml({'image': image, 'local': local, 'install_id': install_id, 'host_network': False,
                              'networks': [], 'bind': '127.0.0.1', 'port': port, 'external_volume': external_volume})


def web_server_rule(address, target):
    """What the hoster's web server needs so the whole address reaches subsync, with the visitor's address passed on
    (the sign-up limit counts it, SUBSYNC_TRUSTED_PROXY)."""
    host = urllib.parse.urlsplit(address).hostname
    return {
        'Caddy': [f'{host} {{', f'    reverse_proxy {target}', '}'],
        'nginx': ['location / {', f'    proxy_pass http://{target};',
                  '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;', '}'],
        'Traefik': [f'rule: Host(`{host}`)', f'service: http://{target}'],
    }


def fetch(url, timeout=10):
    """(status, headers, body) of one GET from this machine, through subsync's request gate (realdebrid.urlopen: never
    to Real-Debrid, nor on to it by a redirect). OSError when nothing answers with HTTP: no answer, a name that can't
    be looked up as written, or an answer that isn't HTTP."""
    try:
        request = urllib.request.Request(url, headers={'User-Agent': user_agent('setup')})
        with realdebrid.urlopen(request, timeout) as response:
            return response.status, dict(response.headers.items()), response.read(4096)
    except urllib.error.HTTPError as e:
        with e:             # its response, closed now rather than when it is collected
            return e.code, dict(e.headers.items()), b''
    except (ValueError, http.client.HTTPException) as e:     # IDNA's UnicodeError; BadStatusLine, IncompleteRead, ...
        raise OSError(' '.join(str(e).split()) or type(e).__name__) from e


def health_ok(address):
    """Whether subsync answers "ok" at `address`/health from this machine: the check of the address on both routes."""
    try:
        status, _, body = fetch(address + '/health')
    except OSError:      # nothing answers, or not with HTTP
        return False
    return status == 200 and body.strip() == b'ok'


def check(address, out):
    """The address answering from this machine, and the sign-up page there with its security headers. True when both
    hold."""
    good = True
    if health_ok(address):
        out.ok(f'People can reach AIOSubtitles at {address}')
    else:
        good = False
        out.warn(NO_ANSWER.format(address=address))
        for line in NO_ANSWER_NOTES:
            out.note(line)
    try:
        status, headers, _ = fetch(address + '/')
        headers = {k.lower(): v for k, v in headers.items()}
        reason = None if status == 200 and "default-src 'none'" in headers.get('content-security-policy', '') \
            else f'HTTP {status}'
    except OSError as e:
        reason = str(getattr(e, 'reason', None) or e)
    if reason is None:
        out.ok(f'The sign-up page loads at {address}, with its security headers')
    else:
        good = False
        out.bad(f"The sign-up page doesn't load at {address}: {reason}.")
    return good
