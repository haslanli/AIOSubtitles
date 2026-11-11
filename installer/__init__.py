"""./setup's code. It shares subsync's Real-Debrid rules (subsync/subsync/realdebrid.py), so subsync's folder goes on the
import path first: `from subsync import realdebrid` then finds that package, not the folder of the same name."""
import sys
from pathlib import Path

_SUBSYNC = str(Path(__file__).resolve().parent.parent / 'subsync')
if _SUBSYNC not in sys.path:
    sys.path.insert(0, _SUBSYNC)
