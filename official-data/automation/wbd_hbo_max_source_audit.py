"""Read-only WBD/HBO/Max audit; research corpus is never production eligibility.

Existing HTTP, strict parser and independent evidence-replay helpers are used.
No monitored process, updater, promotion writer, backend or Android is invoked.
"""
from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit, urljoin

import discovery
import provider_audit as audit
import fx_hulu_qualification as replay
import update

CONFIG = Path(__file__).with_name("wbd_hbo_max_audit_cases.json")
MAX_FETCHES = 72  # shared by crawl, direct probes, historical URLs and replay
MAX_DEPTH = 3
MAX_LINKS = 32


def public_url(url):
    p = urlsplit(url)
    host = p.hostname or ""
    # Even rejected redirect/canonical URLs must not serialize userinfo.
    return urlunsplit((p.scheme, host, p.path, "", ""))


def canonical(url):
    return public_url(url).rstrip("/")


def allowed(url, domains):
    """Audit-specific exact host boundary; does not expand production trust."""
    hosts = set(domains) | {"www." + d for d in domains if d.count(".") == 1}
    return update.allowed(url, domains) and urlsplit(url).hostname in hosts


def traced_fetch(url, domains):
    """Instrument existing redirect guard in this sequential standalone audit.

    The original HTTP layer remains responsible for bounds/content/timeouts.
    The temporary class patch is restored on exit; no alternate HTTP client.
    No request/response headers, cookies or provider body are logged.
    """
    chain = []
    original = audit.BoundedRedirect
    class TracedRedirect(original):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            chain.append({"fromUrl": public_url(req.full_url), "httpStatus": int(code),
                          "toUrl": public_url(newurl), "withinAuditBoundary": allowed(newurl, domains)})
            if not allowed(newurl, domains):
                raise audit.RedirectRejected("redirect_rejected")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    if not allowed(url, domains):
        return {"status": "rejected", "failureCategory": "OFFICIAL_DOMAIN_REJECTED", "redirectChain": []}, None
    with patch.object(audit, "BoundedRedirect", TracedRedirect):
        result, raw = audit.fetch_url(url, domains)
    return {**result, "redirectChain": chain}, raw


def proposed_route(metadata):
    """Network-only proposal. No changes to discovery.route_provider."""
    labels = [n.get("name") if isinstance(n, dict) else n for n in metadata.get("networks", [])]
    labels = [s for s in labels if isinstance(s, str)]
    normalized = {" ".join(s.casefold().split()) for s in labels}
    matches = set()
    if "hbo" in normalized:
        matches.add("HBO")
    if normalized & {"max", "hbo max"}:
        matches.add("MAX")
    # Additional independent network signals are not silently collapsed to WBD.
    other = normalized - {"hbo", "max", "hbo max"}
    if len(matches) != 1 or other:
        return {"route": None, "rawNetworkLabels": labels,
                "result": "AMBIGUOUS_NETWORKS" if matches else "UNSUPPORTED_OR_UNKNOWN_NETWORK",
                "ownershipHomepageDistributorIgnored": True}
    return {"route": next(iter(matches)), "rawNetworkLabels": labels,
            "result": "NETWORK_ONLY_CANDIDATE", "ownershipHomepageDistributorIgnored": True}


def metadata_observations(cases, token, loader=discovery.tmdb_metadata):
    rows = {}
    for case in cases:
        key = case["tmdbId"]
        if key in rows:
            continue
        if not token:
            rows[key] = {"tmdbId": key, "requestedTitle": case["series"], "networks": None,
                         "result": "MISSING_TMDB_CREDENTIAL", "routeProposal": None}
            continue
        try:
            data = loader(key, token)
            if discovery.normalize(data.get("title", "")) not in {discovery.normalize(a) for a in case["aliases"]}:
                raise ValueError("TMDB title mismatch")
            # Discard all non-identity/network fields, including any lifecycle values.
            networks = [{"id": n.get("id"), "name": n.get("name")} for n in data.get("networks", []) if isinstance(n, dict)]
            rows[key] = {"tmdbId": key, "requestedTitle": case["series"], "title": data["title"],
                         "networks": networks, "result": "FETCHED_IDENTITY_METADATA_ONLY",
                         "routeProposal": proposed_route({"networks": networks})}
        except (update.AutomationError, ValueError, OSError) as exc:
            rows[key] = {"tmdbId": key, "requestedTitle": case["series"], "networks": None,
                         "result": "TMDB_METADATA_UNAVAILABLE", "failureType": type(exc).__name__, "routeProposal": None}
    return rows


def access_class(result, raw):
    status = result.get("httpStatus")
    if status == 403:
        return "HTTP_403"
    if status == 404:
        return "HTTP_404"
    if status == 429:
        return "RATE_LIMITED"
    if result.get("failureCategory") == "DNS_FAILURE":
        return "DNS_FAILURE"
    if result.get("status") == "fetched" and raw:
        if any(s in raw.casefold() for s in ("verify you are human", "cf-chl-", "captcha")):
            return "BOT_BLOCKED"
        return "REDIRECTED_ACCESSIBLE" if result.get("redirectCount", 0) else "ACCESSIBLE"
    return "OTHER_NETWORK_FAILURE"  # never infer JS_ONLY from an empty/error body


def family(url):
    host, path = urlsplit(url).hostname or "", urlsplit(url).path
    if host == "press.wbd.com":
        return "WBD_PRESS_ARTICLE" if "/media-release/" in path else "WBD_NEWSROOM"
    if host == "press-spain.max.com":
        return "MAX_PRESS" if path.startswith("/post/") else "WBD_NEWSROOM"
    if host in ("hbo.com", "www.hbo.com"):
        return "HBO_SHOW_PAGE" if path.strip("/") else "PROVIDER_ROOT"
    if host in ("max.com", "www.max.com", "hbomax.com", "www.hbomax.com"):
        return "MAX_SHOW_PAGE" if path.strip("/") else "PROVIDER_ROOT"
    return "OTHER_FIRST_PARTY_DISCOVERY_PROBE"


class DocumentMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = None
        self.json_ld = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and a.get("rel", "").lower() == "canonical":
            self.canonical = a.get("href")
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.json_ld = True


def identity(raw, requested, final, case, domains):
    page = update.parse_page(raw)
    meta = DocumentMetadata()
    meta.feed(raw)
    heading = discovery.normalize(page.heading.strip() or page.title.strip())
    named = any(re.search(r"(?<![a-z0-9])" + re.escape(discovery.normalize(a)) + r"(?![a-z0-9])", heading)
                for a in case["aliases"])
    canonical_url = urljoin(final, meta.canonical) if meta.canonical else None
    ok = bool(allowed(final, domains) and canonical(requested) == canonical(final) and named
              and (not canonical_url or allowed(canonical_url, domains) and canonical(canonical_url) == canonical(final)))
    return {"acceptedForAudit": ok, "pageTitle": page.title.strip(), "articleHeading": page.heading.strip(),
            "canonicalUrl": public_url(canonical_url) if canonical_url else None,
            "canonicalDeclared": bool(meta.canonical), "structuredDataAvailable": meta.json_ld,
            "failure": None if ok else "ARTICLE_IDENTITY_MISMATCH"}


def brand_identity(raw, title):
    parser = audit._QualificationText()
    parser.feed(raw)
    text = discovery.normalize(parser.text())
    target = re.escape(discovery.normalize(title))
    brands = []
    for brand, expression in (("HBO", r"hbo original"), ("MAX", r"(?:hbo )?max original")):
        if re.search(expression + r".{0,120}?\b" + target + r"\b", text):
            brands.append(brand)
    return brands  # diagnostic; mixed-brand context is ambiguous, not ownership routing


def run(*, root=update.ROOT, config=None, fetcher=traced_fetch, token="", metadata_loader=discovery.tmdb_metadata, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    before = replay._hash_protected_files(root)
    config = config if config is not None else json.loads(CONFIG.read_text(encoding="utf-8"))
    cases = config["cases"]
    domains = [d["host"] for d in config["domains"] if d.get("probeAllowed", True)]
    eligible = [d["host"] for d in config["domains"] if d.get("evidenceEligible")]
    metadata = metadata_observations(cases, token, metadata_loader)
    aliases = sorted({a for c in cases for a in c["aliases"]})
    cache, paths, matrix = {}, {}, []
    calls = 0

    def get(url, _domains=None, fresh=False):
        nonlocal calls
        key = canonical(url)
        if not allowed(url, domains):
            return {"status":"rejected","failureCategory":"OFFICIAL_DOMAIN_REJECTED"}, None
        if key in cache and not fresh:
            return cache[key]
        if calls >= MAX_FETCHES:
            return {"status":"not_fetched","failureCategory":"AUDIT_BUDGET_EXHAUSTED"}, None
        calls += 1
        result, raw = fetcher(url, domains)
        final = result.get("finalUrl")
        if raw and (result.get("status") != "fetched" or not final or not allowed(final, domains)):
            raw = None
            result = {**result,"status":"rejected","failureCategory":"REDIRECT_REJECTED"}
        meta = DocumentMetadata()
        if raw:
            meta.feed(raw)
        text = audit._QualificationText()
        if raw:
            text.feed(raw)
        access = access_class(result, raw)
        matrix.append({"url": public_url(url), "domain": urlsplit(url).hostname, "sourceFamily": family(url),
            "httpStatus": result.get("httpStatus"), "initialHttpStatus": result.get("initialHttpStatus"),
            "finalUrl": public_url(final) if final and allowed(final, domains) else None,
            "redirectChain": result.get("redirectChain"), "redirectCount": result.get("redirectCount"),
            "contentType": result.get("contentType"), "bodyExtractable": bool(text.text().strip()),
            "accessClassification": access, "failureCategory": result.get("failureCategory"),
            "localOrRunnerAccessibility": access, "requiresJavaScript": None,
            "staticHtmlObserved": bool(raw) if raw else None, "staticHtmlStabilityEstablished": False,
            "structuredDataAvailable": meta.json_ld if raw else None,
            "robotsCdnIndication": "explicit_challenge_marker" if access == "BOT_BLOCKED" else None,
            "githubRunnerAccessibility": access if audit._is_github_actions(None) else "PENDING_RUNNER_CONFIRMATION"})
        if access == "BOT_BLOCKED":
            result, raw = {**result,"status":"rejected","failureCategory":"BOT_BLOCKED"}, None
        cache[key] = result, raw
        return result, raw

    # Reserve budget for all research probes, historical URLs and independent replay.
    queue = deque((u, [u]) for u in config["roots"])
    visited = set()
    while queue and calls < MAX_FETCHES - len(cases) * 2 - 3:
        url, path = queue.popleft()
        key = canonical(url)
        if key in visited:
            continue
        visited.add(key)
        result, raw = get(url)
        if not raw:
            continue
        final = result.get("finalUrl") or url
        links = []
        if urlsplit(final).path.endswith(".xml"):
            links, _sitemap_failure = audit._sitemap_links(raw, domains, aliases)
            links = links[:MAX_LINKS]
        else:
            links = audit._qualification_links(raw, final, domains, aliases, "")[:MAX_LINKS]
        if len(path) <= MAX_DEPTH:
            for link in links:
                if allowed(link, domains):
                    paths.setdefault(canonical(link), path + [public_url(link)])
                    queue.append((link, path + [public_url(link)]))

    rows = []
    for index, case in enumerate(cases, 1):
        url = case["articleUrl"]
        result, raw = get(url)
        final = result.get("finalUrl") or url
        ident = {"acceptedForAudit":False,"failure":"FETCH_UNAVAILABLE"}
        content = {"facts":[],"signals":[],"parserFactCount":0}
        reconstruction = {"result":"not_applicable","reason":"no_fetched_identity_bound_facts"}
        brands = []
        if raw:
            try:
                ident = identity(raw, url, final, case, domains)
                content = audit._qualification_content(raw, final, case, today)
                brands = brand_identity(raw, case["series"])
            except (update.AutomationError, ValueError):
                ident = {"acceptedForAudit":False,"failure":"EXTRACTION_FAILED"}
        is_evidence = family(final) in ("WBD_PRESS_ARTICLE","HBO_SHOW_PAGE","MAX_PRESS") and allowed(final, eligible)
        language_supported = case.get("language", "en") == "en"
        def refetch(u, d):
            fresh_result, fresh_raw = get(u, d, fresh=True)
            if fresh_raw and not identity(fresh_raw, u, fresh_result.get("finalUrl") or u, case, d)["acceptedForAudit"]:
                return {"status":"rejected","failureCategory":"INDEPENDENT_IDENTITY_MISMATCH"}, None
            return fresh_result, fresh_raw
        if ident["acceptedForAudit"] and is_evidence and language_supported and content["facts"]:
            reconstruction = replay._fact_reconstruction({**case,"discoveredUrl":url,"discoveredFinalUrl":final},
                index, eligible, today, refetch)
        route = metadata[case["tmdbId"]].get("routeProposal")
        source_route = brands[0] if len(brands) == 1 else None
        provider_verified = bool(route and route.get("route") and route["route"] == source_route)
        lifecycle = [f for f in content["facts"] if f.get("factType") == "LIFECYCLE"]
        discovered = paths.get(canonical(url))
        parser_gap = bool(raw and (not language_supported or ident["acceptedForAudit"] and content["signals"] and not content["facts"]))
        excerpt = None
        if raw:
            extracted = audit._QualificationText()
            extracted.feed(raw)
            # Short diagnostic only; facts retain their own bound replay evidence.
            excerpt = " ".join(extracted.text().split()[:20])
        stage = ("fetch" if not raw else "identity" if not ident["acceptedForAudit"] else
                 "parser" if parser_gap else "independent_reconstruction" if reconstruction["result"] in ("failed","mismatch") else
                 "provider_identity" if not provider_verified else None)
        rows.append({"caseId":case["caseId"],"title":case["series"],"tmdbId":case["tmdbId"],
            "kind":case["kind"],"researchGoalNotExpectedOutput":case["goal"],
            "requestedOfficialUrl":public_url(url),"sourceFamily":family(final),"identity":ident,
            "discoveryPath":discovered,"discoveryIsEvidence":False,"directProbeOnly":not bool(discovered),
            "genericDiscovery":"DISCOVERABLE_WITH_LIMITATIONS" if discovered else "NOT_GENERICALLY_DISCOVERABLE",
            "providerIdentity":{"tmdbObservation":metadata[case["tmdbId"]],"observedOriginalBrandLabels":brands,
                                "verifiedForAudit":provider_verified,"candidateEcosystemIsNotRouting":True},
            "parserFactsDiagnosticOnly":content["facts"],"signalsDiagnosticOnly":content["signals"],
            "relevantEvidenceTextDiagnosticOnly":excerpt,"languageCoverageSupported":language_supported,
            "parserGap":("PARSER_GAP_LANGUAGE_NOT_SUPPORTED" if not language_supported else "PARSER_GAP_CANDIDATE_REQUIRES_BOUND_CLAIM_REVIEW") if parser_gap else None,
            "independentReconstruction":reconstruction,"failureStage":stage,"verifiedProductionFact":None,
            "negativeSafety":{"lifecycleFacts":lifecycle,"releaseFacts":[f for f in content["facts"] if f.get("factType") != "LIFECYCLE"],
                "targetContentValidated":bool(raw and language_supported and ident["acceptedForAudit"] and is_evidence and not lifecycle),
                "safeIdentityRejection":bool(raw and not ident["acceptedForAudit"] and not content["facts"])} if case["kind"] == "negative" else None})

    trusted = json.loads((root / "official-data/sources.json").read_text(encoding="utf-8"))
    entries = trusted.get("series", trusted.get("sources", [])) if isinstance(trusted, dict) else trusted
    historical = next((e for e in entries if e.get("title") == "The White Lotus"), {})
    old_urls = [u for u in [historical.get("officialUrl"), *historical.get("fallbackUrls", [])] if u]
    for url in old_urls:
        get(url)
    white = [r for r in rows if r["title"] == "The White Lotus"]
    positives = [r for r in rows if r["kind"] == "positive" and r["identity"]["acceptedForAudit"]
                 and r["independentReconstruction"]["result"] == "passed"]
    after = replay._hash_protected_files(root)
    if before != after:
        raise RuntimeError("Protected files changed during WBD/HBO/Max audit")
    web_path = CONFIG.with_name("wbd_hbo_max_web_research.json")
    return {"schemaVersion":1,"generatedAt":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "auditOnly":True,"readOnly":True,"ecosystem":"WBD_HBO_MAX",
        "resultScope":"RUNNER_CONFIRMATION" if audit._is_github_actions(None) else "LOCAL_DIAGNOSTIC_ONLY",
        "knownHistoricalIssue":{"provider":"WBD","issue":"HTTP_403_ON_GITHUB_RUNNER","source":"Prior project documents/user baseline, not inferred from local DNS failure"},
        "routingAssessment":{"observations":list(metadata.values()),"proposedNetworkOnlyRoutes":["HBO","MAX"],
            "legacyWbdRouteAudit":"Existing production WBD groups HBO/HBO Max/Max/WBD and permits productionCompanies/homepage signals. This is documented, not changed.",
            "unknownLabelsNotMapped":True,"availabilityProviderIsNotOriginalNetwork":True},
        "domains":config["domains"],"httpAccessMatrix":matrix,"auditCases":rows,
        "sourceFamilyAnalysis":{"observedFamilies":sorted({r["sourceFamily"] for r in matrix}),"productionEnumsUnchanged":True},
        "discoveryAssessment":{"positiveEvidenceReplayed":len(positives),"genericProductionDiscoveryEstablished":False,
            "budget":{"maximumFetches":MAX_FETCHES,"actualFetches":calls,"maximumDepth":MAX_DEPTH,"maximumLinks":MAX_LINKS},
            "normalTitleDiscoveryStillRequired":True,"researchProbeNeverCountsAsDiscovery":True},
        "identityAssessment":{"http200IsNotIdentity":True,"canonicalAndRedirectChecks":True,"genericHeadingsRejected":True},
        "parserAssessment":{"productionParserUnchanged":True,"positiveReplayedCases":len(positives),
            "gapCases":[r["caseId"] for r in rows if r["parserGap"]]},
        "negativeSafetyAssessment":{"configuredCases":sum(r["kind"]=="negative" for r in rows),
            "contentValidatedCases":sum(bool(r["negativeSafety"] and r["negativeSafety"]["targetContentValidated"]) for r in rows),
            "unavailableIsNeverPass":True},
        "cancellationCoverage":{"status":"CANCELLATION_FIRST_PARTY_COVERAGE_INSUFFICIENT",
            "noInferenceFromAbsenceTmdbOrNoPlans":True},
        "releaseDateCoverage":{"exactDates":[f for r in positives for f in r["parserFactsDiagnosticOnly"] if f.get("releaseDate")],
            "yearOnly":[f for r in positives for f in r["parserFactsDiagnosticOnly"] if f.get("releaseYear") and not f.get("releaseDate")],
            "windowsNeverConvertedToExactDates":True},
        "independentReconstructionAssessment":{"fieldsCompared":list(replay.monitored.FACT_FIELDS),
            "passingCases":[r["caseId"] for r in positives],"persistedMonitoredOrProductionRecordCreated":False},
        "whiteLotusDiagnostic":{"identity":metadata.get(111803),"historicalSources":old_urls,
            "currentSources":[r["requestedOfficialUrl"] for r in white],
            "httpResults":[r for r in matrix if r["url"] in {public_url(u) for u in old_urls}|{r["requestedOfficialUrl"] for r in white}],
            "alternativeFirstPartySources":[r for r in white if r["kind"]=="diagnostic"],
            "genericDiscoveryPossible":any(bool(r["discoveryPath"]) for r in white),
            "conclusion":"Read local/runner HTTP matrix separately from historical 403. No show-page alternative is asserted to prove the same renewal fact without fetched strict evidence."},
        "separateWebResearch":json.loads(web_path.read_text(encoding="utf-8")) if web_path.exists() else None,
        "recommendedProviderModel":"MODEL_B_SEPARATE_HBO_MAX",
        "recommendationConfidence":"PROVISIONAL_B_PENDING_GITHUB_RUNNER",
        "finalConclusion":"INSUFFICIENT_LOCAL_NETWORK_EVIDENCE" if not audit._is_github_actions(None) else "RUNNER_AUDIT_REQUIRES_REVIEW_NO_AUTOMATIC_QUALIFICATION",
        "qualificationStrategyRecommendation":{"proposalOnly":True,"activationGate":False,
            "requirements":"Separate exact network routes and original-brand validation; shared bounded HTTP/press parsing may be reusable but is not qualified. At least three distinct series per proposed route, representative renewal/final/date forms, two independently identity-valid negative cases, strict ambiguity/cross-season safety, all seven replay fields, and repeated real runner access/title-driven discovery. Cancellation remains unsupported without explicit first-party proof. No current production policy lowered."},
        "productionActivationRecommended":False,
        "protectedFileIntegrity":{"before":before,"after":after,"changedFiles":[],"unchanged":True}}


def checked_payload(result):
    sys.path.insert(0, str(update.ROOT / "backend"))
    from check_release_artifacts import PATTERNS, secret_values
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    known = secret_values(update.ROOT) + [v.encode() for k,v in os.environ.items() if len(v)>=12
        and any(s in k.upper() for s in ("TOKEN","SECRET","PASSWORD","API_KEY"))]
    if any(p.search(payload) for p in PATTERNS) or any(v in payload for v in known):
        raise ValueError("Credential scan rejected artifact; no output written")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    if output.name != "wbd-hbo-max-source-audit.json" or output.is_relative_to(update.ROOT.resolve()) and output.parent != update.ROOT.resolve():
        raise ValueError("Use dedicated root artifact or output outside repository")
    result = run(token=discovery.local_tmdb_token())
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(checked_payload(result))
    print(result["finalConclusion"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
