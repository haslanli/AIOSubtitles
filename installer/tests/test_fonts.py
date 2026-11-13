"""The setup page's typeface before the image exists: the files of the Dockerfile,
fetched once with its pinned SHA-256 into state/fonts/; without them the page uses the system font."""
import hashlib
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import fonts

REPO = Path(__file__).resolve().parent.parent.parent
ADD = re.compile(r'ADD --chmod=644 --checksum=sha256:([0-9a-f]{64}) \\\s*\n\s*(https://\S+) \\\s*\n\s*'
                 r'\./subsync/static/fonts/(\S+)')


def fake(contents):
    return [(name, f'https://example.invalid/{name}', hashlib.sha256(body).hexdigest()) for name, body in contents]


class Fonts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.bodies = {'A.ttf': b'font-a', 'B.ttf': b'font-b', 'OFL.txt': b'licence'}
        patcher = mock.patch.object(fonts, 'FONTS', fake(self.bodies.items()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetched = []

    def fetch(self, url, timeout=20):
        self.fetched.append(url)
        return self.bodies[url.rsplit('/', 1)[1]]

    def test_the_list_equals_the_dockerfiles(self):
        mock.patch.stopall()
        found = [(name, url, digest) for digest, url, name in ADD.findall((REPO / 'subsync' / 'Dockerfile').read_text())]
        self.assertEqual(sorted(found), sorted(fonts.FONTS))
        self.assertEqual({name for name, _, _ in fonts.FONTS},
                         {'AtkinsonHyperlegibleNext-wght.ttf', 'AtkinsonHyperlegibleMono-wght.ttf', 'OFL.txt'})

    def test_fetched_once_with_their_sums(self):
        folder = fonts.ensure(self.state, fetch=self.fetch)
        self.assertEqual(folder, self.state / 'fonts')
        self.assertEqual((folder / 'A.ttf').read_bytes(), b'font-a')
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(folder / 'A.ttf').st_mode), 0o644)
        self.assertEqual(len(self.fetched), 3)
        self.assertEqual(fonts.ensure(self.state, fetch=self.fetch), folder)
        self.assertEqual(len(self.fetched), 3)                         # present and right: not fetched again

    def test_a_wrong_sum_keeps_nothing(self):
        self.bodies['B.ttf'] = b'something else'
        with mock.patch.object(fonts, 'FONTS', fake([('A.ttf', b'font-a'), ('B.ttf', b'font-b'),
                                                     ('OFL.txt', b'licence')])):
            self.assertIsNone(fonts.ensure(self.state, fetch=self.fetch))
        self.assertFalse((self.state / 'fonts' / 'B.ttf').exists())

    def test_offline_is_the_system_font(self):
        def offline(url, timeout=20):
            raise OSError('no network')
        self.assertIsNone(fonts.ensure(self.state, fetch=offline))

    def test_a_damaged_copy_is_fetched_again(self):
        folder = fonts.ensure(self.state, fetch=self.fetch)
        (folder / 'A.ttf').write_bytes(b'broken')
        fonts.ensure(self.state, fetch=self.fetch)
        self.assertEqual((folder / 'A.ttf').read_bytes(), b'font-a')


if __name__ == '__main__':
    unittest.main()
