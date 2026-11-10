"""Image subtitles (PGS, VobSub) as the timing reference, from the Matroska Cues alone.

A release whose only subtitles are images still carries their timing in the seek index that subsync already reads, so
no extra read is needed.
  VobSub (S_VOBSUB): one block per line; the cue time is the line's start (duration from CueDuration when present).
  PGS (S_HDMV/PGS): one display set shows a line and another clears it, so the cue times are starts and ends. When a
  line follows the previous one with no gap, the clear is left out (the next display set replaces the image), so the
  times don't simply alternate. Each time is labelled start or end by the cheapest consistent labelling (Viterbi): a
  shown interval must last MIN_SHOWN..MAX_SHOWN (a shorter or longer one costs a lot), a start followed directly by
  another start (a left-out clear) costs MERGE, two ends in a row cost the most. Short gaps and long pauses, which can
  only be gaps, pin the labelling; where two labellings cost about the same, the event is left out rather than
  guessed (forward-backward margin SURE), so the reference has fewer lines but wrong starts are rare.
The result is used only when it looks like subtitles (`plausible`): enough lines, and most shown intervals within
MIN_SHOWN..MAX_SHOWN without capping. Otherwise the pipeline falls back to the route it had before.
"""
import statistics

IMAGE_CODECS = ("S_HDMV/PGS", "S_VOBSUB")
MAX_SHOWN = 8.0
MIN_SHOWN = 0.3
SAME_EVENT_S = 0.010
VOBSUB_DEFAULT_S = 3.0
MIN_LINES = 20
MIN_PLAUSIBLE = 0.85


MERGE = 0.3
SURE = 0.75           # how much cheaper "start" must be than "end" for an event to be used
MIN_SURE_SHARE = 0.35 # sure starts per line the events imply (events / 2); fewer: too thin to be a reference
# MERGE/SURE chosen on PGS times simulated from 6 real subtitles and a synthetic mix: sure starts are
# 97-100 % right and keep 48-94 % of a film's lines; busy anime-episode tracks keep ~15 % and fail MIN_SURE_SHARE.
BAD_SHOWN = 3.0
LONG_SHOWN = 2.0
END_END = 5.0


def _shown_cost(length):
    if length < MIN_SHOWN:
        return BAD_SHOWN
    if length > MAX_SHOWN:
        return LONG_SHOWN
    return 0.0


def _steps(gap):
    """Allowed (from, to, cost) labels for two neighbouring events `gap` apart; 0 = start, 1 = end."""
    shown = _shown_cost(gap)
    return ((0, 1, shown), (0, 0, shown + MERGE), (1, 0, 0.0), (1, 1, END_END))


def pgs_lines(events, margin=SURE):
    """events: cue times of a PGS track. Returns [(start, duration)] for the events that are starts with at least
    `margin` to spare: forward and backward min-sum costs give, for each event, the cheapest labelling with it as a
    start and as an end; an event whose two costs are close could be either, so it is left out."""
    ev = []
    for t in sorted(events):
        if not ev or t - ev[-1] > SAME_EVENT_S:
            ev.append(t)
    n = len(ev)
    if n == 0:
        return []
    INF = float("inf")
    fwd = [[INF, INF] for _ in range(n)]
    fwd[0][0] = 0.0                            # the first event shows a line
    for k in range(n - 1):
        for a, b, c in _steps(ev[k + 1] - ev[k]):
            if fwd[k][a] + c < fwd[k + 1][b]:
                fwd[k + 1][b] = fwd[k][a] + c
    bwd = [[INF, INF] for _ in range(n)]
    bwd[-1] = [0.25, 0.0]                      # the last event most likely clears the last line
    for k in range(n - 2, -1, -1):
        for a, b, c in _steps(ev[k + 1] - ev[k]):
            if c + bwd[k + 1][b] < bwd[k][a]:
                bwd[k][a] = c + bwd[k + 1][b]
    lines = []
    for k in range(n):
        as_start, as_end = fwd[k][0] + bwd[k][0], fwd[k][1] + bwd[k][1]
        if as_start + margin <= as_end:
            end = ev[k + 1] if k + 1 < n else ev[k] + VOBSUB_DEFAULT_S
            lines.append((ev[k], end - ev[k]))
    return lines


def vobsub_lines(cues):
    """cues: sorted (time, duration) of a VobSub track."""
    out = []
    for k, (t, d) in enumerate(cues):
        if d <= 0:
            nxt = cues[k + 1][0] if k + 1 < len(cues) else t + VOBSUB_DEFAULT_S
            d = min(VOBSUB_DEFAULT_S, max(0.0, nxt - t))
        out.append((t, d))
    return out


def reference(track):
    """track: {"codec", "cues": [(time, duration)]}. Returns {"cues": [(start, duration)], "plausible", "stats"}."""
    cues = sorted(track.get("cues") or [])
    if track.get("codec", "").startswith("S_VOBSUB"):
        lines = vobsub_lines(cues)
    else:
        lines = pgs_lines([t for t, _ in cues])
    shown = [d for _, d in lines]   # uncapped: a sparse or random set of times shows as long "shown" intervals
    ok_share = sum(MIN_SHOWN <= d <= MAX_SHOWN for d in shown) / len(shown) if shown else 0.0
    stats = {"events": len(cues), "lines": len(lines), "shown_ok": round(ok_share, 3),
             "shown_median_s": round(statistics.median(shown), 2) if shown else None}
    sure = len(lines) / max(1.0, len(cues) / 2) if not track.get("codec", "").startswith("S_VOBSUB") else 1.0
    stats["sure_share"] = round(sure, 3)
    plausible = len(lines) >= MIN_LINES and ok_share >= MIN_PLAUSIBLE and sure >= MIN_SURE_SHARE
    return {"cues": lines, "plausible": plausible, "stats": stats}
