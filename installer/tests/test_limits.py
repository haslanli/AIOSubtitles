"""The limits each install asks, their defaults from
subsync's explanations, the check of a typed value, and how they reach state/subsync.env."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import engine, limits, plan
from subsync import explain, settings

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
    # with the audio route on
    'SUBSYNC_USER_AUDIO_PER_DAY': 'Audio syncs per person a day',
    'SUBSYNC_USER_OPENED_PER_DAY': 'Of those, as soon as a file is opened', 'SUBSYNC_AUDIO_AT_ONCE': 'Audio syncs at a time',
}


class Routes(unittest.TestCase):
    def test_a_personal_install_asks_the_daily_total_and_with_audio_the_audio_limit(self):
        self.assertEqual([lim.setting for lim in limits.personal(audio=False)], ['SUBSYNC_DAILY_BUDGET_GB'])
        self.assertEqual([lim.setting for lim in limits.personal(audio=True)],
                         ['SUBSYNC_DAILY_BUDGET_GB', 'SUBSYNC_AUDIO_BUDGET_GB'])
        self.assertEqual([lim.label for lim in limits.personal(audio=False, usenet=True)],
                         ['Data a day in total', 'Usenet searches a day'])
        self.assertNotIn('SUBSYNC_USENET_LISTINGS_PER_DAY', limits.names(limits.hosting(usenet=False)))

    def test_a_public_instance_asks_the_audio_limits_only_with_the_audio_route(self):
        # With the audio route on, the audio limit is asked under "Each person", before the Usenet number;
        # Then the two daily counts, and the audio syncs at a time under "This server"
        groups = dict(limits.hosting_groups(usenet=True, audio=True))
        self.assertEqual([lim.setting for lim in groups['Each person']],
                         ['SUBSYNC_USER_FILES_PER_DAY', 'SUBSYNC_USER_DAILY_MB', 'SUBSYNC_MAX_LANGUAGES',
                          'SUBSYNC_AUDIO_BUDGET_GB', 'SUBSYNC_USER_AUDIO_PER_DAY', 'SUBSYNC_USER_OPENED_PER_DAY',
                          'SUBSYNC_USENET_LISTINGS_PER_DAY'])
        self.assertEqual([lim.setting for lim in groups['This server']],
                         ['SUBSYNC_SYNCS_AT_ONCE', 'SUBSYNC_AUDIO_AT_ONCE', 'SUBSYNC_ACTIVE_WORKERS',
                          'SUBSYNC_MAX_CONNECTIONS'])
        audio_only = {'SUBSYNC_AUDIO_BUDGET_GB', 'SUBSYNC_USER_AUDIO_PER_DAY', 'SUBSYNC_USER_OPENED_PER_DAY',
                      'SUBSYNC_AUDIO_AT_ONCE'}
        for usenet in (True, False):
            self.assertFalse(audio_only & set(limits.names(limits.hosting(usenet=usenet))))
            self.assertLessEqual(audio_only, set(limits.names(limits.hosting(usenet=usenet, audio=True))))
        self.assertEqual({lim.setting: lim.default for lim in limits.hosting(audio=True)
                          if lim.setting in audio_only},
                         {'SUBSYNC_AUDIO_BUDGET_GB': '1', 'SUBSYNC_USER_AUDIO_PER_DAY': '20',
                          'SUBSYNC_USER_OPENED_PER_DAY': '5', 'SUBSYNC_AUDIO_AT_ONCE': 'auto'})

    def test_a_public_instance_asks_every_limit_in_three_groups_without_audio(self):
        self.assertEqual([(heading, [lim.setting for lim in group]) for heading, group in limits.HOSTING],
                         HOSTING_ORDER)

    def test_every_limit_has_its_approved_label_and_subsyncs_default(self):
        for lim in limits.personal(audio=True) + limits.hosting(audio=True):
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
        audio = {lim.setting: lim for lim in limits.hosting(audio=True)}
        self.assertEqual([audio[name].kind for name in ('SUBSYNC_USER_AUDIO_PER_DAY', 'SUBSYNC_USER_OPENED_PER_DAY',
                                                        'SUBSYNC_AUDIO_AT_ONCE')], ['count', 'count', 'auto'])


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
                self.assertEqual(limits.check(lim, bad), (None, 'A number above 0, like 100.'))

    def test_a_comma_in_a_gb_value_is_refused(self):
        # every comma was dropped: "1,5" (1.5 with a decimal comma) became 15 GB, "0,5" 5 GB, "1,2,3" 123 GB
        lim = self.lim('SUBSYNC_DAILY_BUDGET_GB')
        for bad in ('1,5', '0,5', '2,5', '12,34', '1,2,3', '1,000', '1,000.5', ',5'):
            with self.subTest(bad=bad):
                self.assertEqual(limits.check(lim, bad),
                                 (None, 'A number above 0, with a dot for decimals and no commas, like 1.5.'))
        self.assertEqual(limits.check(lim, '1.5'), ('1.5', None))
        self.assertEqual(limits.check(self.lim('SUBSYNC_MAX_USERS'), '1,000'), ('1000', None))   # thousands: whole only

    def test_what_subsync_cant_hold_is_refused_with_the_fields_words(self):
        # "1e300" GB and a 400-digit MB a day were written, and subsync crashed at start (OverflowError) and restarted
        # in a loop; over 4300 digits crashed the check itself (ValueError), and with it the setup page's request
        daily = self.lim('SUBSYNC_DAILY_BUDGET_GB')
        for bad in ('1e300', '2e299', '1' * 16, '9' * 400, '9' * 5000):
            with self.subTest(bad=bad[:20]):
                self.assertEqual(limits.check(daily, bad), (None, 'A number above 0, like 100.'))
        self.assertEqual(limits.check(daily, '1e290'), (None, 'A number above 0, like 100.'))   # no exponent
        self.assertEqual(limits.check(daily, ' 123456789012345 '), ('123456789012345', None))    # 15 characters
        by = {lim.setting: lim for lim in limits.hosting(usenet=True, audio=True)}
        for setting, words in (('SUBSYNC_USER_DAILY_MB', 'A whole number from 1, like 10,000.'),
                               ('SUBSYNC_USER_AUDIO_PER_DAY', 'A whole number from 0, like 20.'),
                               ('SUBSYNC_SYNCS_AT_ONCE', 'A whole number from 1, or auto.')):
            for bad in ('9' * 16, '9' * 400, '9' * 5000, '9,999,999,999,999,999'):
                with self.subTest(setting=setting, bad=bad[:20]):
                    self.assertEqual(limits.check(by[setting], bad), (None, words))
            with self.subTest(setting=setting, good='15 characters'):
                self.assertEqual(limits.check(by[setting], '999,999,999,999'), ('999999999999', None))

    def test_a_gb_value_is_plain_digits_of_at_least_one_byte_that_the_next_run_takes_again(self):
        # "1e-10" (or "0.0000000001") passed as above 0 and subsync read it as 0 bytes a day; "1e15" was
        # written as a 16-digit value the next run refused (with --yes the run stopped); "1_5" was read as 15
        daily = self.lim('SUBSYNC_DAILY_BUDGET_GB')
        for bad in ('1e-10', '0.0000000001', '1e15', '1e3', '1E2', '1_5', '1_000', '+5', '1.2.3', '.', '0.0',
                    '.12345678901234'):                                     # the last: 16 characters as written
            with self.subTest(bad=bad):
                self.assertEqual(limits.check(daily, bad), (None, 'A number above 0, like 100.'))
        for typed, written in (('0.000000001', '0.000000001'), ('0.00001', '0.00001'), ('.5', '0.5'), ('1.', '1'),
                               ('007', '7'), ('1.50', '1.5'), ('20.0', '20'), ('999999999999999', '999999999999999')):
            with self.subTest(typed=typed):
                self.assertEqual(limits.check(daily, typed), (written, None))
                self.assertEqual(limits.check(daily, written), (written, None))     # the next run takes it again
                self.assertGreaterEqual(settings.Settings({daily.setting: written}).daily_budget, 1)

    def test_a_number_in_other_digits_is_refused_with_the_fields_words(self):
        # Isdigit took "²", which int refuses (ValueError out of the check, and with it the setup page's
        # request), and int() and float() read other scripts' digits ("１２", "٣") as numbers
        by = {lim.setting: lim for lim in limits.hosting(usenet=True, audio=True)}
        for setting, words in (('SUBSYNC_MAX_USERS', 'A whole number from 1, like 200.'),
                               ('SUBSYNC_USER_AUDIO_PER_DAY', 'A whole number from 0, like 20.'),
                               ('SUBSYNC_SYNCS_AT_ONCE', 'A whole number from 1, or auto.'),
                               ('SUBSYNC_DAILY_BUDGET_GB', 'A number above 0, like 100.')):
            for bad in ('²', '1²', '１２', '٣', '١.٥'):
                with self.subTest(setting=setting, bad=bad):
                    self.assertEqual(limits.check(by[setting], bad), (None, words))
        self.assertEqual(limits.check(by['SUBSYNC_AUDIO_AT_ONCE'], '٣', places=4),
                         (None, 'A whole number from 1, or auto.'))
        self.assertEqual(limits.check(by['SUBSYNC_AUDIO_BUDGET_GB'], '１'), (None, 'Type 0.5, 1, 2 or 4 (GB per file).'))
        for value in ('²', '１２'):                             # a value set by hand: shown as it is written
            with self.subTest(shown=value):
                self.assertEqual(limits.shown(self.lim('SUBSYNC_USER_DAILY_MB'), value), value)

    def test_the_segmented_choices(self):
        audio, langs = self.lim('SUBSYNC_AUDIO_BUDGET_GB'), self.lim('SUBSYNC_MAX_LANGUAGES')
        self.assertEqual(limits.check(audio, '2'), ('2', None))
        self.assertEqual(limits.check(audio, '3'), (None, 'Type 0.5, 1, 2 or 4 (GB per file).'))
        self.assertEqual(limits.check(audio, '3', current='3'), ('3', None))   # set by hand before: kept
        self.assertEqual(limits.check(langs, '3'), ('3', None))
        self.assertEqual(limits.check(langs, '5'), (None, 'SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to 4.'))

    def test_the_audio_limits_choices_are_written_once(self):
        # plan.AUDIO_BUDGETS: the page's choices, the check's message and the terminal's question and message
        self.assertIs(limits.AUDIO.options, plan.AUDIO_BUDGETS)
        self.assertEqual(plan.AUDIO_BUDGET_WORDS, '0.5, 1, 2 or 4')
        self.assertEqual(limits.NOT_AN_AUDIO_LIMIT, 'Type 0.5, 1, 2 or 4 (GB per file).')
        from installer import engine, ui
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, 'STATE', Path(tmp) / 'state'), \
                mock.patch.object(limits, 'NOT_AN_AUDIO_LIMIT', 'The one message.'), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(ui.Stop) as caught:
            engine.Engine({'AUDIO_BUDGET': '3'}).ask_audio_budget()
        self.assertEqual(caught.exception.message, 'AUDIO_BUDGET: The one message.')
        self.assertIn('How much of each file may the audio route read, in GB: 0.5, 1, 2 or 4?', out.getvalue())
        # and the explanations name the same choices
        self.assertIn(f'{plan.AUDIO_BUDGET_WORDS} GB.', explain.QUESTIONS['AUDIO_BUDGET'][1])
        self.assertIn(f'({plan.AUDIO_BUDGET_WORDS})', explain.SETTINGS['SUBSYNC_AUDIO_BUDGET_GB'][1])
        self.assertIn(f'at most {plan.AUDIO_BUDGETS[-1]} GB from ./setup', explain.SETTINGS['SUBSYNC_FILE_CAP_GB'][1])

    def test_the_daily_audio_counts_take_0(self):
        # 0 stops new audio syncs (SUBSYNC_USER_AUDIO_PER_DAY) or removes the opened answer (the other)
        by = {lim.setting: lim for lim in limits.hosting(audio=True)}
        for name, default in (('SUBSYNC_USER_AUDIO_PER_DAY', '20'), ('SUBSYNC_USER_OPENED_PER_DAY', '5')):
            with self.subTest(name=name):
                self.assertEqual(limits.check(by[name], '0'), ('0', None))
                self.assertEqual(limits.check(by[name], ' 12 '), ('12', None))
                for bad in ('-1', '1.5', 'lots', '', 'auto'):
                    self.assertEqual(limits.check(by[name], bad), (None, f'A whole number from 0, like {default}.'))

    def test_the_usenet_searches_take_0_as_their_explanation_says(self):
        # 0 was refused ("A whole number from 1, like 10.") under a text that says "0 turns Usenet off";
        # subsync refuses 0 only where Usenet is your own install's only source
        for lim in (limits.USENET, limits.USENET_HOSTED):
            with self.subTest(label=lim.label):
                self.assertIn('0 turns Usenet off', explain.setup_help(lim.setting))
                self.assertEqual(limits.check(lim, '0'), ('0', None))
                self.assertEqual(limits.check(lim, ' 7 '), ('7', None))
                for bad in ('-1', '1.5', 'lots', '', 'auto'):
                    self.assertEqual(limits.check(lim, bad), (None, 'A whole number from 0, like 10.'))
        only = ("A whole number from 1, like 10: Usenet is this install's only source, so 0 would leave subsync "
                'nothing to read.')
        for bad in ('0', '-1', 'lots', ''):
            with self.subTest(usenet_only=bad):
                self.assertEqual(limits.check(limits.USENET, bad, usenet_only=True), (None, only))
        self.assertEqual(limits.check(limits.USENET, '3', usenet_only=True), ('3', None))
        self.assertEqual(settings.Settings({'SUBSYNC_USENET_LISTINGS_PER_DAY': '0'}).conflicts(), [])   # as subsync

    def test_audio_syncs_at_a_time_cant_be_more_than_the_files_synced_at_a_time(self):
        lim = {lim.setting: lim for lim in limits.hosting(audio=True)}['SUBSYNC_AUDIO_AT_ONCE']
        self.assertEqual(limits.check(lim, 'auto', places=4), ('auto', None))
        self.assertEqual(limits.check(lim, '4', places=4), ('4', None))
        self.assertEqual(limits.check(lim, '5', places=4),
                         (None, 'A whole number from 1 to 4 (the files synced at a time), or auto.'))
        self.assertEqual(limits.check(lim, '0', places=4), (None, 'A whole number from 1, or auto.'))
        self.assertEqual(limits.check(lim, '9'), ('9', None))                 # the places not known: subsync checks

    def test_the_value_shown_has_thousands_marked(self):
        self.assertEqual(limits.shown(self.lim('SUBSYNC_USER_DAILY_MB'), '1000'), '1,000')
        self.assertEqual(limits.shown(self.lim('SUBSYNC_DAILY_BUDGET_GB'), '50'), '50')
        self.assertEqual(limits.shown(self.lim('SUBSYNC_SYNCS_AT_ONCE'), 'auto'), 'auto')
        self.assertEqual(limits.shown(self.lim('SUBSYNC_USER_DAILY_MB'), '9' * 5000), '9' * 5000)   # refused: as typed


class InTheEnvFile(unittest.TestCase):
    def test_a_value_set_by_hand_is_the_current_value(self):
        lim = limits.personal(audio=False)[0]
        self.assertEqual(limits.current(lim, {}), '100')
        self.assertEqual(limits.current(lim, {'SUBSYNC_DAILY_BUDGET_GB': '20'}), '20')

    def test_only_values_other_than_the_default_are_written(self):
        asked = limits.personal(audio=False)
        self.assertEqual(limits.env_lines(asked, {'SUBSYNC_DAILY_BUDGET_GB': '100'}), {})
        self.assertEqual(limits.env_lines(asked, {'SUBSYNC_DAILY_BUDGET_GB': '20'}), {'SUBSYNC_DAILY_BUDGET_GB': '20'})

    def test_an_asked_limit_replaces_its_hand_set_line_and_other_lines_stay(self):
        asked = limits.personal(audio=False)
        current = {'SUBSYNC_DAILY_BUDGET_GB': '20', 'SUBSYNC_MAX_TRIES': '8', 'SUBSYNC_LANGUAGES': 'eng'}
        written = {'SUBSYNC_LANGUAGES': 'spa'}
        merged, kept = plan.merge_env({**written, **limits.env_lines(asked, {'SUBSYNC_DAILY_BUDGET_GB': '100'})},
                                      current, owned=limits.names(asked))
        self.assertEqual(merged, {'SUBSYNC_LANGUAGES': 'spa', 'SUBSYNC_MAX_TRIES': '8'})
        self.assertEqual(kept, ['SUBSYNC_MAX_TRIES'])


class Units(unittest.TestCase):
    def test_setup_counts_a_gb_as_subsync_does(self):
        # Limits kept its own GB (1e9), and the public instance's status line divided by its own 1e9: the unit
        # of every *_GB setting is settings.GB, one place
        self.assertIs(limits.GB, settings.GB)
        self.assertIs(engine.GB, settings.GB)
        self.assertEqual(settings.GB, 10 ** 9)


if __name__ == '__main__':
    unittest.main()
