"""python -m subsync: the addon server and the worker in one process."""
import logging
import signal
import sys
import threading
import time

from .addon import App, serve
from .settings import Settings
from .sources import Aios
from .store import Store
from .worker import Safety, StoreUsenetCounts, UsenetGate, Worker


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = Settings()
    missing = s.missing()
    if missing:
        logging.getLogger("subsync").error("not started: set %s%s", ", ".join(missing),
                                           "" if s.public else " (./setup writes them)")
        sys.exit(2)
    for conflict in s.conflicts():
        logging.getLogger("subsync").error("not started: %s", conflict)
    if s.conflicts():
        sys.exit(2)
    if s.public:
        return main_public(s)
    store = Store(s.data, s.cap_bytes)
    safety = Safety(backoff_s=s.backoff_s)
    aios = Aios(s.source_manifest, own_bases=(s.public_base,), skip_addons=s.skip_addons, size_slack=s.size_slack,
                password=s.source_password)

    def vad_factory():
        from .vad import SileroVad
        return SileroVad()

    from .extra import make_sources
    extra = make_sources(s)
    usenet = gate = None
    if s.usenet and s.usenet_manifest and s.usenet_listings_per_day > 0:   #
        usenet = Aios(s.usenet_manifest, own_bases=(s.public_base,), skip_addons=s.skip_addons, size_slack=s.size_slack,
                      password=s.usenet_password)
        gate = UsenetGate(store, s.usenet_listings_per_day, StoreUsenetCounts(store))
    worker = Worker(s, store, aios, safety, vad_factory=vad_factory, extra=extra, usenet=usenet, usenet_gate=gate)
    worker.start()
    server = serve(App(s, store, worker, safety), s.port)
    def stop(*_):
        worker.stop_flag = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    logging.getLogger("subsync").info("listening on %d; languages %s; audio route %s; Usenet %s", s.port,
                                      ",".join(s.languages), "on" if s.audio_route else "off",
                                      f"on, {s.usenet_listings_per_day} listings a day" if usenet else "off")
    server.serve_forever()



def main_public(s):
    """Public mode: the sign-up pages and every person's own service in one process (public.py)."""
    import os
    from . import net
    from .public import Pool, PublicService, serve_public
    from .users import Users
    net.configure(public_only=True)
    # The shared store keeps how each copy was made, never a subtitle's text; texts an earlier version kept go
    store = Store(s.data, s.cap_bytes, keep_texts=False)
    store.drop_texts()
    users = Users(os.path.join(s.data, "users.sqlite3"), s.secret_bytes())
    env = dict(os.environ)
    pool = Pool(s, env, store, users)
    server = serve_public(PublicService(s, env, store, users, pool), s.port)
    log = logging.getLogger("subsync")

    def housekeeping():
        last_expire = 0.0
        while True:
            time.sleep(60)
            try:
                pool.housekeeping()
                if time.time() - last_expire > 3600:
                    last_expire = time.time()
                    store.expire()
            except Exception as e:   # never let housekeeping stop the service
                log.warning("housekeeping failed: %s", type(e).__name__)
    threading.Thread(target=housekeeping, daemon=True, name="subsync-housekeeping").start()

    def stop(*_):
        pool.close()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    log.info("public mode: listening on %d; up to %d people, %d active at once, %d file%s at once; trusted proxy %s",
             s.port, s.max_users, s.active_workers, pool.turn.places, "" if pool.turn.places == 1 else "s",
             ", ".join(s.trusted_proxy) or "none")
    server.serve_forever()


if __name__ == "__main__":
    main()
