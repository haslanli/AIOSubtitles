"""The four small parts the installer adds to subsync/subsync/ui.py."""
import unittest

from subsync import ui


class UiAdditions(unittest.TestCase):
    def test_the_installers_header_has_no_privacy_link(self):
        self.assertNotIn('/privacy', ui.top('https://q.example', start='./', privacy=False))
        self.assertIn('Questions', ui.top('https://q.example', start='./', privacy=False))
        self.assertIn('/privacy', ui.top('https://q.example'))                  # the site's stays as it was

    def test_a_page_can_load_itself_again(self):
        self.assertIn('<meta http-equiv="refresh" content="2">', ui.page('T', '', 'letter', refresh=2))
        self.assertNotIn('http-equiv', ui.page('T', '', 'letter'))

    def test_the_back_arrow_can_be_a_link(self):
        frame = ui.step_frame(['A', 'B'], 1, '<p>x</p>', 'Setup steps', back='aios')
        self.assertIn('<a class="icon-btn" href="aios" aria-label="Back">', frame)
        self.assertNotIn('data-back', frame)

    def test_the_update_panels_button_sends_its_form(self):
        panel = ui.update_panel('Updates', 'About.', 'Running.', 'Ready.', 'Update')
        self.assertIn('<button class="btn primary" type="submit">Update</button>', panel)

    def test_a_long_address_in_a_check_row_wraps(self):
        # the 16-shape check at 360 x 640: an address in a check row pushed the page sideways
        import re
        self.assertRegex(ui.APP_CSS, r'\.checks \.tx\{[^}]*overflow-wrap:anywhere')

    def test_command_lines_are_escaped_and_kept_as_they_are(self):
        self.assertEqual(ui.code_lines(['a {', '    b <c>', '}']), '<pre class="code">a {\n    b &lt;c&gt;\n}</pre>')
        self.assertIn('pre.code{', ui.APP_CSS)
        self.assertNotRegex(ui.APP_CSS, r'(^|[},\s])\.code\{')                 # a bare .code would hit the invite box


if __name__ == '__main__':
    unittest.main()
