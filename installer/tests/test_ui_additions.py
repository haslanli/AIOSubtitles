"""The small parts the installer adds to subsync/subsync/ui.py, and their look in static/app.css."""
import unittest

from subsync import ui


class UiAdditions(unittest.TestCase):
    def test_the_installers_header_has_no_privacy_link(self):
        self.assertNotIn('/privacy', ui.top('https://q.example', start='./', site=False))
        self.assertIn('Questions', ui.top('https://q.example', start='./', site=False))
        self.assertIn('/privacy', ui.top('https://q.example'))                  # the site's stays as it was
        # nor the site's Source link: setup's page is no service for others
        self.assertNotIn('>Source<', ui.top('https://q.example', start='./', site=False))
        self.assertIn('>Source<', ui.top('https://q.example'))

    def test_a_page_can_load_itself_again(self):
        # without the script only; with it, the script shows the progress and the page stays
        page = ui.page('T', '', 'letter', refresh=2)
        self.assertIn('<noscript><meta http-equiv="refresh" content="2"></noscript>', page)
        self.assertEqual(page.count('http-equiv'), 1)
        self.assertIn('<body class="p-letter" data-refresh="2">', page)
        self.assertNotIn('http-equiv', ui.page('T', '', 'letter'))
        self.assertNotIn('data-refresh', ui.page('T', '', 'letter'))

    def test_the_back_arrow_can_be_a_link(self):
        frame = ui.step_frame(['A', 'B'], 1, '<p>x</p>', 'Setup steps', back='aios')
        self.assertIn('<a class="icon-btn" href="aios" aria-label="Back">', frame)
        self.assertNotIn('data-back', frame)

    def test_the_update_panels_button_sends_its_form(self):
        panel = ui.update_panel('Updates', 'About.', 'Running.', 'Ready.', 'Update')
        self.assertIn('<button class="btn primary" type="submit">Update</button>', panel)

    def test_the_update_panels_lines_are_texts_each_in_a_paragraph_of_its_own(self):
        # The page after setup got its second line by splicing '</p><p>' into the running version's HTML
        panel = ui.update_panel('Updates', 'About.', 'Runs <1.0.0>.', None, 'Update', note='Newest & best.')
        self.assertIn('<div class="version"><p>Runs &lt;1.0.0&gt;.</p><p>Newest &amp; best.</p></div>', panel)
        self.assertNotIn('<button', panel)
        ready = ui.update_panel('Updates', 'About.', 'Runs 1.0.0.', 'Ready.', 'Update')
        self.assertIn('<div class="version"><p>Runs 1.0.0.</p><p class="new">Ready.</p></div>', ready)

    def test_a_config_changes_note_keeps_its_paragraphs(self):
        # The earlier config's sentence is a paragraph of its own under your config's note
        changes = ui.config_changes([('Your config 1a2b3c4d…', [('+', 'adds subsync')], 'One & two.\nThree <four>.'),
                                     ("subsync's own config (new)", [], '')])
        self.assertIn('<li><span class="sign" aria-hidden="true">+</span>adds subsync</li></ul><p>One &amp; two.</p>'
                      '<p>Three &lt;four&gt;.</p></section>', changes)
        self.assertIn("<h2>subsync&#x27;s own config (new)</h2></section>", changes)

    def test_a_config_with_no_change_lines_has_no_list(self):
        # subsync's own configs (and an unchanged one) got an empty list: a 20 px gap between heading and note
        # where the others have 10, and "list, 0 items" to a screen reader
        changes = ui.config_changes([("subsync's own config (new)", [], 'a copy of yours'),
                                     ('Your config', [('+', 'adds subsync')], None)])
        self.assertEqual(changes, '<div class="changes"><section><h2>subsync&#x27;s own config (new)</h2>'
                                  '<p>a copy of yours</p></section><section><h2>Your config</h2><ul><li><span '
                                  'class="sign" aria-hidden="true">+</span>adds subsync</li></ul></section></div>')

    def test_a_choice_without_an_explanation_writes_none(self):
        # "Remove the saved key" and "Delete everyone's records and stored copies" got an empty explanation:
        # 6 px of empty space under the title, and a description pointing at nothing (as radio_row already avoided)
        self.assertEqual(ui.choice('remove-subdl_key', 'Remove the saved key', '', name='REMOVE_SUBDL_KEY', value='yes'),
                         '<label class="choice"><input type="checkbox" id="remove-subdl_key" name="REMOVE_SUBDL_KEY" '
                         'value="yes" aria-labelledby="remove-subdl_key-title"><span><b id="remove-subdl_key-title">'
                         'Remove the saved key</b></span></label>')
        box = ui.choice('confirm', 'Delete', '', error_id='confirm-error')
        self.assertIn('aria-labelledby="confirm-title" aria-describedby="confirm-error" aria-invalid="true">', box)
        self.assertNotIn('confirm-help', box)
        with_help = ui.choice('c', 'Title', 'Help.', error_id='e')      # one with an explanation: as before
        self.assertIn('aria-describedby="e c-help" aria-invalid="true"><span><b id="c-title">Title</b>'
                      '<span id="c-help">Help.</span></span></label>', with_help)

    def test_a_long_address_in_a_check_row_wraps(self):
        # the 16-shape check at 360 x 640: an address in a check row pushed the page sideways
        self.assertRegex(ui.APP_CSS, r'\.checks \.tx\{[^}]*overflow-wrap:anywhere')

    def test_command_lines_are_escaped_and_kept_as_they_are(self):
        self.assertEqual(ui.code_lines(['a {', '    b <c>', '}']), '<pre class="code">a {\n    b &lt;c&gt;\n}</pre>')
        self.assertIn('pre.code{', ui.APP_CSS)
        self.assertNotRegex(ui.APP_CSS, r'(^|[},\s])\.code\{')                 # a bare .code would hit the invite box


    def test_a_progress_line_shows_its_note_under_it(self):
        # The notes under finished lines of an install or update were dropped
        self.assertEqual(ui.progress_list([('done', 'Switched', 'Kept <x>.'), ('now', 'Checking', None)]),
                         '<ol class="progress"><li class="done"><span class="ic" role="img" aria-label="Done">'
                         f'{ui.TICK_BOLD}</span><span class="tx">Switched<span class="more">Kept &lt;x&gt;.</span>'
                         '</span></li><li class="now"><span class="ic" role="img" aria-label="Working"></span>'
                         '<span class="tx">Checking</span></li></ol>')

    def test_a_limits_problem_is_tied_to_its_box_or_row(self):
        # As field ties a problem: under the label, the control described by it first and marked
        box = ui.number_field('n', 'N', '0', help_text='H.', error='Bad <n>.')
        self.assertIn('<label for="n">N</label><p class="field-error" id="n-error">Bad &lt;n&gt;.</p><div class="num">',
                      box)
        self.assertIn('aria-describedby="n-error n-help" aria-invalid="true">', box)
        self.assertIn('aria-describedby="n-help">', ui.number_field('n', 'N', '0', help_text='H.'))
        self.assertNotIn('aria-describedby', ui.number_field('n', 'N', '0'))
        row = ui.segmented('s', 'S', ['1', '2'], '3', help_text='H.', unit='GB', error='Pick one.')
        self.assertIn('<span class="label" id="s-label">S</span><p class="field-error" id="s-error">Pick one.</p>'
                      '<div class="seg-row"><div class="seg" id="s" role="radiogroup" aria-labelledby="s-label" '
                      'aria-describedby="s-error s-help" aria-invalid="true">', row)
        self.assertIn('</div><span class="seg-unit">GB</span></div><p class="help" id="s-help">H.</p></div>', row)
        self.assertIn('aria-labelledby="s-label">', ui.segmented('s', 'S', ['1'], '1'))
        # Its first button has the id a problem's link goes to (the row can't take focus); the others none
        self.assertIn('<label><input type="radio" id="s-1" name="s" value="1"><span>1</span></label><label><input '
                      'type="radio" name="s" value="2"><span>2</span></label>', row)

    def test_a_problems_summary_links_to_its_field(self):
        # The public pages' summary, for setup's steps too (pages._problem uses it)
        summary = ui.problem_summary('No <good>.', 'That didn\'t go through', 'f')
        self.assertTrue(summary.startswith('<div class="note problem" role="alert" id="problem" tabindex="-1">'))
        self.assertIn('<b>That didn&#x27;t go through</b><p id="problem-message"><a href="#f">No &lt;good&gt;.</a></p>',
                      summary)
        self.assertIn('<p id="problem-message">No &lt;good&gt;.</p>', ui.problem_summary('No <good>.', 'T'))
        self.assertEqual(ui.problem_summary('', 'T', 'f'), '')

    def test_the_done_screens_first_line_and_the_step_heading_have_rules_of_their_own(self):
        # The first line took no look (.letter .lead needs main.letter); the heading's top space came from the
        # small pages' h1 rule, which the step pages happen to share
        self.assertRegex(ui.APP_CSS, r'(?m)^\.letter \.lead,\.steparea \.lead\{font-weight:700;color:var\(--text\);'
                                     r'font-size:19px\}')
        self.assertRegex(ui.APP_CSS, r'(?m)^\.steparea h1\{[^}]*margin-top:clamp\(56px,8vh,88px\)\}')
        self.assertRegex(ui.APP_CSS, r'(?m)^\.p-settings h1,\.p-letter h1\{[^}]*margin-top:clamp\(56px,8vh,88px\)\}')
        self.assertNotRegex(ui.APP_CSS, r'h1\[tabindex')                    # no heading takes focus now

    def test_a_long_image_name_in_a_choice_row_wraps(self):
        # A digest-pinned image, one long word, made the step 910 px wide on a 390 px phone
        self.assertRegex(ui.APP_CSS, r'(?m)^\.way b\{[^}]*overflow-wrap:anywhere\}')
        self.assertRegex(ui.APP_CSS, r'(?m)^\.way \.head~span\{[^}]*overflow-wrap:anywhere\}')


if __name__ == '__main__':
    unittest.main()
