"""The few AIOStreams API calls ./setup makes: status, read and save a config, create the sync config, sign in, and
the subtitle cache setting. Errors come back as plain sentences that say how to fix them."""
import base64
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from subsync import realdebrid

UA = 'AIOSubtitles/1.0 (setup)'   # starts with "AIO": stock AIOStreams then includes file details in stream listings


class AiosError(Exception):
    def __init__(self, message, fix=None, code=None):
        super().__init__(message)
        self.message, self.fix, self.code = message, fix, code


def _plain(code, message):
    """AIOStreams error codes, in plain words."""
    code = str(code or '')
    if code in ('USER_INVALID_DETAILS', 'USER_INVALID_PASSWORD', 'USER_NOT_FOUND'):
        return 'That config ID and password don\'t match a config on this AIOStreams.'
    if code == 'USER_INVALID_CONFIG':
        return f'AIOStreams refused the config: {message}'
    if code in ('FORBIDDEN', 'UNAUTHORIZED') or 'not allowed' in (message or '').lower():
        return 'This AIOStreams asks you to sign in before configs can be saved.'
    if code == 'RATE_LIMIT_EXCEEDED' or 'too many' in (message or '').lower():
        return 'AIOStreams is limiting requests from this machine (it allows 10 new configs an hour); try again later.'
    return message or code or 'AIOStreams gave an error without a message.'


class Aios:
    def __init__(self, base, timeout=30):
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), realdebrid.GuardedRedirects)
        self.signed_in = False

    def call(self, method, path, body=None, auth=None, timeout=None, raw=False):
        """One request. A short "too many requests" (stock AIOStreams allows 5 config API calls per 5 seconds) is
        waited out and retried; a long one (10 new configs per hour) is reported."""
        for attempt in range(6):
            status, payload, wait = self._call(method, path, body, auth, timeout, raw)
            if status != 429 or wait is None or wait > 60 or attempt == 5:
                return status, payload
            if attempt == 0:
                print(f'  ..  AIOStreams asks to slow down; waiting {wait} s', file=sys.stderr, flush=True)
            time.sleep(wait)
        return status, payload

    def _call(self, method, path, body, auth, timeout, raw):
        headers = {'User-Agent': UA, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
        url = path if path.startswith('http') else self.base + path
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            realdebrid.check(url)
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                payload = r.read()
                return r.status, payload if raw else (json.loads(payload) if payload[:1] in (b'{', b'[') else {}), None
        except urllib.error.HTTPError as e:
            payload = e.read() or b''
            wait = None
            if e.code == 429:
                header = e.headers.get('Retry-After') or e.headers.get('RateLimit-Reset') or ''
                wait = int(header) if header.strip().isdigit() else 6
            try:
                return e.code, json.loads(payload), wait
            except ValueError:
                return e.code, {'error': {'code': 'RATE_LIMIT_EXCEEDED' if e.code == 429 else None,
                                          'message': payload[:300].decode('utf-8', 'replace')}}, wait
        except realdebrid.Blocked:
            raise AiosError('Stopped a request to Real-Debrid: AIOSubtitles never uses Real-Debrid.',
                            'Give the address of your AIOStreams, not of Real-Debrid.') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AiosError(f'Can\'t reach AIOStreams at {self.base} ({getattr(e, "reason", e)}).',
                            'Check the address, and that AIOStreams is running (docker ps).') from None

    @staticmethod
    def _error(status, body, doing):
        err = (body or {}).get('error') or {}
        if isinstance(err, str):
            err = {'message': err}
        message = _plain(err.get('code'), err.get('message') or (body or {}).get('detail'))
        return AiosError(f'{doing}: {message}', code=err.get('code') or status)

    # ------------------------------------------------------------------------------------------- reads

    def status(self):
        status, body = self.call('GET', '/api/v1/status', timeout=15)
        data = (body or {}).get('data') if isinstance(body, dict) else None
        if status != 200 or not data or 'version' not in data:
            raise AiosError(f'{self.base} answered, but not like AIOStreams (HTTP {status}).',
                            'Give the address you open AIOStreams\' configure page at.')
        return data

    def config_exists(self, uuid):
        """Whether a config with this ID exists, asked without its password (stock AIOStreams answers HEAD
        /api/v1/user?uuid= with 200 or 400). None when this AIOStreams can't tell (another answer)."""
        status, _ = self.call('HEAD', '/api/v1/user?' + urllib.parse.urlencode({'uuid': uuid}), timeout=15, raw=True)
        return {200: True, 400: False}.get(status)

    def get_config(self, uuid, password):
        status, body = self.call('GET', '/api/v1/user', auth=(uuid, password))
        if status != 200 or not (body or {}).get('success'):
            raise self._error(status, body, 'Reading the config')
        data = body['data']
        return data['userData'], data['encryptedPassword']

    # ------------------------------------------------------------------------------------------- writes

    def save_config(self, uuid, password, config):
        status, body = self.call('PUT', '/api/v1/user', {'config': config}, auth=(uuid, password), timeout=90)
        if status != 200 or not (body or {}).get('success'):
            raise self._error(status, body, 'Saving the config')

    def create_config(self, config, password):
        status, body = self.call('POST', '/api/v1/user', {'config': config, 'password': password}, timeout=90)
        if status not in (200, 201) or not (body or {}).get('success'):
            raise self._error(status, body, 'Creating the sync config')
        return body['data']['uuid'], body['data']['encryptedPassword']

    def delete_config(self, uuid, password):
        status, body = self.call('DELETE', '/api/v1/user', auth=(uuid, password))
        if status != 200 or not (body or {}).get('success'):
            raise self._error(status, body, 'Removing the sync config')

    def sign_in(self, username, password):
        """AIOStreams' own sign-in (its web page's login). Returns whether that account is an admin."""
        status, body = self.call('POST', '/api/v1/auth/login', {'username': username, 'password': password})
        if status != 200:
            raise self._error(status, body, 'Signing in')
        self.signed_in = True
        return bool(((body or {}).get('data') or {}).get('isAdmin', False))

    def subtitle_cache_setting(self):
        """(key, value, locked) for the per-addon subtitle cache, or None if this AIOStreams has no such setting."""
        status, body = self.call('GET', '/api/v1/dashboard/settings')
        if status != 200:
            raise self._error(status, body, 'Reading AIOStreams\' settings')
        data = (body or {}).get('data') or {}
        items = data.get('settings') or data.get('keys') or data
        if isinstance(items, dict):
            items = [dict(v, key=k) if isinstance(v, dict) else {'key': k, 'value': v} for k, v in items.items()]
        for item in items if isinstance(items, list) else []:
            key = str(item.get('key', ''))
            if item.get('env') == 'SUBTITLE_CACHE_TTL' or key.endswith('subtitle.ttl'):
                return key, item.get('value'), item.get('source') == 'environment'
        return None

    def set_setting(self, key, value):
        status, body = self.call('PATCH', '/api/v1/dashboard/settings', {key: value})
        if status != 200 or (isinstance(body, dict) and body.get('success') is False):
            raise self._error(status, body, 'Changing the subtitle cache setting')

    # ------------------------------------------------------------------------------------------- addon routes

    def addon_get(self, manifest_base, path, timeout=60):
        status, body = self.call('GET', manifest_base + path, timeout=timeout)
        if status != 200:
            raise AiosError(f'AIOStreams answered HTTP {status} for {path.split("/")[1]}.')
        return body
