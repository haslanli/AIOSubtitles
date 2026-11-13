"""The setup page's typeface before subsync's image exists.

The image build fetches the two fonts and their licence, each checked by a pinned SHA-256 (subsync/Dockerfile); the
setup page runs before that build, so ./setup fetches the same three files once, with the same sums, into
state/fonts/. Nothing else is fetched. Without them (offline, a file that changed) the page uses the system font and
works the same. A test keeps this list equal to the Dockerfile's.
"""
import hashlib
import os
import urllib.request
from pathlib import Path

_BASE = 'https://raw.githubusercontent.com/google/fonts/35e77c3854970a550d9894a85aba21420813a019/ofl/'
FONTS = [   # (file name, address, sha256): as subsync/Dockerfile's ADD --checksum lines
    ('AtkinsonHyperlegibleNext-wght.ttf', _BASE + 'atkinsonhyperlegiblenext/AtkinsonHyperlegibleNext%5Bwght%5D.ttf',
     '5a455d1cfa099b601ab70751bb9673e8fe1854dc4500c80e1a220d0d75e31745'),
    ('AtkinsonHyperlegibleMono-wght.ttf', _BASE + 'atkinsonhyperlegiblemono/AtkinsonHyperlegibleMono%5Bwght%5D.ttf',
     '5ce8b1698d1ded7dff2178c1a3ad159470085a58ea239e8b2cb88f4fb4a6f646'),
    ('OFL.txt', _BASE + 'atkinsonhyperlegiblenext/OFL.txt',
     'aca6a428580965d2297d1b718042dd427c2a9443ece3b0d02d758e161e0c4030'),
]
LIMIT = 1024 * 1024          # each file is well under 1 MB


def _download(url, timeout=20):
    request = urllib.request.Request(url, headers={'User-Agent': 'AIOSubtitles-setup'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(LIMIT + 1)


def _right(path, digest):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def ensure(state, fetch=_download):
    """state/fonts with every file in place and right, fetching what is missing; None when one can't be had."""
    folder = Path(state) / 'fonts'
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    for name, url, digest in FONTS:
        path = folder / name
        if _right(path, digest):
            continue
        try:
            body = fetch(url, timeout=20)
        except (OSError, ValueError):
            return None
        if len(body) > LIMIT or hashlib.sha256(body).hexdigest() != digest:
            path.unlink(missing_ok=True)
            return None
        tmp = path.with_name(name + '.tmp')
        tmp.write_bytes(body)
        os.chmod(tmp, 0o644)
        tmp.replace(path)
    return folder
