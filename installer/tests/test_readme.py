"""The README's own texts (its Settings section is test_explain's) say what setup and subsync do, in the words the owner
approved, and the numbers they give are the code's."""
import ast
import unittest
from pathlib import Path

from installer import engine, screens
from subsync import explain, relgate

REPO = Path(__file__).resolve().parent.parent.parent


def section(title, joined=True):
    """A '## title' section of the README; `joined`: its words joined by single spaces."""
    text = (REPO / 'README.md').read_text().split(f'\n## {title}\n', 1)[1].split('\n## ', 1)[0]
    return ' '.join(text.split()) if joined else text


def pct(share):
    return f'{round(share * 100)} %'


def constants(path):
    """A module's top-level NAME = literal values, read without importing it (audiofit needs numpy)."""
    out = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


class WhatYouNeed(unittest.TestCase):
    def test_aiostreams_elsewhere_usenet_and_a_public_instance_are_named(self):
        # it said AIOSubtitles goes on AIOStreams' machine and reads debrid files only
        need = section('What you need')
        self.assertIn('- **AIOStreams** (v2.37 or newer), usually in Docker on the machine AIOSubtitles goes on; one '
                      'elsewhere works too, at an address this machine can reach. A public instance needs no '
                      'AIOStreams of its own, but a web address with https that reaches this machine.', need)
        self.assertIn('- An AIOStreams config with a **debrid service other than Real-Debrid** (TorBox, AllDebrid, '
                      'Premiumize, ...), or a Usenet service subsync can read through (turned on in setup). subsync '
                      'never uses Real-Debrid (see [Fixed rules](#fixed-rules)).', need)
        self.assertNotIn('reads debrid files only', need)

    def test_the_disk_space_is_the_setup_pages(self):
        # it said 3 GB, the page 4 GB
        self.assertEqual(screens.DISK_GB, 4)
        self.assertIn(f'and about **{screens.DISK_GB} GB** of free disk space. Nothing else: setup runs inside the '
                      'AIOSubtitles image.', section('What you need'))
        self.assertIn(f'About {screens.DISK_GB} GB of free disk space.', screens.BEFORE)


class Install(unittest.TestCase):
    def test_the_guard_names_the_one_unmarked_volume_it_treats_as_its_own(self):
        # it said every item under setup's names carries the mark; an install by setup 1.3.0 or before keeps its
        # unmarked data volume, recorded in state/settings.json (R3-2)
        self.assertIn("Setup changes only what it made: before every change it checks that each container, network and "
                      "volume under its names carries its own install's mark, and stops without changing anything if "
                      "one doesn't. The one exception is the data volume of an install made by setup 1.3.0 or before: "
                      "Docker can't mark a volume after it is made, so setup keeps that volume's name in "
                      '`state/settings.json` and treats only that unmarked volume as its own.', section('Install'))
        taken_over = {'installed': True, 'install_id': 'a1b2c3d4', 'external_volume': 'subsync_data'}
        self.assertEqual(engine.unlabelled_own(taken_over), {('volume', 'subsync_data')})
        self.assertEqual(engine.unlabelled_own({'installed': True, 'install_id': 'a1b2c3d4'}), set())


class WhatItChanges(unittest.TestCase):
    def test_subsyncs_config_never_has_usenet(self):
        # it said "without Usenet unless you turn it on"; Usenet always gets the third config
        self.assertIn("- **A second config for subsync.** It is made from yours, without Real-Debrid, Usenet and "
                      'subsync, and nothing runs in it in the background. subsync uses it to find the file you are '
                      "watching. It never appears in your apps. With Usenet on, a third one, subsync's Usenet config, "
                      'holds only your Usenet searches and services (see [Fixed rules](#fixed-rules)); a config whose '
                      'only debrid service is Real-Debrid gets only this one.', section('What it changes'))


class WhatSubsyncReads(unittest.TestCase):
    def test_the_audio_limit_is_the_one_you_choose(self):
        # it gave the recommended 1 GB as a fixed maximum
        self.assertEqual(explain.SETTINGS['SUBSYNC_AUDIO_BUDGET_GB'][0], '1')
        self.assertIn("subsync then reads short parts of the file's audio (about 60–350 MB, at most the audio limit "
                      'you choose per file, 1 GB recommended) through your debrid service or Usenet',
                      section('What subsync reads'))

    def test_an_audio_sync_reads_beside_the_main_read(self):
        # it said one file at a time; the audio lane reads beside the main one
        self.assertEqual(explain.SETTINGS['SUBSYNC_DAILY_BUDGET_GB'][0], '100')
        self.assertIn('- **Limits:** subsync reads one file at a time, plus one audio sync beside it when the audio '
                      'route is on; it stops for a while if a service refuses, and has a daily limit (100 GB by '
                      'default).', section('What subsync reads'))


class HowTheSyncWorks(unittest.TestCase):
    def test_the_check_counts_the_lines_that_pair(self):
        # it said 90 % of all lines; the check counts the paired ones and needs a median too
        self.assertEqual(relgate.WINDOW, 1.0)                     # "within a second"
        snapped = float(explain.SETTINGS['SUBSYNC_SNAP_WITHIN'][0])
        self.assertIn(f'4. A copy is kept only if at least {pct(relgate.MIN_PAIRED)} of its lines pair with a line of '
                      "the file's track (start within a second of it), at least "
                      f'{pct(relgate.WITHIN_SHARE)} of those land within {relgate.WITHIN_MS} ms of it, and the typical '
                      f'(median) error is {relgate.MEDIAN_MS} ms or less, spread over the whole film. Copies whose '
                      f'lines snapped need {pct(snapped)}.', section('How the sync works'))

    def test_the_audio_route_gives_both_ways_to_pass(self):
        # it gave only the "clearly closer" way's numbers
        c = constants(REPO / 'subsync' / 'subsync' / 'audiofit.py')
        better = c['BETTER']
        self.assertIn(f"A copy is kept when, on those parts, its typical error is {c['GATE_MEDIAN_MS']} ms or less, at "
                      f"least {pct(c['GATE_WITHIN'])} of its lines that match speech start within {c['WITHIN_MS']} ms "
                      f"of it (and at least {pct(c['GATE_STRICT'])} of all its lines do), and at most "
                      f"{pct(c['GATE_MISS'])} match no speech. A copy that misses that is still kept when it is "
                      'clearly closer to the speech than the original (SUBSYNC_AUDIO_BETTER): its typical error is '
                      f"{better['median_ms']:.0f} ms or less, at least {pct(better['within'])} of its lines are within "
                      f"{c['WITHIN_MS']} ms of the speech, few lines are unmatched, and it moved the lines by at least "
                      'a second.', section('How the sync works'))
        self.assertEqual(better['min_shift_s'], 1.0)              # "at least a second"


class Folder(unittest.TestCase):
    def rows(self):
        """The table's rows: {path: what it holds}."""
        cells = [line.strip('| ').split(' | ', 1) for line in section('Folder', joined=False).splitlines()
                 if line.startswith('| `')]
        return {path.strip('`'): text for path, text in cells}

    def test_every_part_of_the_repository_has_its_row(self):
        # docs/, the workflows and VERSION had none
        rows = self.rows()
        self.assertEqual(rows['docs/'], '`answers.example`, a sample answers file for `./setup --answers FILE`, '
                                        'written by `installer/docs.py`.')
        self.assertEqual(rows['.github/workflows/'], "The image's build on GitHub: its tests on x86 and ARM, and "
                                                     'publishing to ghcr.io only after they pass.')
        self.assertEqual(rows['VERSION'], "This release's version number, which the image carries and updates "
                                          'compare.')
        self.assertEqual(sorted(p.name for p in (REPO / 'docs').iterdir()), ['answers.example'])
        for path in rows:
            if path != 'state/':                                  # made by setup, never in the repository
                self.assertTrue((REPO / path).exists(), path)

    def test_each_suites_command_is_the_one_the_image_build_runs(self):
        # subsync's had none (R3-1)
        rows = self.rows()
        self.assertEqual(rows['subsync/'], "The service, and the image's `Dockerfile` (built from this folder: "
                                           '`docker build -f subsync/Dockerfile .`). Its tests run with `python3 -m '
                                           'unittest discover -s subsync/tests -t subsync/tests`; the public-mode '
                                           'tests need the `cryptography` package and are skipped without it. The '
                                           'build runs both test suites and stops if any test fails.')
        build = (REPO / 'subsync' / 'Dockerfile').read_text()
        self.assertIn('set -- -s /src/subsync/tests -t /src/subsync/tests;', build)
        self.assertIn('set -- -s /src/installer/tests -t /src;', build)
        self.assertIn('`python3 -m unittest discover -s installer/tests -t .`', rows['installer/'])


if __name__ == '__main__':
    unittest.main()
