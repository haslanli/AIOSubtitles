"""subsync's tests. All of them: `python3 -m unittest discover -s tests -t tests` from subsync/. One module:
`python3 -m unittest tests.test_x` from subsync/, or `python3 -m unittest test_x` from this folder.

Imported as a package (tests.test_x), this folder goes on the import path, so each module finds `helpers` there as it
does when this folder is the top of the run (discover's -t, or this folder as the working folder). helpers then puts
the package on the path."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
