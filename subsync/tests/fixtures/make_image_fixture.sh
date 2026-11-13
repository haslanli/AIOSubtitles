#!/bin/zsh
# Builds tiny-pgs.mkv and tiny-pgs-zlib.mkv (our own generated test data): tiny.mkv with a PGS track added whose
# 30 lines show at tiny.srt's times, each a bar whose width stands for its text (tests/imagesubs.py), line 5 with two
# rows, line 8's object split over two segments; the first file stores the PGS track uncompressed, the second
# zlib-compressed, as mkvmerge does by default. Needs tiny.mkv (make_fixture.sh).
set -e
cd "${0:A:h}"
python3 -I - <<'PY'
import sys
sys.path.insert(0, "..")
sys.path.insert(0, "../..")
import imagesubs as I
from subsync.subs import parse
lines = [l for l in parse(open("tiny.srt").read())]
events = []
for i, l in enumerate(lines):
    img = I.stack(I.bar(60 + 4 * i), I.bar(200)) if i == 4 else I.bar(60 + 4 * i)
    events.append((l["s"], I.pgs_show(img, number=2 * i + 1, split=900 if i == 7 else 0)))
    events.append((l["e"], I.pgs_clear(number=2 * i + 2)))
open("lines.sup", "wb").write(I.sup(events))
PY
mkvmerge -q -o tiny-pgs.mkv tiny.mkv --language 0:eng --compression 0:none lines.sup
mkvmerge -q -o tiny-pgs-zlib.mkv tiny.mkv --language 0:eng --compression 0:zlib lines.sup
rm -f lines.sup
ls -l tiny-pgs.mkv tiny-pgs-zlib.mkv
