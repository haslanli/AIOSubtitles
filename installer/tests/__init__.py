"""The installer's tests. They run on a developer's machine, often in a folder ./setup has set up and in a terminal, so
before any test imports installer.engine or installer.ui (which read the environment at import): the variables setup
reads are never the developer's (the compose project is the default one, no test takes the image's route); its state
folder is a new empty one of the tests' own, never state/ or an exported AIOSUBTITLES_STATE; nothing printed has colour
in it; and a question no test answers ends as setup's "No more input." stop instead of waiting for the keyboard
(test_engine's typing() answers questions over this)."""
import atexit
import builtins
import getpass
import os
import shutil
import tempfile

SETUP_VARIABLES = ('AIOSUBTITLES_PROJECT', 'AIOSUBTITLES_IN_IMAGE', 'AIOSUBTITLES_ROUTE', 'AIOSUBTITLES_SETUP_ID')
for variable in SETUP_VARIABLES:
    os.environ.pop(variable, None)
STATE = tempfile.mkdtemp(prefix='aiosubtitles-tests-')
atexit.register(shutil.rmtree, STATE, ignore_errors=True)
os.environ['AIOSUBTITLES_STATE'] = STATE
os.environ['NO_COLOR'] = '1'


def no_more_input(prompt=''):
    """input() and getpass.getpass() for every test: the input has ended."""
    raise EOFError


builtins.input = getpass.getpass = no_more_input
