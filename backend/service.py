"""Provider-neutral, anonymous monitored-series request endpoint."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib import error, parse, request

MAX_BODY = 128
MAX_TMDB_RESPONSE = 512 * 1024
WINDOW_SECONDS = 3600
COOLDOWN_SECONDS = 86400


class DispatchFailure(Exception):
    def __init__(self, status: int):
        self.status = status


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Config:
    repository: str
    token: str
    ref: str = "main"

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid TARGET_REPOSITORY")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.ref) or ".." in self.ref:
            raise ValueError("Invalid GITHUB_REF")
        if not self.token.strip():
            raise ValueError("Missing GITHUB_TOKEN")


class GitHubDispatcher:
    def __init__(self, config: Config, opener=None, sleep=time.sleep):
        config.validate()
        self.config = config
        self.opener = opener or request.urlopen
        self.sleep = sleep

    def dispatch(self, tmdb_id: int):
        url = ("https://api.github.com/repos/" + self.config.repository +
               "/actions/workflows/process-series-request.yml/dispatches")
        body = json.dumps({"ref": self.config.ref, "inputs": {"tmdb_id": str(tmdb_id)}}).encode()
        req = request.Request(url, data=body, method="POST", headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.config.token,
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tv-series-monitoring-request/1",
        })
        for attempt in range(3):
            try:
                with self.opener(req, timeout=8) as response:
                    if response.status in (200, 204):
                        return
                    status = response.status
            except error.HTTPError as exc:
                status = exc.code
                exc.close()
            except (error.URLError, TimeoutError, OSError):
                status = 503
            if status not in (429, 500, 502, 503, 504) or attempt == 2:
                raise DispatchFailure(status)
            self.sleep((1, 3)[attempt])


class RequestStore:
    """One shared durable SQLite file; BEGIN IMMEDIATE serializes all replicas."""
    def __init__(self, path: str, ip_key: bytes, clock=time.time, per_client=10, global_limit=200,
                 tmdb_per_client=120, tmdb_global_limit=3000):
        if not path or path == ":memory:" or not ip_key:
            raise ValueError("Request storage and IP HMAC key are required")
        self.path, self.ip_key, self.clock = path, ip_key, clock
        self.per_client, self.global_limit = per_client, global_limit
        self.tmdb_per_client, self.tmdb_global_limit = tmdb_per_client, tmdb_global_limit
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (tmdb_id INTEGER PRIMARY KEY, at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS rate_events (ip_hash TEXT NOT NULL, at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS rate_at ON rate_events(at)")
            db.execute("CREATE TABLE IF NOT EXISTS tmdb_rate_events (ip_hash TEXT NOT NULL, at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS tmdb_rate_at ON tmdb_rate_events(at)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, tmdb_id: int, dispatcher):
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM requests WHERE at < ?", (now - COOLDOWN_SECONDS,))
            if db.execute("SELECT 1 FROM requests WHERE tmdb_id=?", (tmdb_id,)).fetchone():
                return 202, "already_queued"
            try:
                dispatcher.dispatch(tmdb_id)
            except DispatchFailure as exc:
                # Keep the rate charge, but allow a later valid dispatch attempt.
                db.commit()
                return (503 if exc.status in (429, 500, 502, 503, 504) else 424), "dispatch_unavailable"
            db.execute("INSERT INTO requests VALUES (?, ?)", (tmdb_id, now))
            return 202, "queued"

    def _charge(self, db, ip, now, kind):
        if kind == "tmdb":
            table = "tmdb_rate_events"
            per_client, global_limit = self.tmdb_per_client, self.tmdb_global_limit
        elif kind == "dispatch":
            table = "rate_events"
            per_client, global_limit = self.per_client, self.global_limit
        else:
            raise ValueError("Unknown rate-limit class")
        day = int(now // 86400)
        hash_input = f"{day}:{ip}" if kind == "dispatch" else f"{day}:tmdb:{ip}"
        digest = hmac.new(self.ip_key, hash_input.encode(), hashlib.sha256).hexdigest()
        db.execute(f"DELETE FROM {table} WHERE at < ?", (now - WINDOW_SECONDS,))
        total, first_global = db.execute(f"SELECT COUNT(*), MIN(at) FROM {table}").fetchone()
        own, first_client = db.execute(
            f"SELECT COUNT(*), MIN(at) FROM {table} WHERE ip_hash=?", (digest,)).fetchone()
        if total >= global_limit or own >= per_client:
            expiry = max(first_global + WINDOW_SECONDS if total >= global_limit else now,
                         first_client + WINDOW_SECONDS if own >= per_client else now)
            return False, max(1, math.ceil(expiry - now))
        db.execute(f"INSERT INTO {table} VALUES (?, ?)", (digest, now))
        return True, None

    def charge(self, ip, kind="dispatch"):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._charge(db, ip, self.clock(), kind)


class TmdbProxy:
    """Fixed TMDB metadata routes; response never becomes official lifecycle data."""
    def __init__(self, token: str, opener=None):
        self.token = token
        self.opener = opener or request.urlopen

    def get(self, route: str, query: dict):
        if not self.token:
            return 503, b'{"error":"tmdb_unavailable"}'
        language = query.get("language", [""])
        if len(language) != 1 or language[0] not in ("hr-HR", "en-US"):
            return 400, b'{"error":"invalid_language"}'
        if route == "/v1/tmdb/search":
            terms = query.get("query", [])
            if len(terms) != 1 or not 2 <= len(terms[0].strip()) <= 80 or set(query) != {"query", "language"}:
                return 400, b'{"error":"invalid_query"}'
            path = "search/tv?" + parse.urlencode({"query": terms[0].strip(), "language": language[0],
                                                    "include_adult": "false"})
        else:
            match = re.fullmatch(r"/v1/tmdb/series/([1-9][0-9]*)", route)
            if not match or int(match.group(1)) > 2147483647 or set(query) != {"language"}:
                return 400, b'{"error":"invalid_tmdb_id"}'
            path = "tv/" + match.group(1) + "?" + parse.urlencode({"language": language[0]})
        req = request.Request("https://api.themoviedb.org/3/" + path, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json",
            "User-Agent": "tv-series-metadata-proxy/1"})
        try:
            with self.opener(req, timeout=8) as response:
                body = response.read(MAX_TMDB_RESPONSE + 1)
                if len(body) > MAX_TMDB_RESPONSE:
                    return 503, b'{"error":"upstream_too_large"}'
                json.loads(body)
                return 200, body
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            return (404 if status == 404 else 503), b'{"error":"tmdb_unavailable"}'
        except (error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
            return 503, b'{"error":"tmdb_unavailable"}'


class App:
    def __init__(self, store: RequestStore, dispatcher, tmdb_proxy=None, trusted_proxy_ips=()):
        self.store, self.dispatcher, self.tmdb_proxy = store, dispatcher, tmdb_proxy
        self.trusted_proxy_ips = set(trusted_proxy_ips)

    def client_ip(self, environ):
        peer = environ.get("REMOTE_ADDR", "unknown")
        if peer not in self.trusted_proxy_ips:
            return peer
        forwarded = environ.get("HTTP_X_FORWARDED_FOR", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return peer

    def __call__(self, environ, start_response):
        def reply(status, data, retry_after=None):
            labels = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found",
                      405: "Method Not Allowed", 413: "Content Too Large",
                      415: "Unsupported Media Type", 424: "Failed Dependency", 429: "Too Many Requests",
                      502: "Bad Gateway", 503: "Service Unavailable"}
            body = json.dumps(data, separators=(",", ":")).encode()
            headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                       ("Cache-Control", "no-store")]
            if retry_after is not None:
                headers.append(("Retry-After", str(retry_after)))
            start_response(f"{status} {labels[status]}", headers)
            return [body]

        path = environ.get("PATH_INFO", "")
        if path.startswith("/v1/tmdb/"):
            try:
                allowed, retry_after = self.store.charge(self.client_ip(environ), "tmdb")
                if not allowed:
                    return reply(429, {"error": "rate_limited"}, retry_after)
            except (sqlite3.Error, OSError):
                return reply(503, {"error": "storage_unavailable"})
            if environ.get("REQUEST_METHOD") != "GET":
                return reply(405, {"error": "method_not_allowed"})
            if self.tmdb_proxy is None:
                return reply(503, {"error": "tmdb_unavailable"})
            query_string = environ.get("QUERY_STRING", "")
            if len(query_string) > 256:
                return reply(413, {"error": "query_size"})
            query = parse.parse_qs(query_string, keep_blank_values=True)
            try:
                status, body = self.tmdb_proxy.get(path, query)
            except (sqlite3.Error, OSError):
                return reply(503, {"error": "storage_unavailable"})
            start_response(f"{status} " + {200: "OK", 400: "Bad Request", 404: "Not Found", 503: "Service Unavailable"}[status],
                           [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
            return [body]
        if path != "/v1/series-requests":
            return reply(404, {"error": "not_found"})
        try:
            allowed, retry_after = self.store.charge(self.client_ip(environ))
            if not allowed:
                return reply(429, {"error": "rate_limited"}, retry_after)
        except (sqlite3.Error, OSError):
            return reply(503, {"error": "storage_unavailable"})
        if environ.get("REQUEST_METHOD") != "POST":
            return reply(405, {"error": "method_not_allowed"})
        if environ.get("CONTENT_TYPE", "").split(";")[0].lower() != "application/json":
            return reply(415, {"error": "content_type"})
        try:
            length = int(environ.get("CONTENT_LENGTH", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return reply(413, {"error": "body_size"})
        raw = environ["wsgi.input"].read(length)
        if len(raw) != length:
            return reply(400, {"error": "invalid_body"})
        try:
            data = json.loads(raw, object_pairs_hook=strict_object)
        except (UnicodeError, ValueError):
            return reply(400, {"error": "invalid_json"})
        if (not isinstance(data, dict) or set(data) != {"tmdbId"} or
                type(data["tmdbId"]) is not int or not 0 < data["tmdbId"] <= 2147483647):
            return reply(400, {"error": "invalid_tmdb_id"})
        # The trusted ingress must set REMOTE_ADDR; do not trust client headers.
        try:
            status, state = self.store.submit(data["tmdbId"], self.dispatcher)
        except (sqlite3.Error, OSError):
            return reply(503, {"error": "storage_unavailable"})
        if status == 202:
            return reply(status, {"accepted": True, "tmdbId": data["tmdbId"], "state": state})
        return reply(status, {"error": state})


def from_environment():
    config = Config(os.getenv("TARGET_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", ""),
                    os.getenv("GITHUB_REF", "main"))
    dispatcher = GitHubDispatcher(config)
    store = RequestStore(os.getenv("REQUEST_DB_PATH", ""),
                         os.getenv("CLIENT_IP_HMAC_KEY", "").encode())
    return App(store, dispatcher, TmdbProxy(os.getenv("TMDB_API_TOKEN", "")),
               os.getenv("TRUSTED_PROXY_IPS", "").split(",") if os.getenv("TRUSTED_PROXY_IPS") else ())
