"""subsync: synced subtitles for AIOStreams.

A Stremio subtitles addon (added to AIOStreams as a custom addon) that lists "<Language> (synced)" copies of the
subtitles a viewer would see, made per (file, subtitle): the file's own text track from the Matroska seek index
as the reference, else short audio windows (Silero VAD); alass sub-to-sub, then each line snapped to its release
line; the gate against the release track. Below the bar the original is kept.
"""
ALGO_VERSION = "v3"   # part of every stored result's key: a new version recomputes old results
