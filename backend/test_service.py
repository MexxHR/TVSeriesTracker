import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error

from service import App, Config, DispatchFailure, GitHubDispatcher, RequestStore, TmdbProxy, WINDOW_SECONDS
from check_release_artifacts import PATTERNS


class FakeDispatcher:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def dispatch(self, tmdb_id):
        self.calls.append(tmdb_id)
        if self.failure:
            raise DispatchFailure(self.failure)


class Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class TmdbResponse(Response):
    status = 200

    def read(self, size):
        return b'{"results":[]}'


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [100000.0]
        self.store = RequestStore(str(Path(self.temp.name) / 'requests.db'), b'test-only-key',
                                  clock=lambda: self.now[0], per_client=3, global_limit=5)
        self.dispatcher = FakeDispatcher()
        self.app = App(self.store, self.dispatcher)

    def call(self, value, ip='127.0.0.1', path='/v1/series-requests', method='POST', content_type='application/json', length=None, query_string=''):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        env = {'PATH_INFO': path, 'REQUEST_METHOD': method, 'CONTENT_TYPE': content_type,
               'CONTENT_LENGTH': str(len(raw) if length is None else length),
               'wsgi.input': io.BytesIO(raw), 'REMOTE_ADDR': ip, 'QUERY_STRING': query_string}
        status = []
        headers = []
        body = b''.join(self.app(env, lambda code, response_headers: (status.append(code), headers.extend(response_headers))))
        self.last_headers = dict(headers)
        return int(status[0][:3]), json.loads(body)

    def tmdb_get(self, path='/v1/tmdb/search', query='sever', ip='127.0.0.1'):
        query_string = ('query=' + query + '&language=hr-HR' if path == '/v1/tmdb/search'
                        else 'language=hr-HR')
        return self.call({}, ip=ip, path=path, method='GET', query_string=query_string)

    def test_valid(self):
        self.assertEqual((202, {'accepted': True, 'tmdbId': 111110, 'state': 'queued'}),
                         self.call({'tmdbId': 111110}))
        self.assertEqual([111110], self.dispatcher.calls)

    def test_duplicate(self):
        self.call({'tmdbId': 1})
        self.assertEqual('already_queued', self.call({'tmdbId': 1})[1]['state'])
        self.assertEqual([1], self.dispatcher.calls)

    def test_cooldown_expired(self):
        self.call({'tmdbId': 1})
        self.now[0] += 86401
        self.assertEqual('queued', self.call({'tmdbId': 1})[1]['state'])
        self.assertEqual([1, 1], self.dispatcher.calls)

    def test_invalid_values(self):
        self.store.global_limit = 100
        for index, value in enumerate((0, -1, 1.5, '1', True, [], {}, None, 2147483648)):
            with self.subTest(value=value):
                self.assertEqual(400, self.call({'tmdbId': value}, ip=str(index))[0])
        self.assertEqual([], self.dispatcher.calls)

    def test_unknown_fields(self):
        self.assertEqual(400, self.call({'tmdbId': 1, 'status': 'RENEWED'})[0])
        self.assertEqual([], self.dispatcher.calls)

    def test_oversized(self):
        self.assertEqual(413, self.call(b' ' * 129)[0])

    def test_bad_json(self):
        self.assertEqual(400, self.call(b'{')[0])

    def test_duplicate_json_key(self):
        self.assertEqual(400, self.call(b'{"tmdbId":1,"tmdbId":2}')[0])

    def test_bad_length(self):
        self.assertEqual(413, self.call(b'{}', length=-1)[0])

    def test_content_type(self):
        self.assertEqual(415, self.call({'tmdbId': 1}, content_type='text/plain')[0])

    def test_method_and_path(self):
        self.assertEqual(405, self.call({}, method='GET')[0])
        self.assertEqual(404, self.call({}, path='/else')[0])

    def test_per_ip_limit(self):
        for value in (1, 2, 3):
            self.assertEqual(202, self.call({'tmdbId': value})[0])
        self.assertEqual(429, self.call({'tmdbId': 4})[0])
        self.assertEqual('3600', self.last_headers['Retry-After'])

    def test_invalid_requests_are_rate_limited(self):
        for _ in range(3):
            self.assertEqual(400, self.call({'tmdbId': 0})[0])
        self.assertEqual(429, self.call({'tmdbId': 0})[0])

    def test_global_limit(self):
        for value in range(1, 6):
            self.assertEqual(202, self.call({'tmdbId': value}, ip=str(value))[0])
        self.assertEqual(429, self.call({'tmdbId': 6}, ip='6')[0])

    def test_no_raw_ip_in_store(self):
        self.call({'tmdbId': 1}, ip='198.51.100.12')
        self.app.tmdb_proxy = TmdbProxy('test-token', opener=lambda req, timeout: TmdbResponse())
        self.tmdb_get(ip='198.51.100.12')
        self.assertNotIn(b'198.51.100.12', Path(self.store.path).read_bytes())

    def test_tmdb_typeahead_and_details_have_separate_quota(self):
        self.app.tmdb_proxy = TmdbProxy('test-token', opener=lambda req, timeout: TmdbResponse())
        self.store.per_client = 1
        self.store.global_limit = 1
        for query in ('sever', 'severance', 'dark', 'dark%20matter'):
            self.assertEqual(200, self.tmdb_get(query=query)[0])
        self.assertEqual(200, self.tmdb_get(path='/v1/tmdb/series/123')[0])
        self.assertEqual(202, self.call({'tmdbId': 123})[0])
        self.assertEqual(429, self.call({'tmdbId': 124})[0])
        self.assertEqual([123], self.dispatcher.calls)

    def test_tmdb_read_limit_and_retry_after(self):
        self.app.tmdb_proxy = TmdbProxy('test-token', opener=lambda req, timeout: TmdbResponse())
        self.store.tmdb_per_client = 3
        for query in ('sever', 'severance', 'dark'):
            self.assertEqual(200, self.tmdb_get(query=query)[0])
        self.assertEqual(429, self.tmdb_get(query='dark%20matter')[0])
        self.assertEqual('3600', self.last_headers['Retry-After'])
        self.now[0] += 30
        self.assertEqual(429, self.tmdb_get()[0])
        self.assertEqual('3570', self.last_headers['Retry-After'])
        self.now[0] += WINDOW_SECONDS
        self.assertEqual(200, self.tmdb_get()[0])

    def test_tmdb_global_limit_is_separate_from_dispatch(self):
        self.app.tmdb_proxy = TmdbProxy('test-token', opener=lambda req, timeout: TmdbResponse())
        self.store.tmdb_global_limit = 2
        self.assertEqual(200, self.tmdb_get(ip='198.51.100.1')[0])
        self.assertEqual(200, self.tmdb_get(ip='198.51.100.2')[0])
        self.assertEqual(429, self.tmdb_get(ip='198.51.100.3')[0])
        self.assertEqual('3600', self.last_headers['Retry-After'])
        self.assertEqual(202, self.call({'tmdbId': 1}, ip='198.51.100.3')[0])

    def test_dispatch_quota_does_not_consume_tmdb_reads(self):
        self.app.tmdb_proxy = TmdbProxy('test-token', opener=lambda req, timeout: TmdbResponse())
        for tmdb_id in (1, 2, 3):
            self.assertEqual(202, self.call({'tmdbId': tmdb_id})[0])
        self.assertEqual(429, self.call({'tmdbId': 4})[0])
        self.assertEqual(200, self.tmdb_get()[0])

    def test_forwarded_ip_only_from_trusted_proxy(self):
        env = {'REMOTE_ADDR': '203.0.113.1', 'HTTP_X_FORWARDED_FOR': '198.51.100.12'}
        self.assertEqual('203.0.113.1', self.app.client_ip(env))
        trusted = App(self.store, self.dispatcher, trusted_proxy_ips={'203.0.113.1'})
        self.assertEqual('198.51.100.12', trusted.client_ip(env))

    def test_dispatch_error_not_reserved(self):
        self.dispatcher.failure = 401
        self.assertEqual(424, self.call({'tmdbId': 1})[0])
        self.dispatcher.failure = None
        self.assertEqual('queued', self.call({'tmdbId': 1})[1]['state'])

    def test_transient_dispatch_error(self):
        self.dispatcher.failure = 429
        self.assertEqual(503, self.call({'tmdbId': 1})[0])

    def test_concurrent_duplicate(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.call({'tmdbId': 1}, ip='thread')[1].get('state')))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(['queued', 'already_queued'], results)
        self.assertEqual([1], self.dispatcher.calls)

    def test_config(self):
        for config in (Config('', 'token'), Config('a/b', ''), Config('a/b', 'token', '../x')):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    config.validate()

    def test_missing_store_config(self):
        with self.assertRaises(ValueError):
            RequestStore('', b'key')
        with self.assertRaises(ValueError):
            RequestStore('db', b'')
        with self.assertRaises(ValueError):
            RequestStore(':memory:', b'key')


class DispatcherTest(unittest.TestCase):
    def test_204_and_minimal_input(self):
        seen = []
        def open_request(req, timeout):
            seen.append(json.loads(req.data))
            return Response()
        GitHubDispatcher(Config('owner/repo', 'test-token'), opener=open_request).dispatch(123)
        self.assertEqual([{'ref': 'main', 'inputs': {'tmdb_id': '123'}}], seen)

    def test_200_dispatch_success(self):
        class Accepted(Response):
            status = 200
        GitHubDispatcher(Config('owner/repo', 'test-token'), opener=lambda req, timeout: Accepted()).dispatch(123)

    def test_permanent_http(self):
        for status in (401, 403, 404, 422):
            with self.subTest(status=status):
                attempts = []
                def fail(req, timeout):
                    attempts.append(1)
                    raise error.HTTPError(req.full_url, status, 'failure', {}, None)
                with self.assertRaises(DispatchFailure) as caught:
                    GitHubDispatcher(Config('a/b', 'token'), opener=fail).dispatch(1)
                self.assertEqual(status, caught.exception.status)
                self.assertEqual(1, len(attempts))

    def test_transient_retries_and_exhaustion(self):
        for status in (429, 500):
            with self.subTest(status=status):
                attempts = []
                def fail(req, timeout):
                    attempts.append(1)
                    raise error.HTTPError(req.full_url, status, 'failure', {}, None)
                with self.assertRaises(DispatchFailure):
                    GitHubDispatcher(Config('a/b', 'token'), opener=fail, sleep=lambda _: None).dispatch(1)
                self.assertEqual(3, len(attempts))

    def test_retry_success(self):
        calls = []
        def open_request(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise error.HTTPError(req.full_url, 503, 'retry', {}, None)
            return Response()
        GitHubDispatcher(Config('a/b', 'token'), opener=open_request, sleep=lambda _: None).dispatch(1)
        self.assertEqual(2, len(calls))

    def test_timeout(self):
        calls = []
        def timeout(req, timeout):
            calls.append(1)
            raise TimeoutError()
        with self.assertRaises(DispatchFailure):
            GitHubDispatcher(Config('a/b', 'token'), opener=timeout, sleep=lambda _: None).dispatch(1)
        self.assertEqual(3, len(calls))


class TmdbProxyTest(unittest.TestCase):
    def test_search_is_fixed_metadata_route(self):
        urls = []
        def open_request(req, timeout):
            urls.append(req.full_url)
            return TmdbResponse()
        proxy = TmdbProxy('test-token', opener=open_request)
        status, body = proxy.get('/v1/tmdb/search', {'query': ['ONE PIECE'], 'language': ['en-US']})
        self.assertEqual(200, status)
        self.assertEqual({'results': []}, json.loads(body))
        self.assertTrue(urls[0].startswith('https://api.themoviedb.org/3/search/tv?'))

    def test_rejects_arbitrary_url_or_factual_metadata(self):
        proxy = TmdbProxy('test-token', opener=lambda *_: self.fail('unexpected network'))
        self.assertEqual(400, proxy.get('/v1/tmdb/search', {'query': ['x'], 'language': ['en-US']})[0])
        self.assertEqual(400, proxy.get('/v1/tmdb/search', {'query': ['test'], 'language': ['en-US'], 'status': ['RENEWED']})[0])
        self.assertEqual(400, proxy.get('/v1/tmdb/series/-1', {'language': ['en-US']})[0])

    def test_missing_token_is_unavailable(self):
        self.assertEqual(503, TmdbProxy('').get('/v1/tmdb/series/1', {'language': ['hr-HR']})[0])

    def test_details_path(self):
        urls = []
        def open_request(req, timeout):
            urls.append(req.full_url)
            return TmdbResponse()
        self.assertEqual(200, TmdbProxy('test-token', opener=open_request).get(
            '/v1/tmdb/series/123', {'language': ['hr-HR']})[0])
        self.assertEqual('https://api.themoviedb.org/3/tv/123?language=hr-HR', urls[0])

    def test_upstream_timeout_is_temporary_unavailable(self):
        def timeout(req, timeout):
            raise TimeoutError()
        self.assertEqual(503, TmdbProxy('test-token', opener=timeout).get(
            '/v1/tmdb/series/123', {'language': ['en-US']})[0])

    def test_oversized_upstream_body_is_rejected(self):
        class Huge(TmdbResponse):
            def read(self, size):
                return b'x' * size
        self.assertEqual(503, TmdbProxy('test-token', opener=lambda req, timeout: Huge()).get(
            '/v1/tmdb/series/123', {'language': ['en-US']})[0])


class SecretPatternTest(unittest.TestCase):
    def test_rejects_private_key_and_pat(self):
        self.assertTrue(any(p.search(b'-----BEGIN ' + b'PRIVATE KEY-----') for p in PATTERNS))
        self.assertTrue(any(p.search(b'github_' + b'pat_' + b'a' * 30) for p in PATTERNS))


class WorkflowBoundaryTest(unittest.TestCase):
    def test_all_production_writers_share_non_canceling_queue(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('process-series-request.yml', 'process-discovered-series.yml', 'update-official-data.yml'):
            content = (root / '.github/workflows' / name).read_text(encoding='utf-8')
            self.assertIn('group: official-data-writes', content)
            self.assertIn('queue: max', content)
            self.assertIn('cancel-in-progress: false', content)

    def test_android_workflow_commits_only_allowlisted_data(self):
        root = Path(__file__).resolve().parents[1]
        content = (root / '.github/workflows/process-series-request.yml').read_text(encoding='utf-8')
        self.assertIn('--process-discovered "$TMDB_ID"', content)
        self.assertIn('--promote-monitored "$TMDB_ID"', content)
        self.assertIn('Enforce changed-file allowlist', content)
        self.assertIn('git", "diff", "--name-only", "--no-renames", "-z", "HEAD', content)
        self.assertIn('git", "ls-files", "--others", "--exclude-standard", "-z', content)
        self.assertIn("vars.OFFICIAL_DATA_PROMOTION_ENABLED == 'true'", content)
        self.assertIn('git add -- official-data/monitored_series.json official-data/history/monitored_changes.jsonl official-data/official_series_data.json official-data/history/changes.jsonl', content)
        self.assertNotIn('git add -- official-data/sources.json', content)

    def test_android_build_has_no_token_build_config(self):
        root = Path(__file__).resolve().parents[1]
        gradle = (root / 'app/build.gradle.kts').read_text(encoding='utf-8')
        self.assertNotIn('buildConfigField("String", "TMDB_API_TOKEN"', gradle)
        self.assertIn('buildConfigField("String", "SERIES_REQUEST_API_BASE_URL"', gradle)


if __name__ == '__main__':
    unittest.main()
