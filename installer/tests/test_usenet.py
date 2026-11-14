"""./setup with Usenet: the question, the Usenet config, its number, a Usenet-only config, a
re-run that turns it off, uninstall; and the hosting route's question and per-person number."""
import unittest

from installer import engine, hosting, limits
from installer.tests.test_flow import PASSWORD, PUBLIC, UUID, FlowFixture
from subsync import explain

TORBOX = {'id': 'torbox', 'enabled': True, 'credentials': {'apiKey': 'tb'}}
ENGINE = {'id': 'aiostreams', 'enabled': True, 'credentials': {'aiostreamsAuth': 'a:b'}}
ZILEAN = {'type': 'zilean', 'instanceId': 'zil', 'enabled': True, 'options': {'name': 'Zilean'}}
NEWZNAB = {'type': 'newznab', 'instanceId': 'nzb', 'enabled': True, 'options': {'name': 'Geek'}}
PROWLARR = {'type': 'prowlarr', 'instanceId': 'prw', 'enabled': True, 'options': {'name': 'Prowlarr'}}
SUBS = {'type': 'opensubtitles', 'instanceId': 'ost', 'enabled': True,
        'options': {'name': 'OpenSubtitles', 'resources': ['subtitles']}}


class Personal(FlowFixture):
    def viewer(self, services, presets):
        self.aios.users[UUID]['config'] = {'services': services, 'presets': presets, 'failover': {'enabled': True}}

    def keys(self):
        return engine.read_kv(self.state / 'secrets.env')

    def env(self):
        return engine.read_kv(self.state / 'subsync.env')

    def test_usenet_on_makes_the_usenet_config(self):
        self.viewer([TORBOX, ENGINE], [ZILEAN, NEWZNAB, PROWLARR, SUBS])
        code, out = self.run_setup(dict(self.ANSWERS, USENET='yes', SUBSYNC_USENET_LISTINGS_PER_DAY='3'))
        self.assertEqual(code, 0, out)
        keys, env = self.keys(), self.env()
        usenet = self.aios.users[keys['USENET_UUID']]['config']
        self.assertEqual([p['type'] for p in usenet['presets']], ['newznab', 'prowlarr'])
        self.assertEqual([s['id'] for s in usenet['services']], ['torbox', 'aiostreams'])
        sync = self.aios.users[keys['SYNC_UUID']]['config']
        self.assertEqual([p['options'].get('sources') for p in sync['presets'] if p['type'] == 'prowlarr'],
                         [['torrent']])                                   # Prowlarr: torrent indexers only
        self.assertEqual((env['SUBSYNC_USENET'], env['SUBSYNC_USENET_LISTINGS_PER_DAY']), ('on', '3'))
        self.assertEqual(env['SUBSYNC_USENET_MANIFEST'], f"http://aiostreams:3000/stremio/{keys['USENET_UUID']}/"
                                                         f"ENC-{keys['USENET_UUID'][:4]}/manifest.json")
        self.assertEqual(env['SUBSYNC_USENET_PASSWORD'], keys['USENET_PASSWORD'])
        self.assertIn("subsync's Usenet config (new)", out)
        self.assertIn("subsync's Usenet config answers", out)
        listed = [p for m, p, _ in self.aios.log if m == 'GET' and f"/stremio/{keys['USENET_UUID']}/stream" in str(p)]
        self.assertEqual(listed, [])                                      # the self-test searches no indexer
        self.assertNotIn(keys['USENET_PASSWORD'], out)

        # a re-run with Usenet off: its config is removed, its lines go
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'USENET': 'no', 'TEST_READ': 'no'})
        self.assertEqual(code, 0, out)
        self.assertNotIn(keys['USENET_UUID'], self.aios.users)
        self.assertNotIn('USENET_UUID', self.keys())
        env = self.env()
        self.assertEqual(env['SUBSYNC_USENET'], 'off')
        self.assertNotIn('SUBSYNC_USENET_MANIFEST', env)
        self.assertNotIn('SUBSYNC_USENET_LISTINGS_PER_DAY', env)

    def test_the_question_only_with_a_readable_route(self):
        # a Newznab set to a NzbDAV that isn't served through AIOStreams' proxy: no route, no question
        nzbdav = {'id': 'nzbdav', 'enabled': True, 'credentials': {'url': 'http://nzbdav:3000'}}
        self.viewer([TORBOX, nzbdav], [ZILEAN, dict(NEWZNAB, options={'name': 'Geek', 'services': ['nzbdav']})])
        code, out = self.run_setup(dict(self.ANSWERS, USENET='yes'))
        self.assertEqual(code, 0, out)
        self.assertNotIn('USENET_UUID', self.keys())
        self.assertIn(explain.USENET_WHY['proxy'].split(': ', 1)[1][:40], out)
        self.assertEqual(self.env()['SUBSYNC_USENET'], 'off')

    def test_a_usenet_only_config(self):
        self.viewer([ENGINE], [NEWZNAB, SUBS])
        code, out = self.run_setup(dict(self.ANSWERS, USENET='no'))
        self.assertNotEqual(code, 0)
        self.assertIn('Answer yes to the Usenet question', out)
        self.assertEqual(len(self.aios.users), 1)                         # nothing made
        code, out = self.run_setup(dict(self.ANSWERS, USENET='yes'))
        self.assertEqual(code, 0, out)
        keys, env = self.keys(), self.env()
        self.assertNotIn('SYNC_UUID', keys)
        self.assertNotIn('SUBSYNC_SOURCE_MANIFEST', env)
        usenet = self.aios.users[keys['USENET_UUID']]['config']
        self.assertEqual([p['type'] for p in usenet['presets']], ['newznab', 'opensubtitles'])   # with its subtitles
        self.assertNotIn("subsync's own config", out.split('Your AIOStreams configs')[1].split('Save these')[0])
        self.assertFalse([c for c in self.exec_calls if 'subsync.selftest' in c[0]])     # no test read of Usenet

    def test_uninstall_removes_the_usenet_config(self):
        self.viewer([TORBOX, ENGINE], [ZILEAN, NEWZNAB, SUBS])
        code, out = self.run_setup(dict(self.ANSWERS, USENET='yes', TEST_READ='no'))
        self.assertEqual(code, 0, out)
        usenet_id = self.keys()['USENET_UUID']
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'}, '--uninstall')
        self.assertEqual(code, 0, out)
        self.assertNotIn(usenet_id, self.aios.users)


class Hosting(unittest.TestCase):
    def test_the_per_person_number_is_a_limit_of_its_own_group(self):
        each = dict(limits.HOSTING)['Each person']
        self.assertIn('SUBSYNC_USENET_LISTINGS_PER_DAY', [lim.setting for lim in each])
        self.assertEqual(limits.USENET.label, 'Usenet searches a day')
        self.assertEqual(limits.USENET_HOSTED.label, 'Usenet searches per person a day')
        self.assertEqual(limits.personal(audio=False, usenet=True), [limits.DAILY, limits.USENET])

    def test_public_env_writes_the_switch(self):
        answers = {'HOST_ADDRESS': 'https://subs.example.com', 'SUBSYNC_USENET': 'yes'}
        self.assertEqual(hosting.public_env(answers, 's' * 43, {})['SUBSYNC_USENET'], 'on')
        self.assertEqual(hosting.public_env(dict(answers, SUBSYNC_USENET='no'), 's' * 43, {})['SUBSYNC_USENET'], 'off')
        self.assertIn('SUBSYNC_USENET', hosting.OWNED)


if __name__ == '__main__':
    unittest.main()
