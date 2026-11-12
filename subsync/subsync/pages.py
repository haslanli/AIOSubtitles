"""The public mode's web pages: sign-up, the result, a person's settings, delete, privacy, not found.

HTML with the style inline and one small script of our own (static/app.js, served by this service at /static/app.js)
for the few moments that move; every page works without it. Nothing is loaded from elsewhere, and every value from a
person or a config is escaped. The texts come from explain.py, where each has its one explanation.

The look: warm paper and ink with one grease-pencil red for marks (a darkroom's safelight in dark mode); an old-style
serif for headings over a humanist sans, monospace for links and timecodes, all from fonts already on people's
devices; type and spacing that grow with the screen; one layout for phones, a wider one for tablets and two columns
on wide screens. The picture on the sign-up page shows what the service does: your subtitles sliding along the file's
own subtitle track until their lines land on it.
"""
import html
import os

from . import explain
from .aiosconfig import LANGUAGES

_NAMES = {three: name for name, _, three in LANGUAGES}
MAX_LANGUAGES = 4

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "app.js"), encoding="utf-8") as _f:
    APP_JS = _f.read()

# Every colour, by name, for light and dark. Each pair people read or must see passes WCAG 2.1 AA (tests).
TOKENS = {
    "light": {"paper": "#f3ede2", "surface": "#fbf8f1", "sunk": "#e9e1d2", "rule": "#d6cab5", "ink": "#1c1915",
              "ink-2": "#4f483d", "on-ink": "#f3ede2", "field": "#837866", "mark": "#b5391c", "on-mark": "#ffffff",
              "link": "#9a3116", "danger": "#8f200d", "on-danger": "#ffffff", "focus": "#1c1915", "cue": "#2b2720",
              "ok-bg": "#e2e9d6", "ok-ink": "#2d4a1c", "warn-bg": "#f5e2b0", "warn-ink": "#573a00",
              "err-bg": "#f6dbd1", "err-ink": "#7d1c0d"},
    "dark": {"paper": "#15120e", "surface": "#1f1b16", "sunk": "#2a251e", "rule": "#3b342a", "ink": "#f2ebdd",
             "ink-2": "#bcb19c", "on-ink": "#15120e", "field": "#8f8572", "mark": "#ff7f57", "on-mark": "#1d0b04",
             "link": "#ff9b7a", "danger": "#c43a1f", "on-danger": "#ffffff", "focus": "#f2ebdd", "cue": "#e6dcc8",
             "ok-bg": "#1e2a16", "ok-ink": "#c6ddae", "warn-bg": "#3a2b0d", "warn-ink": "#f3d690",
             "err-bg": "#3d170f", "err-ink": "#ffb8a5"},
}


def _vars(scheme):
    return " ".join(f"--{name}: {value};" for name, value in TOKENS[scheme].items())


STYLE = """
:root { color-scheme: light dark; """ + _vars("light") + """
  --display: "Iowan Old Style", "Palatino Linotype", Palatino, "URW Palladio L", P052, "Book Antiqua", Georgia, serif;
  --sans: Seravek, "Gill Sans Nova", Ubuntu, Calibri, "DejaVu Sans", source-sans-pro, system-ui, sans-serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Mono", "Segoe UI Mono", Menlo, Consolas, "Roboto Mono", monospace;
  --s-1: clamp(.84rem, .8rem + .18vw, .93rem); --s0: clamp(1rem, .96rem + .2vw, 1.08rem);
  --s1: clamp(1.15rem, 1.04rem + .5vw, 1.38rem); --s2: clamp(1.4rem, 1.18rem + 1vw, 1.9rem);
  --s3: clamp(1.9rem, 1.4rem + 2.3vw, 3.1rem); --s4: clamp(2.75rem, 1.6rem + 5.4vw, 5.9rem);
  --gap: clamp(1rem, .7rem + 1.4vw, 2rem); --gap-l: clamp(1.75rem, 1rem + 3.4vw, 4.75rem);
  --edge: clamp(1rem, .4rem + 3vw, 3.75rem); --radius: 16px;
  --ease: cubic-bezier(.2, .8, .2, 1); --spring: cubic-bezier(.3, 1.45, .5, 1); --grain: .085; }
@media (prefers-color-scheme: dark) {
  :root { """ + _vars("dark") + """ --grain: .05; }
}
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body { margin: 0; min-height: 100vh; background: var(--paper); color: var(--ink); font: 400 var(--s0)/1.6 var(--sans);
  -webkit-font-smoothing: antialiased; overflow-x: clip; display: flex; flex-direction: column; }
.grain { position: fixed; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 0; opacity: var(--grain); }
.site, main, .site-foot { position: relative; z-index: 1; width: 100%; max-width: 92rem; margin: 0 auto;
  padding-inline: var(--edge); }
main { flex: 1; padding-block: var(--gap) var(--gap-l); }
.site { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding-top: var(--gap); }
.wordmark { font: 600 var(--s1)/1 var(--display); color: var(--ink); text-decoration: none; letter-spacing: -.01em;
  padding-block: .6rem; }
.wordmark em { color: var(--mark); }
.site nav { display: flex; gap: clamp(.75rem, .4rem + 1.5vw, 1.75rem); }
.site nav a, .site-foot a { color: var(--ink-2); text-decoration: none; font-size: var(--s-1); padding-block: .7rem;
  background: linear-gradient(var(--mark), var(--mark)) no-repeat 0 85% / 0 2px; transition: background-size .35s var(--ease), color .2s; }
.site nav a:hover, .site-foot a:hover { color: var(--ink); background-size: 100% 2px; }
.site-foot { display: flex; flex-wrap: wrap; gap: .25rem clamp(1rem, .5rem + 2vw, 2rem); align-items: baseline;
  padding-block: var(--gap) calc(var(--gap) * 1.5); color: var(--ink-2); font-size: var(--s-1); }
.site-foot::before { content: ""; flex-basis: 100%; height: 1px; margin-bottom: var(--gap); background: var(--rule); }
.site-foot span { margin-right: auto; }
@media (max-width: 40rem) { .site-foot span { flex-basis: 100%; } }
h1, h2, h3 { font-family: var(--display); font-weight: 600; letter-spacing: -.015em; line-height: 1.1; margin: 0;
  text-wrap: balance; }
h1 { font-size: var(--s3); }
h2 { font-size: var(--s2); }
.display { font-size: var(--s4); font-weight: 500; letter-spacing: -.032em; line-height: .96; max-width: 11ch; }
.display .hand { position: relative; display: inline-block; font-style: italic; color: var(--mark); }
.display .hand svg { position: absolute; left: -3%; bottom: -.16em; width: 106%; height: .34em; overflow: visible; }
.hand path, .done path { fill: none; stroke: var(--mark); stroke-linecap: round; stroke-linejoin: round;
  stroke-dasharray: 1; stroke-dashoffset: 1; animation: draw 1.05s var(--ease) .85s forwards; }
.hand path { stroke-width: 3.4; }
p { margin: 0 0 .85em; }
.lede { font-size: var(--s1); line-height: 1.5; max-width: 36rem; margin-top: clamp(1rem, .6rem + 1.6vw, 2rem); }
.help { color: var(--ink-2); font-size: var(--s-1); line-height: 1.55; margin: .5rem 0 0; max-width: 42rem; }
.kicker { display: flex; align-items: center; gap: .7rem; font: 500 var(--s-1)/1.3 var(--mono); color: var(--ink-2);
  margin: 0 0 clamp(.9rem, .6rem + 1vw, 1.6rem); }
.kicker::before { content: ""; width: 1.75rem; height: 2px; background: var(--mark); }
a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: .2em; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.layout { display: grid; gap: var(--gap-l); grid-template-columns: minmax(0, 1fr); align-items: start; }
@media (min-width: 64rem) {
  .layout { grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); column-gap: clamp(2.5rem, 1rem + 4vw, 6rem); }
}
@media (min-width: 64rem) and (min-height: 50rem) { .layout.settled > .rail { position: sticky; top: 1.5rem; } }
.panel { background: var(--surface); border: 1px solid var(--rule); border-radius: var(--radius);
  padding: clamp(1.1rem, .75rem + 1.8vw, 2.25rem); box-shadow: 5px 6px 0 -1px var(--sunk), 5px 6px 0 0 var(--rule); }
.panel + .panel { margin-top: var(--gap); }
.panel h2 + p, .panel h2 + .help { margin-top: .5rem; }
.panel h2 + .note { margin-top: 1rem; }
.panel.cut { background: transparent; box-shadow: none; border: 1.5px dashed var(--field); }
.stack > * + * { margin-top: var(--gap); }
.strip { position: relative; margin: clamp(1.5rem, 1rem + 2vw, 2.75rem) calc(var(--edge) * -1) 0;
  padding: 1rem var(--edge) 1.1rem; background: var(--sunk); border-block: 1px solid var(--rule); overflow: hidden;
  touch-action: pan-y; user-select: none; -webkit-user-select: none; }
@media (min-width: 40rem) { .strip { margin-inline: 0; padding-inline: 1.25rem; border: 1px solid var(--rule);
  border-radius: var(--radius); } }
.strip-head { display: flex; justify-content: space-between; gap: 1rem; font: 500 .74rem/1 var(--mono);
  color: var(--ink-2); margin-bottom: .9rem; }
.readout { position: relative; min-width: 5.5rem; text-align: right; font-weight: 700; }
.r-off { color: var(--mark); animation: hide .2s linear 1.9s forwards; }
.r-on { position: absolute; right: 0; opacity: 0; color: var(--ink); animation: show .3s linear 2s forwards; }
.js .r-off, .js .r-on { display: none; }
.lanes { position: relative; display: grid; gap: .35rem; }
.lane-label { font: 500 .72rem/1.2 var(--mono); color: var(--ink-2); }
.lane-label + .lane { margin-bottom: .5rem; }
.lane { position: relative; height: clamp(1.9rem, 1.6rem + 1vw, 2.4rem); }
.cue { position: absolute; top: 0; bottom: 0; left: calc(var(--x) * 1%); width: calc(var(--w) * 1%);
  border-radius: 5px 8px 6px 4px / 7px 4px 8px 5px; background: var(--cue); color: var(--paper);
  font: 500 .76rem/1 var(--sans); display: flex; align-items: center; padding: 0 .55rem; white-space: nowrap;
  overflow: hidden; }
.cue span { opacity: 0; } @media (min-width: 48rem) { .cue span { opacity: .92; } }
.yours .cue { background: var(--mark); color: var(--on-mark); }
.slide, .nudge { position: absolute; inset: 0; }
.slide { transform: translateX(var(--from)); animation: settle 1.5s var(--spring) .6s forwards; }
.nudge { will-change: transform; }
.ruler { height: .8rem; margin-top: .15rem; opacity: .75;
  background: repeating-linear-gradient(90deg, var(--ink-2) 0 1px, transparent 1px 12.5%) 0 0 / 100% 100% no-repeat,
              repeating-linear-gradient(90deg, var(--field) 0 1px, transparent 1px 2.5%) 0 100% / 100% 45% no-repeat; }
.playhead { position: absolute; top: -.45rem; bottom: 0; left: 41%; width: 2px; background: var(--ink); opacity: .85;
  pointer-events: none; }
.playhead::before { content: ""; position: absolute; top: 0; left: -5px; border: 6px solid transparent;
  border-top: 7px solid var(--ink); border-bottom: 0; }
.notes ol, .steps { list-style: none; counter-reset: n; padding: 0; margin: 1rem 0 0; display: grid; gap: .85rem; }
.notes li, .steps li { counter-increment: n; display: grid; grid-template-columns: 2.25rem minmax(0, 1fr); gap: .6rem; }
.notes li::before, .steps li::before { content: counter(n, decimal-leading-zero); font: 600 var(--s-1)/1.75 var(--mono);
  color: var(--mark); }
.notes { margin-top: var(--gap-l); max-width: 36rem; }
.notes h2 { font-size: var(--s1); }
fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
fieldset + fieldset, .submit { margin-top: clamp(1.6rem, 1rem + 2vw, 2.5rem); }
fieldset + fieldset { padding-top: .35rem; border-top: 1.5px dashed var(--rule); }
legend { display: flex; align-items: baseline; gap: .8rem; padding: 0; margin: 0; font: 600 var(--s2)/1.1 var(--display);
  letter-spacing: -.015em; }
legend .no { font: 600 var(--s-1)/1 var(--mono); color: var(--mark); letter-spacing: .03em; }
.field { margin-top: 1.15rem; }
label { display: block; font-weight: 600; margin-bottom: .4rem; }
input[type=text], input[type=url], input[type=password], textarea { display: block; width: 100%; min-height: 3rem;
  padding: .7rem .85rem; font: inherit; color: var(--ink); background: var(--paper); border: 1px solid var(--field);
  border-bottom-width: 2px; border-radius: 10px; transition: border-color .15s, background-color .15s; }
input[type=text]:hover, input[type=url]:hover, input[type=password]:hover { border-color: var(--ink-2); }
input:focus-visible, textarea:focus-visible { outline-offset: 1px; border-color: var(--ink); }
input[aria-invalid=true] { border-color: var(--err-ink); background: var(--err-bg); }
::placeholder { color: var(--ink-2); opacity: .8; }
textarea[readonly] { resize: none; font: 500 var(--s-1)/1.6 var(--mono); word-break: break-all; background: var(--sunk);
  min-height: 0; }
.field-error { color: var(--err-ink); font-weight: 600; margin: 0 0 .45rem; }
.pw { position: relative; }
.pw input { padding-right: 5.25rem; }
.pw button { position: absolute; top: .3rem; right: .3rem; bottom: .3rem; min-width: 4.5rem; padding: 0 .8rem;
  font: 600 var(--s-1)/1 var(--sans); color: var(--ink); background: var(--sunk); border: 0; border-radius: 7px;
  cursor: pointer; }
.chip-set { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1.1rem; }
.chip { position: relative; display: inline-block; margin: 0; font-weight: 500; cursor: pointer; }
.chip input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.chip-face { display: inline-flex; align-items: center; gap: .45rem; min-height: 2.75rem; padding: .45rem 1rem;
  border: 1px solid var(--field); border-radius: 999px; background: var(--paper);
  transition: background-color .2s, color .2s, border-color .2s, transform .25s var(--spring); }
.chip:hover .chip-face { border-color: var(--ink); }
.chip input:active + .chip-face { transform: scale(.95); }
.chip input:checked + .chip-face { background: var(--mark); color: var(--on-mark); border-color: var(--mark); }
.chip input:checked + .chip-face::before { content: "\\2713"; font-weight: 700; }
.js .chip input:checked + .chip-face::before { content: none; }
.chip-n { display: none; }
.js .chip input:checked + .chip-face .chip-n { display: inline-grid; place-items: center; width: 1.4rem; height: 1.4rem;
  border-radius: 50%; background: var(--on-mark); color: var(--mark); font: 700 .74rem/1 var(--mono); }
.chip input:focus-visible + .chip-face { outline: 3px solid var(--focus); outline-offset: 3px; }
.chip input:disabled + .chip-face { opacity: .45; }
.chip input:disabled { cursor: not-allowed; }
.chip-count { font: 500 var(--s-1)/1.4 var(--mono); color: var(--ink-2); margin: .85rem 0 0; }
.check { display: grid; grid-template-columns: 1.6rem minmax(0, 1fr); gap: .15rem .8rem; margin-top: 1.2rem; }
.check input { appearance: none; -webkit-appearance: none; width: 1.6rem; height: 1.6rem; margin: .1rem 0 0;
  border: 2px solid var(--field); border-radius: 7px; background: var(--paper); display: grid; place-items: center;
  cursor: pointer; transition: background-color .15s, border-color .15s; }
.check input::after { content: ""; width: .45rem; height: .8rem; margin-top: -.2rem; border: solid var(--on-mark);
  border-width: 0 3px 3px 0; transform: rotate(45deg) scale(0); transition: transform .25s var(--spring); }
.check input:checked { background: var(--mark); border-color: var(--mark); }
.check input:checked::after { transform: rotate(45deg) scale(1); }
.check label { margin: .15rem 0 0; }
.check .help { grid-column: 2; margin-top: .2rem; }
@media (forced-colors: active) { .check input { appearance: auto; -webkit-appearance: auto; } }
.btn { appearance: none; display: inline-flex; align-items: center; justify-content: center; gap: .65rem;
  min-height: 3.1rem; padding: .85rem 1.55rem; font: 600 var(--s0)/1.1 var(--sans); color: var(--on-ink);
  background: var(--ink); border: 0; border-radius: 12px; cursor: pointer; text-decoration: none;
  box-shadow: 0 4px 0 0 var(--mark); transition: transform .14s var(--ease), box-shadow .14s var(--ease); }
.btn:hover { transform: translateY(-1px); box-shadow: 0 5px 0 0 var(--mark); }
.btn:active { transform: translateY(3px); box-shadow: 0 1px 0 0 var(--mark); }
.btn .arrow { display: inline-block; transition: transform .3s var(--spring); }
.btn:hover .arrow { transform: translateX(4px); }
.btn.quiet { color: var(--ink); background: var(--surface); border: 1px solid var(--field); box-shadow: 0 3px 0 0 var(--rule); }
.btn.quiet:hover { box-shadow: 0 4px 0 0 var(--field); }
.btn.quiet.done { color: var(--ok-ink); background: var(--ok-bg); }
.btn.danger { color: var(--on-danger); background: var(--danger); box-shadow: 0 4px 0 0 var(--ink); }
.btn.small { min-height: 2.75rem; padding: .6rem 1.05rem; font-size: var(--s-1); }
.submit { display: grid; gap: .9rem; justify-items: start; }
.actions { display: flex; flex-wrap: wrap; gap: .9rem 1rem; align-items: center; margin-top: 1.25rem; }
@media (max-width: 40rem) { .submit .btn, .actions .btn { width: 100%; } }
.note { position: relative; border: 1px solid; border-radius: 12px; padding: 1rem 1.15rem 1rem 1.35rem;
  margin: 0 0 var(--gap); }
.note::before { content: ""; position: absolute; left: -1px; top: .8rem; bottom: .8rem; width: 4px; border-radius: 4px;
  background: currentColor; }
.note strong { display: block; font: 600 var(--s1)/1.25 var(--display); margin-bottom: .3rem; }
.note p { margin: .35rem 0 0; } .note a { color: inherit; }
.note.ok { background: var(--ok-bg); color: var(--ok-ink); border-color: var(--ok-ink); }
.note.warn { background: var(--warn-bg); color: var(--warn-ink); border-color: var(--warn-ink); }
.note.error { background: var(--err-bg); color: var(--err-ink); border-color: var(--err-ink); }
.note:focus { outline: none; } .note:focus-visible { outline: 3px solid var(--focus); }
.done { list-style: none; padding: 0; margin: 1rem 0 0; display: grid; gap: .8rem; }
.done li { display: grid; grid-template-columns: 1.5rem minmax(0, 1fr); gap: .75rem; }
.done svg { width: 1.4rem; height: 1.4rem; margin-top: .15rem; overflow: visible; }
.done path { stroke-width: 3; animation-duration: .55s; animation-delay: calc(.5s + var(--i) * .2s); }
.gauge { margin-top: 1rem; }
.gauge p { margin: 0 0 .4rem; font: 500 var(--s-1)/1.4 var(--mono); }
.track { position: relative; height: .6rem; border-radius: 99px; background: var(--sunk); overflow: hidden; }
.track i { position: absolute; inset: 0; border-radius: inherit; background: var(--ink); transform-origin: left center;
  transform: scaleX(var(--v)); transition: transform 1.2s var(--ease) .15s; }
.gauge.full .track i { background: var(--mark); }
.letter { max-width: 46rem; }
.letter h1 { font-size: var(--s3); margin-bottom: var(--gap); }
.letter .panel h2 { font-size: var(--s1); }
.rise { transition: opacity .75s var(--ease), transform .75s var(--ease); transition-delay: var(--d, 0s); }
.js .rise.pending { opacity: 0; transform: translateY(1.25rem); }
.js .rise.pending .track i { transform: scaleX(0); }
.enter > * { animation: enter .85s var(--ease) both; animation-delay: calc(var(--i, 0) * 90ms + 60ms); }
@keyframes enter { from { opacity: 0; transform: translateY(14px); } }
@keyframes settle { to { transform: translateX(0); } }
@keyframes draw { to { stroke-dashoffset: 0; } }
@keyframes hide { to { opacity: 0; } }
@keyframes show { to { opacity: 1; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  .slide { transform: none; }
  .hand path, .done path { stroke-dashoffset: 0; }
  .r-off { display: none; }
  .r-on { position: static; opacity: 1; }
  .js .rise.pending { opacity: 1; transform: none; }
}
@media print { .grain, .strip, .site nav { display: none; } .panel { box-shadow: none; }
  .js .rise.pending { opacity: 1; transform: none; } }
"""

GRAIN = ('<svg class="grain" aria-hidden="true" focusable="false"><filter id="grain-noise"><feTurbulence '
         'type="fractalNoise" baseFrequency=".85" numOctaves="3" stitchTiles="stitch"/><feColorMatrix type="matrix" '
         'values="0 0 0 0 .1 0 0 0 0 .08 0 0 0 0 .05 0 0 0 1.4 -.35"/></filter><rect width="100%" height="100%" '
         'filter="url(#grain-noise)"/></svg>')
_HAND = ('<svg viewBox="0 0 200 20" preserveAspectRatio="none" aria-hidden="true" focusable="false"><path pathLength="1" '
         'd="M3 13 C 38 6, 70 15, 104 9 S 168 5, 197 11"/></svg>')
_TICK = ('<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path pathLength="1" d="M4 13.5 9.5 19 20 5.5"/>'
         '</svg>')

# The illustration: the file's own subtitle track, and yours, a little late until it settles. (x, width, words)
_REF_CUES = ((3, 16, "Where were you?"), (23, 8, "Out."), (35, 14, "Out where?"), (54, 12, "Just out."),
             (73, 21, "You missed dinner."))
_YOUR_CUES = ((3, 16, "¿Dónde estabas?"), (23, 8, "Fuera."), (35, 14, "¿Fuera dónde?"), (54, 12, "Solo fuera."),
              (73, 21, "Te perdiste la cena."))


def e(value):
    return html.escape(str(value), quote=True)


def _page(title, body, personal=False):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            + ("<meta name=\"robots\" content=\"noindex\">" if personal else "")
            + f"<title>{e(title)}</title><style>{STYLE}</style><script src=\"/static/app.js\" defer></script></head>"
            f"<body>{GRAIN}<header class=\"site\"><a class=\"wordmark\" href=\"/\">AIO<em>Subtitles</em></a>"
            f"<nav aria-label=\"AIOSubtitles\"><a href=\"/privacy\">Privacy</a><a href=\"{e(explain.CONTACT)}\">Questions"
            f"</a></nav></header><main id=\"main\">{body}</main><footer class=\"site-foot\"><span>AIOSubtitles: subtitles "
            "in sync with the file you play.</span><a href=\"/privacy\">Privacy</a>"
            f"<a href=\"{e(explain.CONTACT)}\">Questions</a></footer></body></html>")


def _help(key, text=None):
    return f"<p class=\"help\" id=\"{e(key)}-help\">{e(text if text is not None else explain.PUBLIC_FIELDS[key])}</p>"


def _described(key, error):
    return f"{key}-help" + (f" {key}-error" if error else "")


def _field_error(key, error):
    return f"<p class=\"field-error\" id=\"{e(key)}-error\">{e(error)}</p>" if error else ""


def _field(name, label, kind="text", extra="", help=None, error=None, reveal=False):
    key = help or name   # the explanation (and the element id) when the field's name is shared, like "password"
    box = (f"<input type=\"{kind}\" id=\"{e(key)}\" name=\"{e(name)}\" aria-describedby=\"{_described(key, error)}\""
           + (" aria-invalid=\"true\"" if error else "") + f" {extra}>")
    if reveal:
        box = (f"<div class=\"pw\">{box}<button type=\"button\" data-reveal=\"{e(key)}\" data-show=\"Show\" "
               f"data-hide=\"Hide\" aria-controls=\"{e(key)}\" hidden>Show</button></div>")
    return (f"<div class=\"field\"><label for=\"{e(key)}\">{e(label)}</label>{_field_error(key, error)}{box}"
            f"{_help(key)}</div>")


def _check(name, label, checked=True):
    return (f"<div class=\"check\"><input type=\"checkbox\" id=\"{e(name)}\" name=\"{e(name)}\" value=\"on\""
            f"{' checked' if checked else ''} aria-describedby=\"{e(name)}-help\"><label for=\"{e(name)}\">{e(label)}"
            f"</label>{_help(name)}</div>")


def _chips(name, order, error=None):
    """The languages as chips: a real checkbox in each, numbered by the script in the order they were picked."""
    order = [c for c in dict.fromkeys(order) if c in _NAMES][:MAX_LANGUAGES]
    chips = []
    for lang_name, _, three in LANGUAGES:
        at = order.index(three) + 1 if three in order else ""
        chips.append(f"<label class=\"chip\"><input type=\"checkbox\" name=\"lang\" value=\"{e(three)}\""
                     f"{' checked' if at else ''}><span class=\"chip-face\">"
                     f"<span class=\"chip-n\" aria-hidden=\"true\">{at}</span>{e(lang_name)}</span></label>")
    return (f"<input type=\"hidden\" name=\"lang_ui\" value=\"1\"><input type=\"hidden\" name=\"lang_order\" "
            f"value=\"{e(','.join(order))}\">{_field_error(name, error)}{_help(name)}"
            f"<div class=\"chip-set\">{''.join(chips)}</div>"
            f"<p class=\"chip-count\" data-count data-template=\"{{n}} of {MAX_LANGUAGES} chosen.\" "
            f"hidden>{len(order)} of {MAX_LANGUAGES} chosen.</p>")


def _languages_fieldset(number, order, error=None):
    no = f"<span class=\"no\">{number:02d}</span>" if number else ""
    return (f"<fieldset data-max=\"{MAX_LANGUAGES}\" aria-describedby=\"{_described('languages', error)}\"><legend>{no}"
            "Languages</legend>" + _chips("languages", order, error) + "</fieldset>")


def _note(kind, text, title=None, role=None, extra=""):
    if not text:
        return ""
    role_attr = f" role=\"{role}\"" if role else ""
    head = f"<strong>{e(title)}</strong>" if title else ""
    return f"<div class=\"note {kind}\"{role_attr}{extra}>{head}<p>{e(text)}</p></div>"


def _problem(error, field, title, fields=(), kept=False):
    """The summary of a problem at the top of the page, with a link to the field it is about when the page has it.
    kept: say that the link, password and invite code must be entered again (the sign-up form)."""
    if not error:
        return ""
    message = f"<a href=\"#{e(field)}\">{e(error)}</a>" if field in fields else e(error)
    note = f"<p>{e(explain.PUBLIC_TEXTS['kept_note'])}</p>" if kept else ""
    return (f"<div class=\"note error\" role=\"alert\" id=\"problem\" tabindex=\"-1\"><strong>{e(title)}</strong>"
            f"<p>{message}</p>{note}</div>")


def _strip(settled=False):
    def cues(rows):
        return "".join(f"<i class=\"cue\" style=\"--x:{x};--w:{w}\"><span>{e(words)}</span></i>" for x, w, words in rows)
    offset = "1.5%" if settled else "7%"
    return (f"<figure class=\"strip\" role=\"img\" aria-label=\"{e(explain.PUBLIC_TEXTS['strip'])}\" data-strip "
            "data-seconds=\"0.35\"><div class=\"strip-head\" aria-hidden=\"true\">"
            "<span>00:41:07,200</span><span class=\"readout\">"
            + ("" if settled else "<span class=\"r-off\">+2.45 s</span>")
            + "<span class=\"r-on\">in sync</span><span data-readout hidden></span></span></div>"
            "<div class=\"lanes\" aria-hidden=\"true\"><span class=\"lane-label\">the file's own track</span>"
            f"<div class=\"lane\">{cues(_REF_CUES)}</div><span class=\"lane-label\">your subtitles</span>"
            f"<div class=\"lane yours\"><div class=\"slide\" style=\"--from:{offset}\"><div class=\"nudge\">"
            f"{cues(_YOUR_CUES)}</div></div></div><div class=\"ruler\"></div><span class=\"playhead\"></span></div></figure>")


def language_names(codes):
    return ", ".join(_NAMES.get(c, c) for c in codes)


# ------------------------------------------------------------------------------------------- sign-up

def signup_page(settings, error=None, field=None, order=None):
    """The start page. After a problem, the summary comes first and the languages keep their order; the link, the
    password and the invite code are never sent back."""
    order = ["eng"] if order is None else order
    first, *rest = explain.PUBLIC_HEADLINE
    headline = (f"{e(first)} <span class=\"hand\">{e(rest[0])}{_HAND}</span> {e(rest[1])}")
    parts = []
    number = 1
    if settings.invite:
        parts.append(f"<fieldset><legend><span class=\"no\">{number:02d}</span>Your invite</legend>"
                     + _field("invite", "Invite code", extra="required autocomplete=\"off\" spellcheck=\"false\"",
                              error=error if field == "invite" else None) + "</fieldset>")
        number += 1
    parts.append(f"<fieldset><legend><span class=\"no\">{number:02d}</span>Your AIOStreams</legend>"
                 + _field("link", "Your AIOStreams link", "url", extra="required autocomplete=\"off\" spellcheck=\"false\" "
                          "inputmode=\"url\" placeholder=\"https://…/stremio/…/manifest.json\"",
                          error=error if field == "link" else None)
                 + _field("password", "Your AIOStreams password", "password", extra="required autocomplete=\"off\"",
                          error=error if field == "password" else None, reveal=True)
                 + _check("keep_password", "Keep my password, so AIOSubtitles follows my config by itself", checked=False)
                 + "</fieldset>")
    parts.append(_languages_fieldset(number + 1, order, error if field == "languages" else None))
    parts.append(f"<fieldset><legend><span class=\"no\">{number + 2:02d}</span>Your AIOStreams config</legend>"
                 + _check("add_addon", "Add AIOSubtitles to my AIOStreams config")
                 + _check("clean_up", "Remove the subtitle addons AIOSubtitles now searches itself", checked=False)
                 + "</fieldset>")
    before = "".join(f"<li><span>{e(line)}</span></li>" for line in explain.PUBLIC_BEFORE)
    tries = explain.PUBLIC_TEXTS["tries"].format(n=settings.signups_per_hour)
    fields = ("link", "password", "languages") + (("invite",) if settings.invite else ())
    body = (_problem(error, field, "Sign-up didn't go through", fields, kept=True)
            + "<div class=\"layout\"><div class=\"rail\"><div class=\"enter\">"
            "<p class=\"kicker\" style=\"--i:0\">Subtitle sync for AIOStreams</p>"
            f"<h1 class=\"display\" style=\"--i:1\">{headline}</h1><div style=\"--i:2\">{_strip()}</div>"
            f"<p class=\"lede\" style=\"--i:3\">{e(explain.PUBLIC_TEXTS['intro'])}</p></div>"
            f"<section class=\"notes rise\" aria-labelledby=\"before-h\"><h2 id=\"before-h\">Before you start</h2>"
            f"<ol>{before}</ol><p class=\"help\">{e(tries)}</p></section></div>"
            "<form class=\"panel rise\" method=\"post\" action=\"/signup\">" + "".join(parts)
            + "<div class=\"submit\"><button class=\"btn\" type=\"submit\">Sign up <span class=\"arrow\" "
              f"aria-hidden=\"true\">→</span></button><p class=\"help\">{e(explain.PUBLIC_TEXTS['password_once'])} What "
              "AIOSubtitles reads and keeps: <a href=\"/privacy\">Privacy</a>.</p></div></form></div>")
    return _page("AIOSubtitles", body)


def _settings_link(manifest_url):
    return manifest_url[: -len("manifest.json")] + "configure"


def _link_box(manifest_url, help_text):
    return (f"<label for=\"personal\">Your personal addon link</label><textarea id=\"personal\" readonly rows=\"3\" "
            f"spellcheck=\"false\" aria-describedby=\"personal-help\">{e(manifest_url)}</textarea>"
            f"{_help('personal', help_text)}<div class=\"actions\"><button type=\"button\" class=\"btn quiet small\" "
            "data-copy=\"personal\" data-done=\"Copied\" hidden>Copy link</button>"
            "<span class=\"sr\" id=\"personal-status\" role=\"status\"></span></div>")


def _manual_steps():
    return "<ol class=\"steps\">" + "".join(f"<li><span>{e(s)}</span></li>" for s in explain.PUBLIC_MANUAL_STEPS) + "</ol>"


def result_page(result):
    done = [explain.PUBLIC_TEXTS["made_config"]]
    if result.added:
        done += [f"Changed in your AIOStreams config: {c}" for c in result.changes] or \
                ["AIOSubtitles is in your AIOStreams config already."]
    if result.languages:
        done.append(explain.PUBLIC_TEXTS["languages_chosen"].format(names=language_names(result.languages)))
    items = "".join(f"<li style=\"--i:{i}\">{_TICK}<span>{e(text)}</span></li>" for i, text in enumerate(done))
    left = ""
    if not result.added:
        left = ("<div class=\"note warn\"><strong>One step left</strong>"
                + (f"<p>{e(explain.PUBLIC_TEXTS['not_added'])}</p>" if result.manual else "")
                + "<p>Add AIOSubtitles to your AIOStreams config yourself:</p></div>" + _manual_steps())
    warning = ""
    if result.warning:
        head, sep, rest = result.warning.partition(": ")
        warning = (_note("warn", rest[:1].upper() + rest[1:], head, "status") if sep
                   else _note("warn", result.warning, role="status"))
    body = ("<div class=\"layout\"><div class=\"rail enter\"><p class=\"kicker\" style=\"--i:0\">All set</p>"
            "<h1 class=\"display\" style=\"--i:1\">You're signed up</h1>"
            f"<div style=\"--i:2\">{_strip(settled=True)}</div></div>"
            f"<div class=\"stack\"><section class=\"panel\">{_link_box(result.manifest_url, explain.PUBLIC_TEXTS['signed_up'])}"
            f"</section>{warning}<section class=\"panel rise\" aria-labelledby=\"done-h\"><h2 id=\"done-h\">Done on your "
            f"AIOStreams</h2><ul class=\"done\">{items}</ul>{left}</section>"
            "<section class=\"panel rise\" aria-labelledby=\"next-h\"><h2 id=\"next-h\">What happens next</h2>"
            f"<p>{e(explain.PUBLIC_TEXTS['next'])}</p><div class=\"actions\"><a class=\"btn\" "
            f"href=\"{e(_settings_link(result.manifest_url))}\">Open your settings page <span class=\"arrow\" "
            f"aria-hidden=\"true\">→</span></a></div><p class=\"help\">{e(explain.PUBLIC_TEXTS['settings_link'])}</p>"
            "</section></div></div>")
    return _page("Signed up · AIOSubtitles", body, personal=True)


# ------------------------------------------------------------------------------------------- settings

def _megabytes(nbytes):
    """A read amount: one decimal under 10 MB (at least 0.1 when anything was read), whole MB from 10 MB."""
    mb = nbytes / (1024 * 1024)
    if nbytes <= 0:
        return "0"
    return f"{max(mb, 0.1):.1f}" if mb < 10 else f"{mb:.0f}"


def reset_in(now):
    """How long until 00:00 UTC, in plain words: "in 7 h 12 min", "in 1 min", "in 24 h"."""
    left = 86400 - int(now) % 86400
    minutes = -(-left // 60)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"in {minutes} min"
    return f"in {hours} h" + (f" {minutes} min" if minutes else "")


def _share(used, limit):
    if used <= 0 or limit <= 0:
        return 0
    return max(0.015, min(1.0, used / limit))


DONE = ("languages", "refreshed", "refreshed-rd", "refreshed-manual", "forgot")


def done_message(code, record):
    """(message, notice) for the page shown after a save: fixed texts only, chosen by a known code."""
    texts = explain.PUBLIC_TEXTS
    if code == "languages":
        return texts["languages_saved"].format(names=language_names(record["languages"])), None
    if code == "refreshed":
        return texts["refreshed"], None
    if code == "refreshed-rd":
        return texts["refreshed"], explain.PUBLIC_REAL_DEBRID_WARNING
    if code == "refreshed-manual":
        return None, " ".join((texts["refresh_manual"],) + explain.PUBLIC_MANUAL_STEPS)
    if code == "forgot":
        return texts["forgot"], None
    return None, None


def days_ago(then, now):
    """When something happened, in plain words: "today", "yesterday", "5 days ago"; "at sign-up" when unknown."""
    if not then:
        return "at sign-up"
    days = int(now // 86400) - int(then // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def _state_note(record, state):
    """What the daily check found (or the live reads-refused state), as a note at the top of the settings page."""
    state = state or record.get("state")
    if state not in explain.PUBLIC_STATES:
        return ""
    title, text, action = explain.PUBLIC_STATES[state]
    text = text.replace("{detail}", record.get("state_detail") or "")
    return _note("warn", text, title, "status")


def settings_page(settings, record, usage, manifest_url, message=None, error=None, notice=None, now=0, field=None,
                  state=None):
    files_limit, bytes_limit = settings.user_files_per_day, settings.user_daily_bytes
    full = usage["files"] >= files_limit or usage["bytes"] >= bytes_limit
    limit_mb = bytes_limit / (1024 * 1024)
    gauges = (
        f"<div class=\"gauge{' full' if usage['files'] >= files_limit else ''}\"><p>{usage['files']} of {files_limit} "
        f"new files</p><div class=\"track\" aria-hidden=\"true\"><i style=\"--v:{_share(usage['files'], files_limit):.3f}\">"
        f"</i></div></div><div class=\"gauge{' full' if usage['bytes'] >= bytes_limit else ''}\"><p>"
        f"{_megabytes(usage['bytes'])} MB of {limit_mb:.0f} MB read</p><div class=\"track\" aria-hidden=\"true\">"
        f"<i style=\"--v:{_share(usage['bytes'], bytes_limit):.3f}\"></i></div></div>")
    limit = _note("warn", explain.PUBLIC_TEXTS["limit_reached"], "Today's limit is reached") if full else ""
    status = (_note("ok", message, role="status") + _note("warn", notice, role="status") + _state_note(record, state)
              + _problem(error, field, "That didn't go through", ("refresh", "new_link", "languages")))
    kept = bool(record.get("auto_password"))
    copied = explain.PUBLIC_TEXTS["copied_auto" if kept else "copied"].format(when=days_ago(record.get("synced_at"), now))
    forget = ("<form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" "
              "value=\"forget-password\"><div class=\"actions\"><button class=\"btn quiet\" type=\"submit\" "
              "aria-describedby=\"forget_password-help\">Stop keeping my password</button></div>"
              + _help("forget_password") + "</form>") if kept else ""
    body = (status
            + "<div class=\"layout settled\"><div class=\"rail enter\"><p class=\"kicker\" style=\"--i:0\">Your settings</p>"
            "<h1 style=\"--i:1\">Your AIOSubtitles</h1>"
            f"<section class=\"panel\" style=\"--i:2;margin-top:var(--gap)\">"
            f"{_link_box(manifest_url, explain.PUBLIC_TEXTS['settings_lead'])}</section>"
            f"<section class=\"panel\" style=\"--i:3;margin-top:var(--gap)\" aria-labelledby=\"today-h\"><h2 id=\"today-h\">"
            f"Today</h2>{limit}{gauges}<p class=\"help\">"
            f"{e(explain.PUBLIC_TEXTS['reset'].format(when=reset_in(now)))}</p></section></div>"
            "<div class=\"stack\"><section class=\"panel rise\" aria-labelledby=\"languages-h\"><h2 id=\"languages-h\" "
            "class=\"sr\">Languages</h2><form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" "
            "value=\"languages\">" + _languages_fieldset(None, record["languages"], error if field == "languages" else None)
            + "<div class=\"actions\"><button class=\"btn\" type=\"submit\">Save languages</button></div></form></section>"
            "<section class=\"panel rise\" aria-labelledby=\"refresh-h\"><h2 id=\"refresh-h\">Refresh after changing your "
            f"AIOStreams config</h2><p class=\"help\">{e(copied)}</p><form method=\"post\" action=\"configure\">"
            "<input type=\"hidden\" name=\"action\" value=\"refresh\">"
            + _field("password", "Your AIOStreams password", "password", extra="required autocomplete=\"off\"",
                     help="refresh", error=error if field == "refresh" else None, reveal=True)
            + _field("link", "A new AIOStreams link (optional)", "url", extra="autocomplete=\"off\" spellcheck=\"false\" "
                     "inputmode=\"url\" placeholder=\"https://…/stremio/…/manifest.json\"", help="new_link",
                     error=error if field == "new_link" else None)
            + _check("keep_password", "Keep my password, so AIOSubtitles follows my config by itself", checked=kept)
            + "<input type=\"hidden\" name=\"add_addon\" value=\"on\"><div class=\"actions\"><button class=\"btn\" "
              "type=\"submit\">Refresh</button></div></form>" + forget + "</section>"
            "<section class=\"panel cut rise\" aria-labelledby=\"delete-h\"><h2 id=\"delete-h\">Delete my data</h2>"
            f"<p class=\"help\" id=\"delete-help\">{e(explain.PUBLIC_FIELDS['delete'])}</p><form method=\"post\" "
            "action=\"configure\"><input type=\"hidden\" name=\"action\" value=\"delete-confirm\"><div class=\"actions\">"
            "<button type=\"submit\" class=\"btn danger\" aria-describedby=\"delete-help\">Delete my data…</button></div>"
            "</form></section></div></div>")
    return _page("Settings · AIOSubtitles", body, personal=True)


# ------------------------------------------------------------------------------------------- delete, privacy, errors

def delete_confirm_page(error=None, field=None):
    gone = "".join(f"<li><span>{e(line)}</span></li>" for line in explain.PUBLIC_DELETE_LIST)
    body = (_problem(error, field, "Nothing was deleted", ("delete",)) + "<div class=\"letter enter\"><p class=\"kicker\" "
            "style=\"--i:0\">Delete</p><h1 style=\"--i:1\">Delete your data?</h1><div class=\"panel\" style=\"--i:2\">"
            f"<p><strong>{e(explain.PUBLIC_TEXTS['undone'])}</strong></p><ol class=\"steps\">{gone}</ol>"
            "<form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" value=\"delete\">"
            + _field("password", "Your AIOStreams password (optional)", "password", extra="autocomplete=\"off\"",
                     help="delete", error=error if field == "delete" else None, reveal=True)
            + "<div class=\"actions\"><button type=\"submit\" class=\"btn danger\">Delete my data</button>"
              "<a class=\"btn quiet\" href=\"configure\">Keep my data</a></div></form></div></div>")
    return _page("Delete · AIOSubtitles", body, personal=True)


def deleted_page(outcome):
    parts = [_note("ok", explain.PUBLIC_TEXTS["deleted"], role="status")]
    left = []
    if not outcome.get("sync_removed"):
        left.append(explain.PUBLIC_TEXTS["sync_left"])
    if not outcome.get("addon_removed"):
        left.append(explain.PUBLIC_TEXTS["addon_left"])
    if left:
        parts.append("<div class=\"note warn\"><strong>One step left</strong>"
                     + "".join(f"<p>{e(text)}</p>" for text in left) + "</div>")
    body = ("<div class=\"letter enter\"><p class=\"kicker\" style=\"--i:0\">Deleted</p><h1 style=\"--i:1\">Your data is "
            f"deleted</h1><div style=\"--i:2\">{''.join(parts)}<p><a href=\"/\">Sign up again</a> to use AIOSubtitles "
            "again.</p></div></div>")
    return _page("Deleted · AIOSubtitles", body, personal=True)


def privacy_page(settings):
    sections = "".join(
        f"<section class=\"panel rise\" aria-labelledby=\"privacy-{i}\"><h2 id=\"privacy-{i}\">{e(head)}</h2>"
        f"<p>{e(text[:1].upper() + text[1:])}</p></section>" for i, (head, _, text) in enumerate(explain.PRIVACY_PARTS))
    body = ("<div class=\"letter\"><div class=\"enter\"><p class=\"kicker\" style=\"--i:0\">Privacy</p>"
            "<h1 style=\"--i:1\">What AIOSubtitles reads and keeps</h1></div><div class=\"stack\">" + sections
            + "<section class=\"panel rise\" aria-labelledby=\"privacy-q\"><h2 id=\"privacy-q\">Questions</h2>"
              f"<p><a href=\"{e(explain.CONTACT)}\">{e(explain.CONTACT)}</a></p></section></div></div>")
    return _page("Privacy · AIOSubtitles", body)


def not_found_page():
    body = ("<div class=\"letter enter\"><p class=\"kicker\" style=\"--i:0\">Not found</p><h1 style=\"--i:1\">This page "
            f"doesn't exist</h1><div class=\"panel\" style=\"--i:2\"><p>{e(explain.PUBLIC_TEXTS['not_found'])}</p>"
            "<p><a href=\"/\">Sign up</a> to start over.</p></div></div>")
    return _page("Not found · AIOSubtitles", body)


def error_page(message):
    body = ("<div class=\"letter enter\"><h1 style=\"--i:0\">Something went wrong</h1>"
            f"<div style=\"--i:1\">{_note('error', message, role='alert')}<p><a href=\"/\">Back to the start page</a></p>"
            "</div></div>")
    return _page("AIOSubtitles", body)
