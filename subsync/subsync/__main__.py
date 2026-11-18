"""python -m subsync: the addon server and the worker in one process."""
import logging
import signal
import sys
import threading
import time

from . import explain
from .addon import App, serve
from .settings import Settings
from .sources import Aios
from .store import Store
from .worker import EXPIRE_EVERY_S, Safety, StoreUsenetCounts, UsenetGate, Worker

HOUSEKEEPING_EVERY_S = 60   # public mode's housekeeping runs this often (housekeeping_round)
log = logging.getLogger("subsync")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = Settings()
    missing = s.missing()
    if missing:
        log.error("not started: set %s%s", ", ".join(missing), "" if s.public else " (./setup writes them)")
        sys.exit(2)
    for conflict in s.conflicts():
        log.error("not started: %s", conflict)
    if s.conflicts():
        sys.exit(2)
    if s.public:
        return main_public(s)
    store = Store(s.data, s.cap_bytes)
    safety = Safety(backoff_s=s.backoff_s)
    aios = Aios.from_settings(s, s.source_manifest, s.source_password, s.public_base)

    def vad_factory():
        from .vad import SileroVad
        return SileroVad()

    from .extra import make_sources
    from .origlang import make_lookup
    extra = make_sources(s)
    lookup = make_lookup(s, store)
    usenet = gate = None
    if s.usenet and s.usenet_manifest and s.usenet_listings_per_day > 0:
        usenet = Aios.from_settings(s, s.usenet_manifest, s.usenet_password, s.public_base)
        gate = UsenetGate(store, s.usenet_listings_per_day, StoreUsenetCounts(store))
    worker = Worker(s, store, aios, safety, vad_factory=vad_factory, extra=extra, usenet=usenet, usenet_gate=gate,
                    lookup=lookup)
    worker.start()
    server = serve(App(s, store, worker, safety), s.port)

    def stop(*_):
        worker.stop()
        if lookup is not None:     # lookups still waiting are dropped, so stopping isn't held up by them
            lookup.close()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    log.info("%s", start_line(s, usenet is not None))
    server.serve_forever()


def _audio_limit(s):
    """The per-file audio limit as the start lines say it: as the pages do (explain), or "no limit"."""
    return explain.audio_limit_words(s) if s.audio_budget else "no limit"


def start_line(s, usenet):
    """The self-hosted service's start line: its languages, its one audio choice with its limits, and Usenet.
    One audio lane: one audio sync at a time."""
    choice = s.audio_choice
    if choice == "off":
        audio = "audio route off"
    else:
        opened = choice == "open" and s.user_opened_per_day > 0     # with none allowed it syncs on a pick
        audio = (f"audio route {'when opened' if opened else 'on pick'}, up to {_audio_limit(s)} a file, 1 at a time, "
                 f"{s.user_audio_per_day} a day" + (f" ({s.user_opened_per_day} opened)" if opened else ""))
    return (f"listening on {s.port}; languages {','.join(s.languages)}; {audio}; Usenet "
            + (f"on, {s.usenet_listings_per_day} listings a day" if usenet else "off")
            + f"; original language {_original_language(s)}")


# where titles' original languages come from, by explain.lookup_kind, as the start lines say it (never the key)
ORIGINAL_LANGUAGE_WORDS = {"off": "off", "tmdb": "from TMDB and Wikidata",
                           "personal": "from TMDB with each person's own key, and Wikidata",
                           "wikidata": "from Wikidata only"}


def _original_language(s):
    return ORIGINAL_LANGUAGE_WORDS[explain.lookup_kind(s)]


def public_store(s):
    """The shared store of public mode: it keeps how each copy was made, never a subtitle's text (texts an
    earlier version kept go), and waiting files and their original subtitles are each person's own (rows an
    earlier version kept by the file alone go)."""
    store = Store(s.data, s.cap_bytes, keep_texts=False)
    store.drop_texts()
    store.drop_shared_waiting()
    return store


def public_start_line(s, places, audio_places):
    """The public service's start line: its limits and whether the audio route is allowed, the answers each person may
    choose and its limits."""
    if s.audio_route:
        opened = s.user_opened_per_day
        answers = "off / on pick / when opened" if opened else "off / on pick"
        audio = (f"audio route allowed: each person {answers}, up to {_audio_limit(s)} a file, {audio_places} at a "
                 f"time, {s.user_audio_per_day} a person a day" + (f" ({opened} opened)" if opened else ""))
    else:
        audio = "audio route off"
    return (f"public mode: listening on {s.port}; up to {s.max_users} people, {s.active_workers} active at once, "
            f"{places} file{'' if places == 1 else 's'} at once; {audio}; trusted proxy "
            f"{', '.join(s.trusted_proxy) or 'none'}; original language {_original_language(s)}")


def housekeeping_round(pool, store, last_expire, now):
    """Public mode's housekeeping, every HOUSEKEEPING_EVERY_S: idle workers stop (Pool.housekeeping), and every
    EXPIRE_EVERY_S the shared store's old rows go (only here: the workers leave it to this). Neither failure skips the
    other or stops the service. Returns when the store's expiry last ran."""
    try:
        pool.housekeeping()
    except Exception as e:   # never let housekeeping stop the service
        log.warning("housekeeping failed: %s", type(e).__name__)
    if now - last_expire > EXPIRE_EVERY_S:
        last_expire = now
        try:
            store.expire()
        except Exception as e:
            log.warning("store expiry failed: %s", type(e).__name__)
    return last_expire


def main_public(s):
    """Public mode: the sign-up pages and every person's own service in one process (public.py)."""
    import os
    from . import net
    from .public import Pool, PublicService, serve_public
    from .users import Users
    net.configure(public_only=True)
    store = public_store(s)
    users = Users(os.path.join(s.data, "users.sqlite3"), s.secret_bytes())
    env = dict(os.environ)
    pool = Pool(s, env, store, users)
    server = serve_public(PublicService(s, env, store, users, pool), s.port)

    def housekeeping():
        last_expire = 0.0
        while True:
            time.sleep(HOUSEKEEPING_EVERY_S)
            last_expire = housekeeping_round(pool, store, last_expire, time.time())
    threading.Thread(target=housekeeping, daemon=True, name="subsync-housekeeping").start()

    def stop(*_):
        pool.close()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    log.info("%s", public_start_line(s, pool.turn.places, pool.audio_turn.places))
    server.serve_forever()


if __name__ == "__main__":
    main()
