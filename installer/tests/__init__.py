"""The installer's tests. They run on a developer's machine, often in a folder ./setup has set up, so before any test
imports installer.engine (which reads both variables at import): its state folder is a new empty one of the tests' own,
never state/ or an exported AIOSUBTITLES_STATE, and the compose project is the default one, never an exported
AIOSUBTITLES_PROJECT."""
import atexit
import os
import shutil
import tempfile

os.environ.pop('AIOSUBTITLES_PROJECT', None)
STATE = tempfile.mkdtemp(prefix='aiosubtitles-tests-')
atexit.register(shutil.rmtree, STATE, ignore_errors=True)
os.environ['AIOSUBTITLES_STATE'] = STATE
