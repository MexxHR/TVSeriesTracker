"""Loopback-only local test mode. Never deploy this fake dispatcher."""
import tempfile
from wsgiref.simple_server import make_server

from service import App, RequestStore


class FakeDispatcher:
    def dispatch(self, tmdb_id):
        print(f"Fake dispatch accepted TMDB ID {tmdb_id}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        store = RequestStore(directory + "/requests.db", b"local-test-only-key")
        with make_server("127.0.0.1", 8765, App(store, FakeDispatcher())) as server:
            print("Local fake request endpoint: http://127.0.0.1:8765")
            server.serve_forever()
