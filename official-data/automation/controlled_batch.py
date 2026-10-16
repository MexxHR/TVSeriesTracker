"""Bounded serialized queue consumer. No claims, attempts or liveness state.

Execution is opt-in. The workflow owns official-data-writes for the entire
invocation. A backend ACK is sent only after validated remote publication.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib import parse, request

import automatic_promotion as stage_guard
import discovery
import manual_promotion_guard as production_guard
import monitored
import promotion
import update

MAX_ITEMS = 3
MAX_RESPONSE = 16_384
HEX = re.compile(r"[0-9a-f]{64}")


class BatchFailure(Exception):
    """Sanitized failure: never include API bodies, receipts or credentials."""


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def valid_id(value):
    return type(value) is int and 0 < value <= 2147483647


def valid_item(item):
    return (isinstance(item, dict) and set(item) == {"tmdbId", "requestReceipt", "ackProof"}
            and valid_id(item["tmdbId"])
            and all(isinstance(item[key], str) and HEX.fullmatch(item[key])
                    for key in ("requestReceipt", "ackProof")))


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BatchFailure("Batch API redirects are forbidden")


class BatchClient:
    def __init__(self, base_url, expected_host, secret, opener=None):
        url = parse.urlsplit(base_url)
        if (url.scheme != "https" or not expected_host or url.hostname != expected_host
                or url.username or url.password or url.port not in (None, 443)
                or url.path not in ("", "/") or url.query or url.fragment
                or not secret or any(c in secret for c in "\r\n")):
            raise BatchFailure("Invalid batch API configuration")
        self.base = "https://" + expected_host
        self.secret = secret
        self.opener = opener or request.build_opener(NoRedirect())

    def _post(self, route, body):
        req = request.Request(self.base + route, data=json.dumps(body).encode(), method="POST",
                              headers={"Content-Type": "application/json",
                                       "Authorization": "Bearer " + self.secret})
        try:
            with self.opener.open(req, timeout=8) as response:
                if response.status != 200 or response.headers.get_content_type() != "application/json":
                    raise BatchFailure("Batch API response rejected")
                # A custom/injected opener must obey the same host boundary.
                if response.geturl() != self.base + route:
                    raise BatchFailure("Batch API destination changed")
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise BatchFailure("Batch API response too large")
                return json.loads(raw, object_pairs_hook=strict_object)
        except BatchFailure:
            raise
        except Exception:
            raise BatchFailure("Batch API unavailable or invalid") from None

    def snapshot(self):
        data = self._post("/internal/series-batch/snapshot", {})
        if (not isinstance(data, dict) or set(data) != {"items"}
                or not isinstance(data["items"], list) or len(data["items"]) > MAX_ITEMS
                or not all(valid_item(item) for item in data["items"])
                or len({item["tmdbId"] for item in data["items"]}) != len(data["items"])):
            raise BatchFailure("Invalid snapshot response")
        return data["items"]

    def complete(self, item):
        data = self._post("/internal/series-batch/complete", {"items": [item]})
        if (not isinstance(data, dict) or set(data) != {"items"}
                or not isinstance(data["items"], list) or len(data["items"]) != 1
                or not isinstance(data["items"][0], dict)
                or set(data["items"][0]) != {"tmdbId", "state"}
                or data["items"][0]["tmdbId"] != item["tmdbId"]
                or type(data["items"][0]["tmdbId"]) is not int
                or data["items"][0]["state"] not in {"complete", "already_complete", "stale"}):
            raise BatchFailure("Invalid completion response")
        return data["items"][0]["state"]


class Outcome(Enum):
    PUBLISHED = "published"
    NO_CHANGE = "no_change"
    NO_EVIDENCE = "no_evidence"
    UNSUPPORTED = "unsupported"
    RETRY = "retry"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    factual_changes: int = 0

    @property
    def ack_eligible(self):
        return self.outcome is not Outcome.RETRY


def monitored_outcome(report):
    """Only a validated persisted report may reach this mapping."""
    state = report.get("pipelineState")
    if state in {"PROVIDER_UNAVAILABLE", "METADATA_UNAVAILABLE"}:
        return Outcome.RETRY
    if state in {"VALIDATION_FAILED", "ALREADY_TRUSTED"}:
        raise BatchFailure("Monitored processing failed or used the wrong route")
    # Warnings / failed discovery endpoints cannot be turned into clean absence.
    if report.get("discoveryResult") == "provider_unavailable" or any(
            check.get("error") for check in report.get("sourceChecks", [])):
        return Outcome.RETRY
    if state == "UNSUPPORTED_PROVIDER":
        return Outcome.UNSUPPORTED
    if state in {"NO_VERIFIED_FACTS", "NOT_PARSER_ELIGIBLE", "DISCOVERY_INSUFFICIENT"}:
        return Outcome.NO_EVIDENCE
    if state == "VERIFIED_FACTS":
        return None  # Requires independently validated promotion first.
    raise BatchFailure("Unknown processing outcome")


class GitBoundary:
    def __init__(self, root):
        self.root = Path(root)

    def command(self, *args):
        try:
            return subprocess.check_output(["git", *args], cwd=self.root,
                                           stderr=subprocess.PIPE).decode().strip()
        except Exception:
            raise BatchFailure("Repository operation failed; no ACK") from None

    def current(self):
        if production_guard.changed_paths(self.root):
            raise BatchFailure("Unsafe dirty repository")
        if self.command("symbolic-ref", "--short", "HEAD") != "main":
            raise BatchFailure("Batch requires main branch")
        self.command("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        head = self.command("rev-parse", "HEAD")
        if head != self.command("rev-parse", "refs/remotes/origin/main"):
            raise BatchFailure("Main head mismatch")
        return head

    def publish(self, allowed, message):
        changed = production_guard.changed_paths(self.root)
        if not changed:
            self.current()
            return
        if changed != set(allowed):
            raise BatchFailure("Unexpected publication boundary")
        # Recheck remote before commit without requiring the dirty tree clean.
        self.command("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
        if self.command("rev-parse", "HEAD") != self.command("rev-parse", "refs/remotes/origin/main"):
            raise BatchFailure("Main changed before publication")
        self.command("add", "--", *sorted(allowed))
        self.command("config", "user.name", "github-actions[bot]")
        self.command("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        self.command("commit", "-m", message)
        self.command("push", "origin", "HEAD:main")
        self.current()  # Any uncertainty/mismatch prevents ACK.


def verify_trusted(root, before, report, tmdb_id):
    """Validate selected trusted updater's complete-dataset publication boundary."""
    if not report.get("publishable") or report.get("failures"):
        raise BatchFailure("Trusted validation blocked publication")
    after = {name: (root / name).read_bytes() for name in production_guard.PROTECTED}
    changes = report.get("changes")
    if not isinstance(changes, list) or len(changes) > 1 or any(
            row.get("tmdbId") != tmdb_id for row in changes):
        raise BatchFailure("Trusted selection changed unrelated IDs")
    if any(before[name] != after[name] for name in production_guard.PROTECTED[2:]):
        raise BatchFailure("Trusted pass changed monitored/source data")
    if not changes:
        if before != after or production_guard.changed_paths(root):
            raise BatchFailure("Trusted no-op changed files")
        return False
    if production_guard.changed_paths(root) != production_guard.WRITABLE:
        raise BatchFailure("Trusted publication file boundary rejected")
    old, new = (json.loads(values[production_guard.PROTECTED[0]]) for values in (before, after))
    registry = json.loads(after[production_guard.PROTECTED[2]])
    update.validate_dataset(new, registry, old)
    old_ids = {row['tmdbId']: row for row in old['series']}
    new_ids = {row['tmdbId']: row for row in new['series']}
    if {i for i in old_ids.keys() | new_ids.keys() if old_ids.get(i) != new_ids.get(i)} != {tmdb_id}:
        raise BatchFailure("Trusted canonical diff rejected")
    if changes[0].get('old') != old_ids.get(tmdb_id) or changes[0].get('new') != new_ids.get(tmdb_id):
        raise BatchFailure("Trusted report differs from published record")
    old_audit, new_audit = (values[production_guard.PROTECTED[1]] for values in (before, after))
    appended = new_audit[len(old_audit):]
    if (not new_audit.startswith(old_audit) or appended.count(b'\n') != 1
            or not appended.endswith(b'\n') or json.loads(appended) != changes[0]):
        raise BatchFailure("Trusted audit boundary rejected")
    return True


class Processor:
    def __init__(self, root, promotion_enabled, git=None):
        self.root = Path(root)
        self.promotion_enabled = promotion_enabled
        self.git = git or GitBoundary(root)

    def process(self, tmdb_id):
        self.git.current()
        stage_guard._validate_inputs(self.root)
        trusted = json.loads((self.root / 'official-data/sources.json').read_text(encoding='utf-8'))
        if any(row['tmdbId'] == tmdb_id for row in trusted['series']):
            return self._trusted(tmdb_id)
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / 'stage'
            stage_guard.snapshot_stage(self.root, snapshot, tmdb_id)
            discovery_reports = []
            def discover(meta, trusted_registry):
                discovered = discovery.discover(meta, trusted_registry)
                discovery_reports.append(discovered)
                return discovered
            report = monitored.process(tmdb_id, dry_run=False,
                                       discoverer=discover,
                                       registry_path=self.root / production_guard.PROTECTED[3],
                                       audit_path=self.root / production_guard.PROTECTED[4])
            # Determine safety before any publication; unknown/fatal blocks run.
            outcome = monitored_outcome(report)
            if outcome in {Outcome.NO_EVIDENCE, Outcome.UNSUPPORTED} and any(
                    discovered.get('warnings') for discovered in discovery_reports):
                outcome = Outcome.RETRY
            unchanged = all((self.root / name).read_bytes() == (snapshot / name).read_bytes()
                            for name in production_guard.PROTECTED)
            if outcome is Outcome.RETRY and unchanged:
                # Metadata failure / preserved unavailable record can have no
                # persisted row matching pipelineState; validate no-write path.
                self.git.current()
                stage_guard._validate_inputs(self.root)
                return Result(Outcome.RETRY)
            stage_guard.verify_stage(self.root, snapshot, tmdb_id, report)
            self.git.publish(stage_guard.MONITORED_WRITABLE, 'Update monitored series registry')
            if outcome is not None:
                return Result(outcome)
            if not self.promotion_enabled:
                return Result(Outcome.RETRY)
            expected = self.git.current()
            production_snapshot = Path(temporary) / 'production'
            production_guard.preflight(self.root, production_snapshot, tmdb_id)
            result = promotion.promote(tmdb_id, dry_run=False,
                                       production_data=self.root / production_guard.PROTECTED[0],
                                       production_audit=self.root / production_guard.PROTECTED[1],
                                       production_registry=self.root / production_guard.PROTECTED[2],
                                       monitored_registry=self.root / production_guard.PROTECTED[3])
            commit = production_guard.verify(self.root, production_snapshot, tmdb_id, result,
                                             first_one_piece_acceptance=False)
            if self.git.command('rev-parse', 'HEAD') != expected:
                raise BatchFailure('Promotion head mismatch')
            self.git.publish(production_guard.WRITABLE, 'Promote verified monitored series')
            return Result(Outcome.PUBLISHED if commit else Outcome.NO_CHANGE, int(commit))

    def _trusted(self, tmdb_id):
        # update.run uses module paths. Refuse a mismatched root before mutation.
        if update.DATA.resolve() != (self.root / production_guard.PROTECTED[0]).resolve():
            raise BatchFailure('Trusted updater root mismatch')
        before = {name: (self.root / name).read_bytes() for name in production_guard.PROTECTED}
        # The automatic promotion kill switch also gates new canonical writes
        # through this request-driven trusted route. A disabled pass is read-only.
        report = update.run(dry_run=not self.promotion_enabled, selected_tmdb_id=tmdb_id)
        if not report.get('publishable') or report.get('failures'):
            raise BatchFailure('Trusted processing failed')
        if not self.promotion_enabled:
            return Result(Outcome.RETRY)
        wrote = verify_trusted(self.root, before, report, tmdb_id)
        self.git.publish(production_guard.WRITABLE, 'Update requested trusted series')
        return Result(Outcome.RETRY if report.get('warnings') else
                      Outcome.PUBLISHED if wrote else Outcome.NO_CHANGE, int(wrote))


def execute(items, processor, client=None):
    if (len(items) > MAX_ITEMS or not all(valid_id(item['tmdbId']) for item in items)
            or len({item['tmdbId'] for item in items}) != len(items)):
        raise BatchFailure('Batch size/identity rejected')
    summary = dict(selected=len(items), processed=0, completed=0, retained=0, no_change=0,
                   factual_changes=0, failed=0)
    for item in items:
        # A fatal unsafe checkout/global failure aborts; no generic continuation.
        try:
            result = processor.process(item['tmdbId'])
            if (not isinstance(result, Result) or type(result.factual_changes) is not int
                    or result.factual_changes not in (0, 1)):
                raise BatchFailure('Invalid processing result')
            summary['factual_changes'] += result.factual_changes
            if summary['factual_changes'] > 4:
                raise BatchFailure('Maximum-four factual changes exceeded')
            summary['processed'] += 1
            summary['no_change'] += int(result.outcome is Outcome.NO_CHANGE)
            if result.ack_eligible and client is not None:
                state = client.complete(item)
                summary['completed' if state in {'complete', 'already_complete'} else 'retained'] += 1
            elif client is not None:
                summary['retained'] += 1
        except Exception:
            # Print counts only; HTTP/git/provider exception text may be sensitive.
            summary['failed'] += 1
            print(json.dumps(summary, sort_keys=True))
            raise BatchFailure('Batch stopped; unacknowledged work remains pending') from None
    return summary


def monitored_selection(root, day, limit=MAX_ITEMS):
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise BatchFailure('Monitored selection limit rejected')
    trusted = json.loads((Path(root) / production_guard.PROTECTED[2]).read_text(encoding='utf-8'))
    update.validate_registry(trusted)
    registry, _ = monitored.load_registry(Path(root) / production_guard.PROTECTED[3])
    trusted_ids = {row['tmdbId'] for row in trusted['series']}
    identifiers = sorted(row['tmdbId'] for row in registry['series'] if row['tmdbId'] not in trusted_ids)
    if not identifiers:
        return []
    start = day.toordinal() * MAX_ITEMS % len(identifiers)
    rotated = identifiers[start:] + identifiers[:start]
    return [{'tmdbId': identifier} for identifier in rotated[:limit]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('requests', 'monitored'), default='requests')
    parser.add_argument('--execute', action='store_true', help='Opt-in live execution; requires readiness gate')
    args = parser.parse_args()
    root = Path.cwd()
    if not args.execute:
        stage_guard._validate_inputs(root)
        print(json.dumps({'dryRun': True, 'maxItems': MAX_ITEMS, 'networkCalls': 0, 'publication': False}))
        return
    gate = 'SERIES_BATCH_PROCESSING_ENABLED' if args.kind == 'requests' else 'SERIES_MONITORED_REFRESH_ENABLED'
    if os.getenv(gate) != 'true':
        raise BatchFailure('Processing readiness gate is disabled')
    processor = Processor(root, stage_guard.gate_enabled(os.getenv('OFFICIAL_DATA_PROMOTION_ENABLED', '')))
    if args.kind == 'requests':
        client = BatchClient(os.getenv('SERIES_BATCH_API_URL', ''), os.getenv('SERIES_BATCH_API_HOST', ''),
                             os.getenv('SERIES_BATCH_API_SECRET', ''))
        items = client.snapshot()
        summary = execute(items, processor, client)
    else:
        items = monitored_selection(root, dt.datetime.now(dt.timezone.utc).date())
        summary = execute(items, processor)
    print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Controlled batch failed; inspect sanitized counters and readiness configuration') from None
