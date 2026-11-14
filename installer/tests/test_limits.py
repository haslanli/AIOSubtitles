"""The limits each install asks, their defaults from
subsync's explanations, the check of a typed value, and how they reach state/subsync.env."""
import unittest

from installer import limits, plan
from subsync import explain

HOSTING_ORDER = [
    ('Each person', ['SUBSYNC_USER_FILES_PER_DAY', 'SUBSYNC_USER_DAILY_MB', 'SUBSYNC_MAX_LANGUAGES',
                     'SUBSYNC_USENET_LISTINGS_PER_DAY']),   # the last one only when Usenet is allowed
    ('The whole service', ['SUBSYNC_DAILY_BUDGET_GB', 'SUBSYNC_MAX_USERS', 'SUBSYNC_SIGNUPS_PER_HOUR']),
    ('This server', ['SUBSYNC_SYNCS_AT_ONCE', 'SUBSYNC_ACTIVE_WORKERS', 'SUBSYNC_MAX_CONNECTIONS']),
]
LABELS = {   # the approved labels
    'SUBSYNC_DAILY_BUDGET_GB': 'Data a day in total', 'SUBSYNC_AUDIO_BUDGET_GB': 'Audio per file',
    'SUBSYNC_MAX_LANGUAGES': 'Languages per person', 'SUBSYNC_USER_FILES_PER_DAY': 'New files per person a day',
    'SUBSYNC_USER_DAILY_MB': 'Data per person a day', 'SUBSYNC_MAX_USERS': 'People signed up',
    'SUBSYNC_SIGNUPS_PER_HOUR': 'Sign-up attempts per address an hour', 'SUBSYNC_SYNCS_AT_ONCE': 'Files synced at a time',
    'SUBSYNC_ACTIVE_WORKERS': 'Workers at once', 'SUBSYNC_MAX_CONNECTIONS': 'Requests at once',
    'SUBSYNC_USENET_LISTINGS_PER_DAY': 'Usenet searches per person a day',   # personal: 'Usenet searches a day'
}


class Routes(unittest.TestCase):
    def test_a_personal_install_asks_the_daily_total_and_with_audio_the_audio_limit(self):
        self.assertEqual([lim.setting for lim in limits.personal(audio=False)], ['SUBSYNC_DAILY_BUDGET_GB'])
        self.assertEqual([lim.setting for lim in limits.personal(audio=True)],
                         ['SUBSYNC_DAILY_BUDGET_GB', 'SUBSYNC_AUDIO_BUDGET_GB'])
        self.assertEqual([lim.label for lim in limits.personal(audio=False, usenet=True)],
                         ['Data a day in total', 'Usenet searches a day'])
        self.assertNotIn('SUBSYNC_USENET_LISTINGS_PER_DAY', limits.names(limits.hosting(usenet=False)))

    def test_a_public_instance_asks_every_limit_in_three_groups_without_audio(self):
        self.assertEqual([(heading, [lim.setting for lim in group]) for heading, group in limits.HOSTING],
                         HOSTING_ORDER)

    def test_every_limit_has_its_approved_label_and_subsyncs_default(self):
        for lim in limits.personal(audio=True) + [lim for _, group in limits.HOSTING for lim in group]:
            with self.subTest(setting=lim.setting):
                self.assertEqual(lim.label, LABELS[lim.setting])
                self.assertEqual(lim.default, explain.SETTINGS[lim.setting][0])

    def test_units_and_kinds(self):
        by = {lim.setting: lim for _, group in limits.HOSTING for lim in group}
        by.update({lim.setting: lim for lim in limits.personal(audio=True)})
        self.assertEqual((by['SUBSYNC_DAILY_BUDGET_GB'].unit, by['SUBSYNC_DAILY_BUDGET_GB'].kind), ('GB', 'decimal'))
        self.assertEqual((by['SUBSYNC_AUDIO_BUDGET_GB'].kind, by['SUBSYNC_AUDIO_BUDGET_GB'].options),
                         ('segmented', ('0.5', '1', '2', '4')))
        self.assertEqual((by['SUBSYNC_MAX_LANGUAGES'].kind, by['SUBSYNC_MAX_LANGUAGES'].options),
                         ('segmented', ('1', '2', '3', '4')))
        self.assertEqual((by['SUBSYNC_USER_FILES_PER_DAY'].unit, by['SUBSYNC_USER_DAILY_MB'].unit), ('files', 'MB'))
        self.assertEqual(by['SUBSYNC_SYNCS_AT_ONCE'].kind, 'auto')
        self.assertIsNone(by['SUBSYNC_MAX_USERS'].unit)


class Checks(unittest.TestCase):
    def lim(self, setting):
        return next(lim for _, group in limits.HOSTING for lim in group if lim.setting == setting) \
            if setting != 'SUBSYNC_AUDIO_BUDGET_GB' else limits.personal(audio=True)[1]

    def test_whole_numbers(self):
        lim = self.lim('SUBSYNC_MAX_USERS')
        self.assertEqual(limits.check(lim, ' 300 '), ('300', None))
        self.assertEqual(limits.check(lim, '1,000'), ('1000', None))
        for bad in ('0', '-1', '1.5', 'lots', '', 'auto', '1,0,0'):
            with self.subTest(bad=bad):
                self.assertEqual(limits.check(lim, bad), (None, 'A whole number from 1, like 200.'))

    def test_a_number_or_auto(self):
        lim = self.lim('SUBSYNC_SYNCS_AT_ONCE')
        self.assertEqual(limits.check(lim, 'Auto'), ('auto', None))
        self.assertEqual(limits.check(lim, '3'), ('3', None))
        for bad in ('0', '-1', '1.5', 'lots', ''):
            with self.subTest(bad=bad):
                self.assertEqual(limits.check(lim, bad), (None, 'A whole number from 1, or auto.'))

    def test_a_number_above_zero(self):
        lim = self.lim('SUBSYNC_DAILY_BUDGET_GB')
        self.assertEqual(limits.check(lim, '50'), ('50', None))
        self.assertEqual(limits.check(lim, '0.5'), ('0.5', None))
        self.assertEqual(limits.check(lim, '20.0'), ('20', None))
        for bad in ('0', '-1', 'lots', '', 'auto', 'inf', 'nan'):
            with self.subTest(bad=bad):
                self.assertEqual(limits.check(lim, bad), (None, 'A number above 0, like 50.'))

    def test_the_segmented_choices(self):
        audio, langs = self.lim('SUBSYNC_AUDIO_BUDGET_GB'), self.lim('SUBSYNC_MAX_LANGUAGES')
        self.assertEqual(limits.check(audio, '2'), ('2', None))
        self.assertEqual(limits.check(audio, '3'), (None, 'Type 0.5, 1, 2 or 4 (GB per file).'))
        self.assertEqual(limits.check(audio, '3', current='3'), ('3', None))   # set by hand before: kept
        self.assertEqual(limits.check(langs, '3'), ('3', None))
        self.assertEqual(limits.check(langs, '5'), (None, 'SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to 4.'))

    def test_the_value_shown_has_thousands_marked(self):
        self.assertEqual(limits.shown(self.lim('SUBSYNC_USER_DAILY_MB'), '1000'), '1,000')
        self.assertEqual(limits.shown(self.lim('SUBSYNC_DAILY_BUDGET_GB'), '50'), '50')
        self.assertEqual(limits.shown(self.lim('SUBSYNC_SYNCS_AT_ONCE'), 'auto'), 'auto')


class InTheEnvFile(unittest.TestCase):
    def test_a_value_set_by_hand_is_the_current_value(self):
        lim = self.daily = limits.personal(audio=False)[0]
        self.assertEqual(limits.current(lim, {}), '50')
        self.assertEqual(limits.current(lim, {'SUBSYNC_DAILY_BUDGET_GB': '20'}), '20')

    def test_only_values_other_than_the_default_are_written(self):
        asked = limits.personal(audio=False)
        self.assertEqual(limits.env_lines(asked, {'SUBSYNC_DAILY_BUDGET_GB': '50'}), {})
        self.assertEqual(limits.env_lines(asked, {'SUBSYNC_DAILY_BUDGET_GB': '20'}), {'SUBSYNC_DAILY_BUDGET_GB': '20'})

    def test_an_asked_limit_replaces_its_hand_set_line_and_other_lines_stay(self):
        asked = limits.personal(audio=False)
        current = {'SUBSYNC_DAILY_BUDGET_GB': '20', 'SUBSYNC_MAX_TRIES': '8', 'SUBSYNC_LANGUAGES': 'eng'}
        written = {'SUBSYNC_LANGUAGES': 'spa'}
        merged, kept = plan.merge_env({**written, **limits.env_lines(asked, {'SUBSYNC_DAILY_BUDGET_GB': '50'})},
                                      current, owned=limits.names(asked))
        self.assertEqual(merged, {'SUBSYNC_LANGUAGES': 'spa', 'SUBSYNC_MAX_TRIES': '8'})
        self.assertEqual(kept, ['SUBSYNC_MAX_TRIES'])


if __name__ == '__main__':
    unittest.main()
