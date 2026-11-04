"""Serves the real dashboard on the demo world, in a fresh folder of its own.

    .venv/bin/python -m demo.serve [--lang fa]

opens the world as it stands before the film, for a look or a screenshot. demo/film.py uses `start` to play it.
"""
import argparse
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from kandu.config import load_config
from kandu.dashboard import Api, make_handler
from kandu.state import StateStore

from .world import World

CONFIG = Path(__file__).with_name("config")
TOKEN = "demo"


def start(lang, root=None):
    """Returns the world and the page's address. A press of a cycle button plays the world's next step."""
    root = Path(root or tempfile.mkdtemp(prefix="kandu-demo-"))
    (root / "logs").mkdir(parents=True, exist_ok=True)
    store = StateStore(root / "state")
    world = World(store, root / "logs", lang)
    world.seed()
    config = load_config(CONFIG)
    config.settings.language = lang

    def cycle():
        if world.wakes:
            threading.Thread(target=world.wakes.pop(0), daemon=True).start()
        return True

    # The world's GitHub is in memory, so the page may read it as often as it likes.
    api = Api(config, store, world.github, root / "logs", TOKEN, clock=lambda: time.monotonic() * 1000,
              start_cycle=cycle, local_dir=str(CONFIG), timer_on=lambda: True, restart=lambda config: None)
    server = ThreadingHTTPServer(("127.0.0.1", config.settings.dashboard.port), make_handler(api))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return world, f"http://127.0.0.1:{config.settings.dashboard.port}/", server


def main():
    parser = argparse.ArgumentParser(description="Kandu's dashboard on the demo world.")
    parser.add_argument("--lang", default="en", choices=("en", "fa"))
    args = parser.parse_args()
    world, url, server = start(args.lang)
    print(f"demo: {url} (control token: {TOKEN})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
