"""subsync: synced subtitles for AIOStreams.

A Stremio subtitles addon (added to AIOStreams as a custom addon) that lists "<Language> (synced)" copies of the
subtitles a viewer would see, made per (file, subtitle). The reference is read by byte ranges from the file's index
(Matroska or MP4): its own text track, one in the title's original language first, else an image track's (PGS, VobSub)
timings; a file with no subtitle track of its own uses the audio route (short audio windows, Silero VAD), when it is
on. Against a track, each subtitle is fitted by one steady shift, by sections or by alass sub-to-sub, each line then
snapped to its release line; on the audio route, to the speech found. Every copy is judged by the gate against its
reference; below the bar the original is kept.
"""
import os

# onnxruntime (Silero VAD, the OCR models) sends usage events to Microsoft unless this is set before it loads; the
# image sets it too. Set here, at the package's import, before anything can load onnxruntime, and
# over any value inherited from an env file (an empty one turns them back on)
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

ALGO_VERSION = "v3"   # part of every stored result's key: a new version recomputes old results
# RULE_REVISION is part of the rule stamp: a verdict judged by an older revision is judged again. The revisions:
# 2: the fixes of a re-score against AutoSync; 3: a dual-audio file's other audio track;
# 4: typesetting tracks are no reference, the magnet on in-sync originals; 5: the shift path, translations aligned to
# another language's track, re-centred copies; 6: stacked subtitles ranked after plain ones; 7: the new matcher:
# line ends, lines paired by their words, sectioned fits, close calls on the audio route; 8: the language check
#; 9: the same-language track that agrees with the others, a missing scene across languages under one shift,
# the word check's part rule under 40 anchors, the edition order; 10: the original-language track
#; 11: the audio fit's held-out parts, shift changes at a scene added or missing, windows without speech and
# budget, the lines left out only across a shift change (audio and sectioned fits), dialogue tracks only, the 24
# against 23.976 and 25 against 24 fps pairs on the one-shift path, the lines left out at a sectioned fit's break by how
# they land, no sectioned fit where the words say one section, the release's lines far beyond the subtitle's ends, and
# a language settled only by a pass against its own language's track
RULE_REVISION = 11
