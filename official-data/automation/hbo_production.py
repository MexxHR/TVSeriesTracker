"""HBO-only wiring of the qualified Spanish path into shared production gates.

No corpus, oracle URLs, qualification runner or writer is used at runtime.
MAX/FX have no production adapter here. Publication/Spanish/discovery semantics
are reused directly from the qualified V2.7.9 implementation.
"""
import discovery
import hbo_max_discovery as generic
import hbo_max_qualification as qualified
import update

HOST = qualified.HOST
SOURCE = qualified.SOURCE
MAX_FETCHES = qualified.MAX_FETCHES


def strict_network(metadata):
    labels = [n.get('name') if isinstance(n,dict) else n for n in metadata.get('networks') or []]
    return bool(labels) and all(isinstance(n,str) and n.strip().casefold()=='hbo' for n in labels)


def identity_context(metadata):
    if not strict_network(metadata):
        raise update.AutomationError('HBO requires strict unambiguous network identity')
    return {k:metadata.get(k) for k in ('tmdbId','title','originalName','hboSpanishTitle','networks')}


def aliases(metadata):
    names=discovery.metadata_names(metadata)
    localized=metadata.get('hboSpanishTitle')
    if isinstance(localized,str) and localized.strip():
        names.append(localized)
    return list(dict.fromkeys(names))


def validate_context(row):
    context=row.get('providerIdentity')
    if (discovery.provider_specs().get('HBO',{}).get('productionActivated') is not True
            or row.get('provider')!='HBO' or not isinstance(context,dict) or not strict_network(context)
            or context.get('tmdbId')!=row['tmdbId'] or context.get('title')!=row['title']):
        raise update.AutomationError('Missing or inconsistent HBO network proof')
    return aliases(context)


def fetch(url,domains):
    if domains!=[HOST] or not generic.canonical(url):
        raise update.AutomationError('HBO source policy rejected URL')
    result,raw=qualified.fetch(url,domains)
    if not raw:
        raise update.ProviderUnavailable(result.get('failureCategory') or 'HBO source unavailable')
    final=result.get('finalUrl') or url
    if not generic.canonical(final):
        raise update.AutomationError('HBO redirect rejected')
    return raw,final


def parse(raw,url):
    if not qualified.evidence_url(url) or not generic.canonical(url):
        raise update.AutomationError('HBO requires exact first-party article')
    return qualified.article(raw,url)


def validate_article(raw,requested,final,entry):
    if not qualified.evidence_url(final) or not generic.canonical(final):
        raise update.AutomationError('HBO article URL rejected')
    identity=qualified.audit.identity(raw,requested,final,{'aliases':entry['aliases']},[HOST])
    if not identity['acceptedForAudit'] or qualified.original_brand(parse(raw,final),entry)!=['HBO']:
        raise update.AutomationError('HBO article title/canonical/provider identity rejected')
    return identity


def detect(article,entry,today,**kwargs):
    if 'MAX' in qualified.original_brand(article,entry):
        raise update.AutomationError('Max original cannot become HBO evidence')
    return qualified.detector(article,entry,today,**kwargs)


def discover(metadata,spec,report,fetcher):
    if not strict_network(metadata) or spec.get('productionActivated') is not True:
        report.update(result='insufficient_evidence',reason='ambiguous_provider')
        return report
    domains=spec['candidateDomains']
    names=aliases(metadata); entry={'tmdbId':metadata['tmdbId'],'title':metadata['title'],'aliases':names}
    count=0;cache={};unavailable=False
    transport=fetch if fetcher is update.fetch else fetcher
    def get(url):
        nonlocal count,unavailable
        if url in cache:
            return cache[url]
        if count>=MAX_FETCHES or not generic.canonical(url):
            return {'failureCategory':'HBO_BUDGET_OR_SOURCE_REJECTED'},None
        count+=1;report['attemptedOfficialUrls'].append(url)
        try:
            raw,final=transport(url,domains)
            if not generic.canonical(final):
                raise update.AutomationError('HBO redirect rejected')
            update.parse_page(raw)
            value=({'status':'fetched','finalUrl':final},raw)
        except (update.AutomationError,ValueError,OSError) as exc:
            unavailable=unavailable or isinstance(exc,update.ProviderUnavailable)
            value=({'failureCategory':discovery.safe_failure(exc)},None)
        cache[url]=value;return value
    articles,diagnostic=generic.crawl(get,names)
    selected,_=generic.candidates(articles,names,'HBO')
    for candidate in selected:
        url=candidate['url'];result,raw=get(url)
        if not raw:
            continue
        final=result['finalUrl']
        try:
            validate_article(raw,url,final,entry)
        except (update.AutomationError,ValueError):
            report['rejectedCandidates'].append({'url':url,'reasons':['HBO_article_identity_or_brand_rejected']})
            continue
        report['candidateUrls'].append(final)
        report['candidates'].append({'candidateUrl':final,'candidateDomain':HOST,'factualParserEligible':True,
            'identityValidated':True,'evidenceLevel':'STRONG','providerRoutingConfidence':'NETWORK',
            'sourceIdentityConfidence':'CONFIRMED','pageType':'official_article','releaseStructure':True,
            'discoveryMethod':'DISCOVERED_FROM_FIRST_PARTY_INDEX','discoveryProvenance':'hbo_first_party_index',
            'discoveryRoot':candidate['path'][0],'discoverySource':candidate['path'],'knownUrlUsedAsSeed':False,
            'sourceAuthority':'official_provider_announcement','requestedUrl':url,'finalUrl':final,
            'redirectWithinOfficialBoundary':True,'reasons':[]})
    report['discoveryDiagnostics']=diagnostic
    report['fetchCount']=count
    if report['candidates']:
        report.update(result='candidate_found',evidenceLevel='STRONG')
    elif unavailable:
        report.update(result='provider_unavailable',reason='official_provider_access_failed')
    else:
        report['reason']='no_valid_official_candidate'
    return report
