"""The hosting route: a public instance of AIOSubtitles on this machine.

Setup writes public mode's lines of state/subsync.env, makes the service's secret once and never shows it, and
runs subsync on 127.0.0.1 only: the hoster's own web server sends their https address to it; setup shows the one rule
that needs and checks the address before it says done. No Wyzie Subs key (public mode refuses it); the audio route
only when the host chooses it, and then each person chooses on their settings page when it starts, with
its limits asked next.
"""
import secrets
import urllib.error
import urllib.parse
import urllib.request

from installer import limits, plan

LIMITS = limits.hosting(audio=True)
PORT = plan.SUBSYNC_PORT
# the audio route's question on this route: allow it or remove it completely; its explanation is
# explain.QUESTIONS['SUBSYNC_AUDIO_ROUTE'], under the setting's own name as the answers file has it
AUDIO_QUESTION = 'Offer syncing files that have no subtitles of their own, from their audio?'
# asked when no TMDB key is given for everyone: let each person add their own (explain.SETTINGS['SUBSYNC_TMDB_PERSONAL'])
TMDB_PERSONAL_QUESTION = 'Let each person add their own TMDB key?'
ADDRESS_WORDS = 'The web address of your public instance, with https, like https://subtitles.example.com.'   # the address question's own words
# the lines of state/subsync.env this route writes; every other line there is kept as the hoster wrote it
OWNED = ('SUBSYNC_PUBLIC', 'SUBSYNC_PUBLIC_BASE', 'SUBSYNC_SECRET_KEY', 'SUBSYNC_PUBLIC_INVITE', 'SUBSYNC_TRUSTED_PROXY',
         'SUBSYNC_SUBDL_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_USENET', 'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_TMDB_KEY',
         'SUBSYNC_TMDB_PERSONAL') \
    + limits.names(LIMITS)
_INVITE_LETTERS = 'abcdefghjkmnpqrstuvwxyz23456789'     # no 0/o, 1/l/i: easy to read out and type


def new_secret():
    """32 random bytes as base64url, as subsync's SUBSYNC_SECRET_KEY wants."""
    return secrets.token_urlsafe(32)


def suggested_invite():
    return '-'.join(''.join(secrets.choice(_INVITE_LETTERS) for _ in range(4)) for _ in range(3))


def check_address(value):
    """(good, message, value) for the web address question: https and a whole host (public mode's pages and links
    start at the address's root), written the same way every time."""
    text = (value or '').strip()
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != 'https' or not parts.hostname or not plan.has_usable_port(parts) or parts.path not in ('', '/') \
            or parts.query or parts.fragment or parts.username or parts.password:
        return False, ADDRESS_WORDS, text
    return True, '', f'https://{parts.netloc.lower()}'


def public_env(answers, secret, limit_values):
    """Public mode's lines: the address, the secret, the invite code, the trusted proxy, the source keys and every
    limit that differs from subsync's default. Empty values are left out (subsync's default is empty)."""
    env = {
        'SUBSYNC_PUBLIC': 'on',
        'SUBSYNC_PUBLIC_BASE': answers['HOST_ADDRESS'],
        'SUBSYNC_SECRET_KEY': secret,
        'SUBSYNC_PUBLIC_INVITE': answers.get('SUBSYNC_PUBLIC_INVITE', ''),
        'SUBSYNC_TRUSTED_PROXY': answers.get('SUBSYNC_TRUSTED_PROXY') or 'gateway',
        'SUBSYNC_SUBDL_KEY': answers.get('SUBDL_KEY', ''),
        'SUBSYNC_SUBSOURCE_KEY': answers.get('SUBSYNC_SUBSOURCE_KEY', ''),
        # the original-language lookup's TMDB key for everyone; without one, whether each person may add their own
        'SUBSYNC_TMDB_KEY': answers.get('TMDB_KEY', ''),
        'SUBSYNC_TMDB_PERSONAL': 'on' if not answers.get('TMDB_KEY')
        and str(answers.get('SUBSYNC_TMDB_PERSONAL', '')).lower() in ('yes', 'on', 'true') else 'off',
        # whether people may turn Usenet on for themselves (with their own services, through their AIOStreams)
        'SUBSYNC_USENET': 'on' if str(answers.get('SUBSYNC_USENET', '')).lower() in ('yes', 'on', 'true') else 'off',
        # whether files with no subtitle track of their own may be synced from their sound, as each person chooses
        'SUBSYNC_AUDIO_ROUTE': 'on' if str(answers.get('SUBSYNC_AUDIO_ROUTE', '')).lower() in ('yes', 'on', 'true')
        else 'off',
    }
    usenet, audio = env['SUBSYNC_USENET'] == 'on', env['SUBSYNC_AUDIO_ROUTE'] == 'on'
    env.update(limits.env_lines(limits.hosting(usenet, audio), limit_values))
    return {k: v for k, v in env.items() if v not in (None, '')}


def compose_yaml(image, install_id, port, local=False):
    """subsync alone, listening on 127.0.0.1 only, on no AIOStreams network."""
    return plan.compose_yaml({'image': image, 'local': local, 'install_id': install_id, 'host_network': False,
                              'networks': [], 'bind': '127.0.0.1', 'port': port})


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
    """(status, headers, body) of one GET from this machine. OSError when nothing answers."""
    request = urllib.request.Request(url, headers={'User-Agent': 'AIOSubtitles-setup'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read(4096)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), b''


def check(address, out):
    """The address answering from this machine, and the sign-up page there with its security headers. True when both
    hold."""
    good = True
    try:
        status, _, body = fetch(address + '/health')
    except OSError:
        status, body = 0, b''
    if status == 200 and body.strip() == b'ok':
        out.ok(f'People can reach AIOSubtitles at {address}')
    else:
        good = False
        out.warn(f"{address}/health doesn't answer from this machine.")
        out.note('If you just added the web server rule, reload the web server; some routers also block a machine')
        out.note('from reaching its own public address, so check it from your phone too: it should say "ok".')
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
