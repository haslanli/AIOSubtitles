"""subsync: synced subtitles for AIOStreams.

A Stremio subtitles addon (added to AIOStreams as a custom addon) that lists "<Language> (synced)" copies of the
subtitles a viewer would see, made per (file, subtitle): the file's own text track from the Matroska seek index
as the reference, else short audio windows (Silero VAD); alass sub-to-sub, then each line snapped to its release
line; the gate against the release track. Below the bar the original is kept.
"""
import os

# onnxruntime (Silero VAD, the OCR models) sends usage events to Microsoft unless this is set before it loads; the
# image sets it too. Set here, at the package's import, before anything can load onnxruntime, and
# over any value inherited from an env file (an empty one turns them back on)
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

ALGO_VERSION = "v3"   # part of every stored result's key: a new version recomputes old results
RULE_REVISION = 9     # part of the rule stamp: a verdict judged by an older revision is judged again (2: the fixes
                      # of a re-score against AutoSync; 3: a dual-audio file's other audio track;
                      # 4: typesetting tracks are no reference, the magnet on in-sync originals; 5: the shift path,
                      # translations aligned to another language's track, re-centred copies; 6: stacked subtitles
                      # ranked after plain ones; 7: the new matcher: line ends, lines paired by their words,
                      # sectioned fits, close calls on the audio route; 8: the language check; 9: the
                      # same-language track that agrees with the others, a missing scene across languages under one
                      # shift, the word check's part rule under 40 anchors, the edition order)
