#!/bin/zsh
# Builds tiny.mkv (our own generated test data): 90 s of black 64x36 video at 2 fps, a 440 Hz tone that is on
# during each subtitle line (stand-in "speech"), and an English SRT text track with 30 lines; muxed by mkvmerge so
# every subtitle block has a Cue point. Also writes tiny.srt (the embedded timing) and tiny-offset.srt (+2.7 s).
set -e
cd "${0:A:h}"
python3 -I - <<'PY'
lines = []
t = 2.0
for i in range(30):
    d = 1.2 + (i % 4) * 0.3
    lines.append((t, t + d, f"Line {i + 1}"))
    t += d + 1.1 + (i % 3) * 0.4
def ts(x):
    ms = int(round(x * 1000)); return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"
with open("tiny.srt", "w") as f:
    for i, (a, b, x) in enumerate(lines, 1):
        f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{x}\n\n")
with open("tiny-offset.srt", "w") as f:
    for i, (a, b, x) in enumerate(lines, 1):
        f.write(f"{i}\n{ts(a + 2.7)} --> {ts(b + 2.7)}\n{x}\n\n")
expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, _ in lines)
open("tone.expr", "w").write(expr)
PY
ffmpeg -v error -y -f lavfi -i "color=c=black:s=64x36:r=2:d=90" -f lavfi -i "sine=f=440:r=16000:d=90" \
  -filter_complex "[1:a]volume='if($(cat tone.expr),0.5,0)':eval=frame[a]" -map 0:v -map "[a]" \
  -c:v libx264 -preset ultrafast -g 4 -c:a aac -b:a 24k tiny-av.mkv
mkvmerge -q -o tiny.mkv tiny-av.mkv --language 0:eng tiny.srt
rm -f tiny-av.mkv tone.expr
# The same file as MP4, for the MP4 reader: the text track as mov_text, the movie box at the end (tiny.mp4) and at the
# start (tiny-fast.mp4), and one without a subtitle track (tiny-nosub.mp4).
ffmpeg -v error -y -i tiny.mkv -map 0:v -map 0:a -map 0:s -c:v copy -c:a copy -c:s mov_text -metadata:s:s:0 language=eng tiny.mp4
ffmpeg -v error -y -i tiny.mp4 -map 0 -c copy -movflags +faststart tiny-fast.mp4
ffmpeg -v error -y -i tiny.mkv -map 0:v -map 0:a -c copy tiny-nosub.mp4
ls -l tiny.mkv tiny.mp4 tiny-fast.mp4 tiny-nosub.mp4
