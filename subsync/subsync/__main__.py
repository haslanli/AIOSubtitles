"""python -m subsync: the addon server and the worker in one process."""
import logging
import signal
import sys
import threading

from .addon import App, serve
from .settings import Settings
from .sources import Aios
from .store import Store
from .worker import Safety, Worker


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = Settings()
    missing = s.missing()
    if missing:
        logging.getLogger("subsync").error("not started: set %s (./setup writes them)", ", ".join(missing))
        sys.exit(2)
    store = Store(s.data, s.cap_bytes)
    safety = Safety(backoff_s=s.backoff_s)
    aios = Aios(s.source_manifest, own_bases=(s.public_base,), skip_addons=s.skip_addons, size_slack=s.size_slack,
                password=s.source_password)

    def vad_factory():
        from .vad import SileroVad
        return SileroVad()

    from .extra import ExtraSources
    extra = ExtraSources(s) if (s.extra_sources or s.subdl_key or s.wyzie_key) else None
    worker = Worker(s, store, aios, safety, vad_factory=vad_factory, extra=extra)
    worker.start()
    server = serve(App(s, store, worker, safety), s.port)
    def stop(*_):
        worker.stop_flag = True
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    logging.getLogger("subsync").info("listening on %d; languages %s; audio route %s", s.port, ",".join(s.languages),
                                      "on" if s.audio_route else "off")
    server.serve_forever()


if __name__ == "__main__":
    main()
