"""Bounded read-only FX source research. No routing, writer or activation API.

Known URLs are diagnostic probes only. Generic discovery starts from public
provider surfaces and never treats a probe URL as discovered merely by fetching it.
Uses the existing credential-free, allowlisted provider audit HTTP layer.
"""
from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import provider_audit as audit
import fx_hulu_qualification as qualification
import update

DOMAINS = ["fxnetworks.com"]
ROOTS = ["https://www.fxnetworks.com/", "https://www.fxnetworks.com/shows",
         "https://www.fxnetworks.com/shows/all", "https://www.fxnetworks.com/headlines",
         "https://www.fxnetworks.com/news", "https://www.fxnetworks.com/sitemap.xml",
         "https://www.fxnetworks.com/sitemap_index.xml", "https://www.fxnetworks.com/robots.txt"]
MAX_FETCHES = 48  # includes independent replay/refetches
MAX_DEPTH = 3
MAX_LINKS = 40
CASE_FILE = Path(__file__).with_name("fx_source_strategy_cases.json")
BASELINE = {"passedCases": 2, "totalCases": 4, "qualificationState": "AMBIGUITY_SAFETY_NEEDS_WORK",
            "configuredProviderRootAnnouncements": 0, "distinctPassingProviderAnnouncements": 0,
            "minimumDistinctProviderAnnouncements": 3, "thresholdPossibleFromConfiguredCaseSet": False,
            "source": "V2.7.5 GitHub result supplied by user, not reproduced by this audit"}


def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/", "", ""))


def structure(url):
    parts = urlsplit(url).path.strip("/").split("/")
    if parts == [""] or parts in (["shows"], ["shows", "all"], ["headlines"], ["news"]):
        return "provider_index"
    if parts[-1].endswith(".xml"):
        return "sitemap"
    if parts[0] == "news" and len(parts) > 1:
        return "announcement"
    if parts[0] == "shows" and len(parts) == 2:
        return "series_page"
    if parts[0] == "shows" and parts[-1] in ("headlines", "news"):
        return "series_index"
    if any(p in ("cast", "crew", "cast-crew") for p in parts):
        return "biography"
    if parts[0] == "shows" and len(parts) > 2:
        return "show_subpage"  # path alone cannot establish an announcement
    return "other_first_party"


def identity(raw, requested, final, names):
    if not update.allowed(final, DOMAINS):
        return False, "OFFICIAL_DOMAIN_REJECTED"
    if canonical(requested) != canonical(final):
        return False, "REDIRECT_IDENTITY_UNPROVEN"
    page = update.parse_page(raw)
    heading = page.heading.strip() or page.title.strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", heading.casefold()).strip()
    exact_phrase = any(re.search(r"(?<![a-z0-9])" + re.escape(re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip())
                                + r"(?![a-z0-9])", normalized) for name in names)
    return (True, None) if exact_phrase else (False, "ARTICLE_IDENTITY_MISMATCH")


def protected_hashes(root):
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in qualification.PROTECTED_FILES}


def negative_diagnostic(content, identity_ok):
    no_facts = not content.get("facts")
    return {"facts": content.get("facts", []), "verifiedFacts": None,
            "safeRejectionObserved": not identity_ok and no_facts,
            "targetNegativeContentValidated": identity_ok and no_facts,
            "qualificationCoveragePassed": identity_ok and no_facts,
            "interpretation": "Identity rejection prevents an unsafe fact but does not validate the intended negative article. Never count transport failure as safety coverage."}


def run(*, root=update.ROOT, fetcher=audit.fetch_url, cases=None, roots=None, today=None):
    before = protected_hashes(root)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    cases = cases if cases is not None else json.loads(CASE_FILE.read_text(encoding="utf-8"))["cases"]
    roots = roots if roots is not None else ROOTS
    aliases = sorted({name for c in cases for name in c.get("aliases", [c["series"]])})
    cache, paths, surfaces = {}, {}, []
    calls = 0

    def get(url, domains=DOMAINS, *, fresh=False):
        nonlocal calls
        if not update.allowed(url, DOMAINS):
            return {"status": "rejected", "failureCategory": "OFFICIAL_DOMAIN_REJECTED"}, None
        key = canonical(url)
        if key in cache and not fresh:
            return cache[key]
        if calls >= MAX_FETCHES:
            return {"status": "not_fetched", "failureCategory": "AUDIT_BUDGET_EXHAUSTED"}, None
        calls += 1
        result, raw = fetcher(url, DOMAINS)
        if raw and (result.get("status") != "fetched" or not update.allowed(result.get("finalUrl") or "", DOMAINS)):
            result, raw = {"status": "rejected", "failureCategory": "REDIRECT_REJECTED"}, None
        cache[key] = (result, raw)
        return result, raw

    queue = deque((u, [u]) for u in roots)
    visited = set()
    while queue and calls < MAX_FETCHES - len(cases) * 2:
        url, path = queue.popleft()
        key = canonical(url)
        if key in visited:
            continue
        visited.add(key)
        result, raw = get(url)
        final = result.get("finalUrl") or url
        paths.setdefault(key, path)
        links = []
        title, structured = None, {"jsonLdPresent": False, "publicJsonEndpointEstablished": False}
        if raw:
            if structure(final) == "sitemap":
                try:
                    xml = ET.fromstring(raw)
                    links = sorted({canonical(node.text) for node in xml.iter() if node.tag.endswith("}loc")
                                    and node.text and update.allowed(node.text, DOMAINS)})[:MAX_LINKS]
                except ET.ParseError:
                    pass
            else:
                page = update.Page()
                page.feed(raw)
                title = page.title.strip()
                structured["jsonLdPresent"] = bool(re.search(r'application/ld\+json', raw, re.I))
                # Existing link ranking; target URL deliberately empty (no known-article shortcut).
                links = [canonical(u) for u in audit._qualification_links(raw, final, DOMAINS, aliases, "")[:MAX_LINKS]]
        surfaces.append({"url": url, "fetch": result, "pageTitle": title,
                         "sourceStructure": structure(final), "discoveryPath": path,
                         "eligibleLinks": links, "structuredData": structured})
        if len(path) <= MAX_DEPTH:
            for link in links:
                path_key = canonical(link)
                paths.setdefault(path_key, path + [link])
                queue.append((link, path + [link]))

    evidence = []
    for i, case in enumerate(cases, 1):
        url = case["articleUrl"]
        result, raw = get(url)
        final = result.get("finalUrl") or url
        content = {"facts": [], "signals": [], "pageTitle": None, "parserFactCount": 0}
        ok, reason = False, "FETCH_UNAVAILABLE"
        page_title = None
        if raw:
            try:
                page_title = update.parse_page(raw).title.strip()
                ok, reason = identity(raw, url, final, case.get("aliases", [case["series"]]))
                # Parsing is diagnostic only, also when identity fails. Never a production record.
                content = audit._qualification_content(raw, final, case, today)
            except (ValueError, update.AutomationError):
                reason = "EXTRACTION_FAILED"
        source_structure = structure(final)
        if raw and "career highlights" in raw.casefold():
            source_structure = "biography"
        if (source_structure == "show_subpage" and ok and content["facts"]
                and re.search(r"renewed|final season|cancelled|canceled", content.get("pageTitle") or "", re.I)
                and "watch on" not in (content.get("pageTitle") or "").casefold()):
            source_structure = "announcement"  # content proved a local explicit claim, not path alone
        discovered_path = paths.get(canonical(url)) if canonical(url) not in {canonical(u) for u in roots} else None
        replay = {"result": "not_applicable"}
        if ok and content["facts"] and source_structure in ("announcement", "series_page"):
            replay = qualification._fact_reconstruction(
                {**case, "discoveredUrl": url, "discoveredFinalUrl": final}, i, DOMAINS, today,
                lambda u, domains: get(u, domains, fresh=True))
        body = audit._QualificationText()
        if raw:
            body.feed(raw)
        text = body.text()
        matching = [line.strip() for line in text.splitlines() if re.search(r'final season|season\s+(?:\d+|five)|renew|premier|cancel', line, re.I)]
        snippet = " ".join(" ".join(matching).split()[:25])
        generic = ("GENERICALLY_DISCOVERABLE" if discovered_path and ok and replay["result"] == "passed"
                   else "DISCOVERABLE_WITH_LIMITATIONS" if discovered_path else "NOT_GENERICALLY_DISCOVERABLE")
        evidence.append({"caseId": case["caseId"], "title": case["series"], "kind": case.get("kind", "positive"),
                         "requestedOfficialUrl": url, "fetch": result, "pageTitle": page_title,
                         "parserHeading": content.get("pageTitle"), "sourceStructure": source_structure,
                         "publishedAt": "show surface" if source_structure.startswith("series") else "article/subpage",
                         "discoveryPath": discovered_path, "directProbeOnly": not bool(discovered_path),
                         "identity": {"acceptedForAudit": ok, "failure": reason},
                         "relevantExtract": snippet, "signalsDiagnosticOnly": content["signals"],
                         "parserFactsDiagnosticOnly": content["facts"], "independentReconstruction": replay,
                         "genericDiscovery": generic,
                         "productionParserEvidenceUnambiguous": bool(ok and replay["result"] == "passed"),
                         "negativeSafety": negative_diagnostic(content, ok) if case.get("kind") == "negative" and raw else None,
                         "verifiedProductionFact": None})
    passing = [e for e in evidence if e["kind"] != "negative" and e["productionParserEvidenceUnambiguous"]
               and e["genericDiscovery"] == "GENERICALLY_DISCOVERABLE"]
    provider_articles = {canonical(e["requestedOfficialUrl"]) for e in passing if e["sourceStructure"] == "announcement"}
    article_series = {e["title"] for e in passing if e["sourceStructure"] == "announcement"}
    distinct_series = {e["title"] for e in passing}
    applicable = len(provider_articles) >= 3 and len(article_series) >= 3
    # B is a research recommendation, never activation; availability/stability still needs runner review.
    conclusion = ("FX_PROVIDER_ROOT_MODEL_VALID" if applicable else
                  "FX_REQUIRES_PROVIDER_SPECIFIC_QUALIFICATION_MODEL" if len(distinct_series) >= 3 else
                  "FX_NOT_SUITABLE_FOR_AUTOMATIC_PRODUCTION")
    after = protected_hashes(root)
    changed = [p for p in before if before[p] != after[p]]
    if changed:
        raise RuntimeError("Protected files changed during FX audit: " + ", ".join(changed))
    lookup = {e["caseId"]: e for e in evidence}
    web_file = Path(__file__).with_name("fx_source_strategy_web_research.json")
    web_research = json.loads(web_file.read_text(encoding="utf-8")) if web_file.exists() else None
    return {"schemaVersion": 1, "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "provider": "FX", "auditOnly": True, "readOnly": True,
            "runner": {"githubActions": audit._is_github_actions(None)}, "currentQualification": dict(BASELINE),
            "separateHistoricalWebResearch": web_research,
            "fetchBudget": {"maximum": MAX_FETCHES, "actual": calls, "maxDepth": MAX_DEPTH, "maxLinksPerPage": MAX_LINKS},
            "discoveredLifecycleEvidence": evidence,
            "sourceStructureAnalysis": {"surfaces": surfaces, "classificationIsAuditOnly": True,
                "showPagesNeverCountAsProviderAnnouncements": True,
                "structuredData": "Presence is diagnostic; no public stable JSON API or semantic season grouping inferred."},
            "providerRootAssessment": {"qualifyingAnnouncementsFound": len(provider_articles),
                "existingModelApplicable": applicable, "reason": "Counts require actual root-link discovery, identity, strict bound facts and independent replay. Direct probes and show pages do not count. Absence in bounded audit is not proof of absence on FX."},
            "mayansDiagnostic": lookup.get("fx-mayans-final-s5"),
            "farCryNegativeSafetyDiagnostic": lookup.get("fx-far-cry-cast-negative"),
            "genericDiscoveryAssessment": {"passingDistinctSeries": len(distinct_series),
                "titleInputSource": "Audit corpus only; title-scoped crawl exercises normal-title link ranking without known article priority. It is not an arbitrary-title production adapter.",
                "stabilityEstablished": False, "noProductionEligibilityAsserted": True},
            "recommendedQualificationStrategy": {"conclusion": conclusion,
                "alternativeIsProposalOnly": True, "thresholdUnchanged": 3,
                "candidateModel": "Provider catalog -> exact show identity -> first-party editorial article or locally bound show paragraph -> explicit title/status/season -> unchanged strict parser -> seven-field independent evidence replay. Proposed gates: at least three distinct series and two lifecycle structures, at least two dated editorial lifecycle announcements and at most one historical locally bound show-page final-season control, representative negative/adversarial coverage, no biography/nav/card joins, no inferred year, three separate real runner audits and normal-title discovery demonstration. This alternative is not implemented or approved; the existing three-announcement policy remains unchanged.",
                "availabilityLimit": "A blocked/local audit cannot prove FX inherently unsuitable; C means current automatic-production suitability is not established."},
            "productionActivationRecommended": False,
            "protectedFileIntegrity": {"before": before, "after": after, "changedFiles": [], "unchanged": True}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    # Artifact must stay outside source/production trees. Local root artifact explicitly allowed.
    if output.is_relative_to(update.ROOT.resolve()) and output.parent != update.ROOT.resolve():
        raise ValueError("Audit output must be a root artifact or outside repository")
    if output.name != "fx-source-strategy-audit.json":
        raise ValueError("Use the dedicated audit artifact name")
    result = run()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(checked_payload(result))
    print(result["recommendedQualificationStrategy"]["conclusion"])
    return 0


def checked_payload(result):
    """Fail closed before writing provider-controlled strings to an artifact."""
    sys.path.insert(0, str(update.ROOT / "backend"))
    from check_release_artifacts import PATTERNS, secret_values
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    known = secret_values(update.ROOT)
    known += [v.encode() for k, v in os.environ.items() if len(v) >= 12 and
              any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))]
    if any(p.search(payload) for p in PATTERNS) or any(v in payload for v in known):
        raise ValueError("Credential scan rejected audit artifact; no output written")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
