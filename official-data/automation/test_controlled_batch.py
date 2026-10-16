"""Production batch code with offline providers/API and temporary git remotes."""
import contextlib
import copy
import datetime as dt
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import controlled_batch as batch
import update

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import service

NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc)
ID = 999001
TITLE = 'Harbor Signal'
URL = 'https://www.netflix.com/tudum/articles/harbor-signal-renewed-season-2'


class WsgiOpener:
    def __init__(self, app):
        self.app = app
        self.fail_ack = False

    def open(self, req, timeout):
        if self.fail_ack and req.full_url.endswith('/complete'):
            raise OSError('synthetic ACK outage')
        path = req.full_url.removeprefix('https://batch.example.invalid')
        statuses = []
        headers = []
        env = {'PATH_INFO': path, 'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': 'application/json',
               'CONTENT_LENGTH': str(len(req.data)), 'wsgi.input': io.BytesIO(req.data),
               'REMOTE_ADDR': '127.0.0.1', 'wsgi.url_scheme': 'https',
               'HTTP_AUTHORIZATION': req.get_header('Authorization')}
        data = b''.join(self.app(env, lambda s, h: (statuses.append(s), headers.extend(h))))
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return data[:size]
            def geturl(self): return req.full_url
        response = Response()
        response.status = int(statuses[0][:3])
        response.headers = Message()
        for key, value in headers: response.headers[key] = value
        return response


class BatchOrchestrationTests(unittest.TestCase):
    def test_empty_has_no_provider_or_ack_call(self):
        processor, client = Mock(), Mock()
        self.assertEqual(batch.execute([], processor, client)['processed'], 0)
        processor.process.assert_not_called(); client.complete.assert_not_called()

    def test_three_are_sequential_and_fourth_rejected_before_work(self):
        events = []
        processor = Mock()
        processor.process.side_effect = lambda i: (events.append(i) or batch.Result(batch.Outcome.NO_CHANGE))
        client = Mock(); client.complete.return_value = 'complete'
        items = [{'tmdbId': i} for i in (1, 2, 3)]
        result = batch.execute(items, processor, client)
        self.assertEqual(events, [1, 2, 3]); self.assertEqual(result['completed'], 3)
        self.assertEqual(result['factual_changes'], 0)
        with self.assertRaises(batch.BatchFailure): batch.execute(items + [{'tmdbId': 4}], processor, client)
        self.assertEqual(events, [1, 2, 3])

    def test_explicit_result_mapping(self):
        for outcome in (batch.Outcome.PUBLISHED, batch.Outcome.NO_CHANGE,
                        batch.Outcome.NO_EVIDENCE, batch.Outcome.UNSUPPORTED, batch.Outcome.RETRY):
            with self.subTest(outcome=outcome):
                processor = Mock(); processor.process.return_value = batch.Result(outcome)
                client = Mock(); client.complete.return_value = 'complete'
                result = batch.execute([{'tmdbId': 1}], processor, client)
                self.assertEqual(client.complete.call_count, int(outcome is not batch.Outcome.RETRY))
                self.assertEqual(result['retained'], int(outcome is batch.Outcome.RETRY))

    def test_pipeline_states_require_clean_terminal_outcome(self):
        for state in ('PROVIDER_UNAVAILABLE', 'METADATA_UNAVAILABLE'):
            self.assertEqual(batch.monitored_outcome({'pipelineState': state}), batch.Outcome.RETRY)
        for state in ('VALIDATION_FAILED', 'ALREADY_TRUSTED', 'unknown'):
            with self.assertRaises(batch.BatchFailure): batch.monitored_outcome({'pipelineState': state})
        for state in ('NO_VERIFIED_FACTS', 'NOT_PARSER_ELIGIBLE', 'DISCOVERY_INSUFFICIENT'):
            self.assertEqual(batch.monitored_outcome({'pipelineState': state}), batch.Outcome.NO_EVIDENCE)
        self.assertIsNone(batch.monitored_outcome({'pipelineState': 'VERIFIED_FACTS'}))

    def test_unsafe_failure_aborts_without_ack_or_continuation(self):
        processor, client = Mock(), Mock()
        processor.process.side_effect = ValueError('synthetic global unsafe checkout')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(batch.BatchFailure):
            batch.execute([{'tmdbId': 1}, {'tmdbId': 2}], processor, client)
        self.assertEqual(processor.process.call_count, 1)
        client.complete.assert_not_called()

    def test_no_more_than_three_factual_changes_possible_in_request_unit(self):
        processor, client = Mock(), Mock()
        processor.process.return_value = batch.Result(batch.Outcome.PUBLISHED, 1)
        client.complete.return_value = 'complete'
        result = batch.execute([{'tmdbId': i} for i in (1, 2, 3)], processor, client)
        self.assertEqual(result['factual_changes'], 3)
        processor.process.return_value = batch.Result(batch.Outcome.PUBLISHED, 5)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(batch.BatchFailure):
            batch.execute([{'tmdbId': 4}], processor, client)


class BatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'; self.root.mkdir()
        self.remote = Path(self.temp.name) / 'remote.git'
        fixture = Path(__file__).parent / 'fixtures/v235_canonical.json'
        self.sources = json.loads(update.REGISTRY.read_text(encoding='utf-8'))
        values = (fixture.read_bytes(), b'', json.dumps(self.sources).encode(),
                  b'{"schemaVersion":1,"series":[]}\n', b'')
        for name, data in zip(batch.production_guard.PROTECTED, values):
            target = self.root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'core.autocrlf', 'false')
        self.git('add', '--all'); self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                         'commit', '-q', '-m', 'baseline')
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True, capture_output=True)
        self.git('remote', 'add', 'origin', str(self.remote)); self.git('push', '-q', 'origin', 'main')
        self.clock = [1_000_000.0]
        self.secret = 'synthetic-phase1-batch-fixture'
        self.store = service.RequestStore(str(Path(self.temp.name) / 'queue.sqlite'), b'synthetic-ip', clock=lambda: self.clock[0])
        self.app = service.App(self.store, batch_mode=True, batch_secret=self.secret)
        self.opener = WsgiOpener(self.app)
        self.client = batch.BatchClient('https://batch.example.invalid', 'batch.example.invalid', self.secret, self.opener)
        self.processor = batch.Processor(self.root, True)
        self.original_process = batch.monitored.process
        self.original_promote = batch.promotion.promote
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for attr, path in (('DATA', 0), ('AUDIT', 1), ('REGISTRY', 2)):
            self.stack.enter_context(patch.object(update, attr, self.root / batch.production_guard.PROTECTED[path]))
        self.metadata = {'tmdbId': ID, 'title': TITLE, 'networks': [{'name': 'Netflix'}],
                         'homepage': 'https://www.netflix.com/title/80999001'}
        self.discovery = {'provider': 'NETFLIX', 'result': 'candidate_found', 'warnings': [],
                          'candidateUrls': [URL], 'candidates': [{'candidateUrl': URL, 'factualParserEligible': True,
                                                                'evidenceLevel': 'STRONG'}]}
        self.stack.enter_context(patch.object(batch.discovery, 'discover', side_effect=lambda *_: copy.deepcopy(self.discovery)))
        self.stack.enter_context(patch.object(batch.monitored, 'process', side_effect=self.process_fixture))
        self.stack.enter_context(patch.object(batch.promotion, 'promote', side_effect=lambda *a, **k:
                                              self.original_promote(*a, now=NOW, **k)))

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def process_fixture(self, identifier, **kwargs):
        meta = dict(self.metadata, tmdbId=identifier)
        return self.original_process(identifier, metadata=meta, now=NOW, today=NOW.date(),
                                     fetcher=lambda *_: ('<h1>Harbor Signal Season 2</h1><p>Harbor Signal has been renewed for Season 2.</p>', URL),
                                     **kwargs)

    def canonical_bytes(self):
        return tuple((self.root / name).read_bytes() for name in batch.production_guard.PROTECTED[:2])

    def test_actual_publication_ack_failure_replay_no_change_and_ack(self):
        _, _, receipt = self.store.submit_pending(ID)
        first = self.client.snapshot()
        self.opener.fail_ack = True
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(batch.BatchFailure):
            batch.execute(first, self.processor, self.client)
        published = self.canonical_bytes()
        self.assertEqual(self.store.receipt_status(ID, receipt), 'pending')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.clock[0] += 600; self.opener.fail_ack = False
        summary = batch.execute(self.client.snapshot(), self.processor, self.client)
        self.assertEqual(summary['no_change'], 1)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(published, self.canonical_bytes())
        self.assertEqual(self.store.receipt_status(ID, receipt), 'complete')

    def test_promotion_disabled_keeps_pending_and_canonical_unchanged(self):
        self.processor.promotion_enabled = False
        _, _, receipt = self.store.submit_pending(ID); before = self.canonical_bytes()
        summary = batch.execute(self.client.snapshot(), self.processor, self.client)
        self.assertEqual(summary['retained'], 1)
        self.assertEqual(before, self.canonical_bytes())
        self.assertEqual(self.store.receipt_status(ID, receipt), 'pending')

    def test_unsupported_ack_and_low_frequency_future_observation(self):
        self.discovery = {'provider': None, 'result': 'insufficient_evidence', 'reason': 'unknown_provider',
                          'candidateUrls': [], 'candidates': [], 'warnings': []}
        _, _, receipt = self.store.submit_pending(ID); before = self.canonical_bytes()
        self.assertEqual(batch.execute(self.client.snapshot(), self.processor, self.client)['completed'], 1)
        self.assertEqual(self.store.receipt_status(ID, receipt), 'complete')
        self.assertEqual(batch.monitored_selection(self.root, NOW.date()), [{'tmdbId': ID}])
        self.assertEqual(batch.execute(batch.monitored_selection(self.root, NOW.date()), self.processor)['processed'], 1)
        self.assertEqual(before, self.canonical_bytes())
        self.assertEqual(json.loads((self.root / 'official-data/sources.json').read_text()), self.sources)

    def test_clean_no_evidence_ack_but_warning_requires_retry(self):
        self.discovery = {'provider': 'NETFLIX', 'result': 'insufficient_evidence',
                          'candidateUrls': [], 'candidates': [], 'warnings': []}
        self.store.submit_pending(ID)
        self.assertEqual(batch.execute(self.client.snapshot(), self.processor, self.client)['completed'], 1)
        self.discovery['warnings'] = ['synthetic partial transport failure']
        _, _, receipt = self.store.submit_pending(ID)
        self.assertEqual(batch.execute(self.client.snapshot(), self.processor, self.client)['retained'], 1)
        self.assertEqual(self.store.receipt_status(ID, receipt), 'pending')

    def test_provider_unavailable_does_not_ack_or_change_canonical(self):
        # Establish a real guarded supplemental publication first, then observe
        # a low-frequency provider outage against the existing verified record.
        self.store.submit_pending(ID)
        batch.execute(self.client.snapshot(), self.processor, self.client)
        self.discovery = {'provider': 'NETFLIX', 'result': 'provider_unavailable',
                          'candidateUrls': [], 'candidates': [], 'warnings': ['synthetic outage']}
        before = self.canonical_bytes(); _, _, receipt = self.store.submit_pending(ID)
        self.assertEqual(batch.execute(self.client.snapshot(), self.processor, self.client)['retained'], 1)
        self.assertEqual(before, self.canonical_bytes())
        self.assertEqual(self.store.receipt_status(ID, receipt), 'pending')
        batch.execute(batch.monitored_selection(self.root, NOW.date()), self.processor)
        self.assertEqual(before, self.canonical_bytes())

    def test_failed_or_uncertain_push_prevents_ack(self):
        _, _, receipt = self.store.submit_pending(ID)
        original = self.processor.git.command
        def command(*args):
            if args[0] == 'push': raise batch.BatchFailure('synthetic uncertain push')
            return original(*args)
        with patch.object(self.processor.git, 'command', side_effect=command), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(batch.BatchFailure):
            batch.execute(self.client.snapshot(), self.processor, self.client)
        self.assertEqual(self.store.receipt_status(ID, receipt), 'pending')

    def test_dirty_global_boundary_blocks_before_provider_work(self):
        (self.root / 'unexpected.txt').write_text('synthetic dirt')
        with self.assertRaises(batch.BatchFailure): self.processor.process(ID)
        batch.monitored.process.assert_not_called()

    def test_trusted_route_runs_only_selected_collector_and_validates_no_change(self):
        identifier = self.sources['series'][0]['tmdbId']
        original_run = update.run; collected = []
        def collect(entry, old, today):
            collected.append(entry['tmdbId']); return [], 0
        with patch.object(update, 'run', side_effect=lambda **k: original_run(collector=collect, **k)):
            result = self.processor.process(identifier)
        self.assertEqual(collected, [identifier])
        self.assertEqual(result.outcome, batch.Outcome.NO_CHANGE)
        batch.monitored.process.assert_not_called()

    def test_trusted_unavailable_preserves_verified_bytes_and_retains(self):
        identifier = self.sources['series'][0]['tmdbId']; before = self.canonical_bytes()
        original_run = update.run
        def unavailable(*args): raise update.ProviderUnavailable('synthetic provider outage')
        with patch.object(update, 'run', side_effect=lambda **k: original_run(collector=unavailable, **k)):
            result = self.processor.process(identifier)
        self.assertEqual(result.outcome, batch.Outcome.RETRY)
        self.assertEqual(before, self.canonical_bytes())

    def test_trusted_unknown_selection_rejected_without_provider_calls(self):
        collector = Mock()
        with self.assertRaises(ValueError): update.run(True, collector=collector, selected_tmdb_id=ID)
        collector.assert_not_called()

    def test_selected_trusted_factual_change_uses_existing_merge_and_audit(self):
        identifier = self.sources['series'][0]['tmdbId']
        original_run = update.run
        def collector(entry, old, today):
            text = f"{entry['title']} has been renewed for Season {old['nextSeasonNumber'] + 1}."
            article = {'title': text, 'body': text, 'sourceName': old['sourceName'],
                       'url': entry['officialUrl'], 'publicationDate': None}
            facts = update.detect(article, entry, today)
            self.assertTrue(facts)
            return facts, 1
        with patch.object(update, 'run', side_effect=lambda **k: original_run(collector=collector, **k)):
            result = self.processor.process(identifier)
        self.assertEqual(result.outcome, batch.Outcome.PUBLISHED)
        self.assertEqual(result.factual_changes, 1)
        self.assertEqual(self.git('status', '--porcelain'), '')
        batch.monitored.process.assert_not_called()

    def test_metadata_unavailable_no_write_no_ack(self):
        self.store.submit_pending(ID)
        before = self.canonical_bytes()
        with patch.object(batch.monitored, 'process', return_value={'pipelineState': 'METADATA_UNAVAILABLE'}):
            self.assertEqual(batch.execute(self.client.snapshot(), self.processor, self.client)['retained'], 1)
        self.assertEqual(before, self.canonical_bytes())

    def test_three_actual_units_publish_clean_boundaries_and_defer_fourth(self):
        for identifier in (ID, ID+1, ID+2, ID+3): self.store.submit_pending(identifier)
        ticks = [0]
        def promote(*args, **kwargs):
            ticks[0] += 1
            return self.original_promote(*args, now=NOW+dt.timedelta(seconds=ticks[0]), **kwargs)
        with patch.object(batch.promotion, 'promote', side_effect=promote):
            result = batch.execute(self.client.snapshot(), self.processor, self.client)
        self.assertEqual(result['completed'], 3)
        self.assertEqual(result['factual_changes'], 3)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual([item['tmdbId'] for item in self.client.snapshot()], [ID+3])

    def test_low_frequency_rotation_visits_all_ids_and_excludes_trusted(self):
        rows = []
        for identifier in range(ID, ID + 8):
            rows.append({'tmdbId': identifier, 'title': TITLE, 'provider': None,
                         'discoveryState': 'UNSUPPORTED_PROVIDER', 'candidateUrls': [], 'verifiedFacts': {},
                         'sourceEvidence': [], 'parserResult': 'skipped', 'validationResult': 'not_applicable',
                         'eligibleSourceUrls': [], 'lastChecked': NOW.date().isoformat()})
        path = self.root / 'official-data/monitored_series.json'
        path.write_text(json.dumps({'schemaVersion': 1, 'series': rows}))
        selected = set()
        for offset in range(8):
            items = batch.monitored_selection(self.root, NOW.date() + dt.timedelta(days=offset))
            self.assertLessEqual(len(items), 3); selected.update(item['tmdbId'] for item in items)
        self.assertEqual(selected, set(range(ID, ID + 8)))
        with self.assertRaises(batch.BatchFailure): batch.monitored_selection(self.root, NOW.date(), limit=4)


class BatchClientSecurityTests(unittest.TestCase):
    def test_dry_run_and_disabled_execution_make_no_network_calls(self):
        with patch.object(sys, 'argv', ['controlled_batch.py']), patch.object(batch, 'BatchClient') as client, \
                patch.object(batch.stage_guard, '_validate_inputs'), contextlib.redirect_stdout(io.StringIO()):
            batch.main(); client.assert_not_called()
        with patch.object(sys, 'argv', ['controlled_batch.py', '--execute']), \
                patch.dict(batch.os.environ, {}, clear=True), patch.object(batch, 'BatchClient') as client:
            with self.assertRaises(batch.BatchFailure): batch.main()
            client.assert_not_called()

    def test_invalid_host_https_credentials_and_paths_rejected(self):
        for url, host in [('http://example.invalid', 'example.invalid'),
                          ('https://evil.invalid', 'example.invalid'),
                          ('https://user:pass@example.invalid', 'example.invalid'),
                          ('https://example.invalid/path', 'example.invalid')]:
            with self.assertRaises(batch.BatchFailure): batch.BatchClient(url, host, 'synthetic-only')

    def test_redirect_handler_never_replays_secret(self):
        with self.assertRaises(batch.BatchFailure):
            batch.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')

    def test_snapshot_schema_rejects_extra_fields_duplicates_and_oversized_list(self):
        client = batch.BatchClient('https://batch.example.invalid', 'batch.example.invalid', 'synthetic-only')
        item = {'tmdbId': 1, 'requestReceipt': 'a'*64, 'ackProof': 'b'*64}
        for response in ({'items': [], 'extra': 1}, {'items': [item]*2}, {'items': [item]*4},
                         {'items': [dict(item, tmdbId=True)]}):
            with patch.object(client, '_post', return_value=response), self.assertRaises(batch.BatchFailure):
                client.snapshot()

    def test_no_secret_or_receipt_in_error_summary(self):
        processor, client = Mock(), Mock()
        processor.process.return_value = batch.Result(batch.Outcome.NO_CHANGE)
        client.complete.side_effect = ValueError('synthetic-sensitive-receipt-value')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(batch.BatchFailure) as exc:
            batch.execute([{'tmdbId': 1}], processor, client)
        self.assertNotIn('sensitive', str(exc.exception) + output.getvalue())

    def test_manual_workflows_have_no_schedule_and_have_shared_writer_guards(self):
        for name in ('controlled-series-batch.yml', 'refresh-monitored-series.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertNotIn('\n  schedule:', text)
            self.assertIn('default: true', text)
            self.assertIn('group: official-data-writes', text)
            self.assertIn('cancel-in-progress: false', text)
            self.assertIn('Regression', text.replace('regression', 'Regression'))


if __name__ == '__main__': unittest.main()
