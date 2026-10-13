"""Read-only HBO/MAX qualification. No production registration or writers.

Spanish parser is injected only into the existing independent evidence validator.
Current metadata and fresh fetched articles are required; baseline is historical.
"""
import argparse
import datetime as dt
from functools import partial
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import urlsplit,urlencode

import discovery
import provider_audit as http
import promotion
import update
import fx_hulu_qualification as integrity
import wbd_hbo_max_source_audit as audit
import spanish_qualification_parser as spanish
import hbo_max_discovery as generic

HOST='press-spain.max.com'
SOURCE='Max Press Spain'
ROOTS=generic.ENTRY_POINTS
CONFIG=Path(__file__).with_name('hbo_max_qualification_cases.json')
BASELINE=Path(__file__).with_name('fixtures')/'v2781_hbo_max_runner.json'
BASELINE_SHA='3c63bba62acb5e24342a93443831a123a8dd9ab97c68c838f3f314fab38dbd7e'
MIN_SERIES=3
MAX_FETCHES=120

def allowed(url):
    p=urlsplit(url)
    return update.allowed(url,[HOST]) and p.hostname==HOST and p.port in (None,443)

def evidence_url(url):
    return allowed(url) and urlsplit(url).path.startswith('/post/')

def decode_html(payload,header_charset):
    """Use declared encoding; strict UTF-8 only when none is declared. No guessing."""
    m=re.search(rb'<meta\b[^>]*charset\s*=\s*["\']?\s*([\w-]+)',payload[:8192],re.I)
    charset=header_charset or (m[1].decode('ascii') if m else 'utf-8')
    aliases={'utf8':'utf-8','utf-8':'utf-8','iso-8859-1':'iso-8859-1','windows-1252':'cp1252','cp1252':'cp1252'}
    codec=aliases.get(charset.lower())
    if not codec:
        raise ValueError('UNSUPPORTED_CONTENT_ENCODING')
    text=payload.decode(codec,errors='strict')
    if '\ufffd' in text:
        raise ValueError('INVALID_TEXT_ENCODING')
    return text

def fetch(url,domains=None):
    original=http.fetch_url
    try:
        with patch.object(http,'fetch_url',partial(original,decoder=decode_html)):
            return audit.traced_fetch(url,[HOST] if allowed(url) else ['press.wbd.com'])
    except (UnicodeError,ValueError,LookupError):
        return {'status':'failed','failureCategory':'INVALID_CONTENT_ENCODING'},None

def baseline_proof(path=BASELINE):
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=BASELINE_SHA:
        raise ValueError('Runner baseline checksum mismatch')
    b=json.loads(data.decode('utf-8-sig'))
    if b.get('resultScope')!='RUNNER_CONFIRMATION' or b.get('readOnly') is not True or b.get('qualificationOnly') is not True:
        raise ValueError('Actual V2.7.8.1 runner artifact required')
    urls={r['url'] for r in b['httpAccessMatrix'] if r.get('httpStatus')==200 and r.get('bodyExtractable')}
    if not set(ROOTS)<=urls:
        raise ValueError('Runner did not confirm regional discovery roots')
    return {'artifactSha256':BASELINE_SHA,'sha256':BASELINE_SHA,'resultScope':b['resultScope'],'generatedAt':b['generatedAt'],
            'accessibleRegionalUrls':sorted(u for u in urls if urlsplit(u).hostname==HOST),
            'historicalNetworks':b['currentMetadataObservations'],
            'wbd403Observed':b['wbd403']['observed'],
            'previousQualification':{k:{f:v for f,v in b[k].items() if f!='cases'} for k in ('hbo','max')},
            'historicalOnlyNotCurrentQualificationEvidence':True}

class Editorial(http._QualificationText):
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if any(x in (a.get('class','')+' '+a.get('id','')).casefold() for x in ('latest-news','latest-post','related-post','recommend')):
            attrs=list(attrs)+[('class',a.get('class','')+' related')]
        super().handle_starttag(tag,attrs)
    def text(self):
        blocks=super().text().splitlines()
        kept=[]
        for block in blocks:
            if spanish.normalize(block).casefold().strip() in ('últimas noticias','últimos comunicados','también te puede interesar'):
                break
            kept.append(block)
        return '\n'.join(kept)[:25000]

class Structured(HTMLParser):
    def __init__(self):
        super().__init__();self.active=False;self.current=[];self.scripts=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('type','').lower()=='application/ld+json':
            self.active=True;self.current=[]
    def handle_data(self,data):
        if self.active:
            self.current.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.active:
            self.scripts.append(''.join(self.current)[:100000]);self.active=False

def published(raw,url,heading):
    direct=http.publication_date(raw)
    visible=http._PublishedDate();visible.feed(raw)
    if visible.dates and direct is None:
        return None
    structured=Structured();structured.feed(raw)
    dates=set([direct] if direct else [])
    for script in structured.scripts[:8]:
        try:
            obj=json.loads(script)
        except (ValueError,TypeError):
            continue
        nodes=obj if isinstance(obj,list) else [obj]
        nodes=nodes+[n for o in nodes if isinstance(o,dict) for n in o.get('@graph',[]) if isinstance(n,dict)]
        for node in nodes[:32]:
            if not isinstance(node,dict):
                continue
            types=node.get('@type',[]);types=[types] if isinstance(types,str) else types
            if not set(types)&{'NewsArticle','Article','BlogPosting'}:
                continue
            if spanish.normalize(node.get('headline','')).casefold()!=spanish.normalize(heading).casefold():
                continue
            main=node.get('url') or node.get('mainEntityOfPage')
            if isinstance(main,dict):
                main=main.get('@id')
            if not isinstance(main,str) or audit.canonical(main)!=audit.canonical(url):
                continue
            try:
                dates.add(dt.date.fromisoformat(node['datePublished'][:10]).isoformat())
            except (ValueError,TypeError,KeyError):
                pass
    return next(iter(dates)) if len(dates)==1 else None

def article(raw,url):
    page=update.parse_page(raw);text=Editorial();text.feed(raw)
    heading=page.heading.strip() or page.title.strip()
    return {'title':heading,'body':text.text(),'url':url,'sourceName':SOURCE,
            'publicationDate':published(raw,url,heading),'language':'es','territory':'ES'}

def original_brand(a,entry):
    title=spanish.title_pattern(entry)
    found=set()
    for s in spanish.clauses(a):
        for brand,label in (('HBO',r'hbo'),('MAX',r'(?:hbo\s+)?max')):
            if re.search(rf'\b{label}\s+(?:renueva|ha\s+renovado)\s+{spanish.OBJECT}{title}',s,re.I) or re.search(
                rf'\boriginal\s+de\s+{label}\s*,?\s+(?:(?:ganadora|ganador|del|de|un|emmy|premio|premios)\s*,?\s*){{0,6}}{title}',s,re.I):
                found.add(brand)
    return sorted(found)

def detector(a,entry,today,**kwargs):
    if not evidence_url(a['url']) or a.get('sourceName')!=SOURCE:
        raise update.AutomationError('Qualification provenance rejected')
    return spanish.detect({**a,'language':'es','territory':'ES'},entry,today,**kwargs)

def reconstruction(first,first_regional,raw,url,entry,today,expected):
    """Fresh article -> bounded excerpts -> existing independent validator/summary.

    Original parser facts are used only as expected comparison, never as replay
    input. Fresh claims are re-parsed from persisted-style excerpts by promotion.
    No monitored file or promotion writer is invoked.
    """
    a=article(raw,url)
    fresh=detector(a,entry,today)
    fresh_regional=spanish.regional_dates(a,entry)
    if not fresh:
        return {'result':'failed','reason':'NO_FRESH_LIFECYCLE_FACTS','mismatchedFields':[]}
    evidence=[{**{k:f.get(k) for k in promotion.monitored.EVIDENCE_FIELDS},
               'publicationDateSource':'structured_metadata' if f.get('announcementDate') else 'none'} for f in fresh]
    row={'tmdbId':entry['tmdbId'],'title':entry['title'],'lastChecked':today.isoformat(),
         'parserResult':'facts','validationResult':'passed','eligibleSourceUrls':[url],
         'verifiedFacts':expected,'sourceEvidence':evidence}
    try:
        rebuilt=promotion._validate_evidence(row,[HOST],detector=detector,aliases=entry['aliases'])
        summary=promotion._facts_summary(rebuilt,row,today)
        regional_ok=first_regional==fresh_regional
        return {'result':'passed' if regional_ok else 'failed','fieldsCompared':list(promotion.monitored.FACT_FIELDS),
                'mismatchedFields':[],'regionalDiagnosticMatches':regional_ok,
                'reconstructedFacts':{k:summary.get(k) for k in promotion.monitored.FACT_FIELDS},
                'boundedSourceEvidence':evidence,'freshArticleAndPersistedExcerptReplay':True}
    except promotion.EvidenceMismatch as exc:
        return {'result':'mismatch','fieldsCompared':list(promotion.monitored.FACT_FIELDS),
                'mismatchedFields':exc.diagnostic['fields']}
    except (update.AutomationError,ValueError):
        return {'result':'failed','reason':'INDEPENDENT_EVIDENCE_VALIDATION_FAILED','mismatchedFields':[]}

def shared_safety():
    """Executed synthetic adversarial checks, separate from real-source coverage."""
    now=dt.date(2026,10,12)
    entry={'tmdbId':1,'title':'SERIES A','aliases':['SERIES A']}
    def parse(text,title='SERIES A'):
        e={**entry,'title':title,'aliases':[title]}
        a={'title':'','body':text,'url':ROOTS[0]+'post/fixture','sourceName':SOURCE,'publicationDate':'2026-03-01','language':'es','territory':'ES'}
        return spanish.detect(a,e,now),spanish.regional_dates(a,e)
    tests={}
    def check(group,name,condition):
        tests.setdefault(group,[]).append({'name':name,'passed':bool(condition)})
    text='HBO renueva SERIES A por una tercera temporada. La segunda temporada se estrena el 14 de abril.'
    facts,dates=parse(text)
    check('crossSeason','renewal_does_not_borrow_previous_date',len(facts)==1 and facts[0]['nextSeasonNumber']==3 and facts[0]['releaseDate'] is None and not dates)
    text='HBO renueva SERIES A por una tercera temporada. SERIES B vuelve con su segunda temporada el 10 de abril.'
    fa,da=parse(text);fb,db=parse(text,'SERIES B')
    check('multiTitle','separate_subjects',len(fa)==1 and not fb and not da and len(db)==1)
    fa,_=parse('La quinta y última temporada de SERIES A llega. La tercera temporada de SERIES B llega.','SERIES B')
    check('multiTitle','final_season_not_borrowed',not fa)
    for text in ('SERIES A no ha sido renovada por una tercera temporada.',
                 'SERIES A todavía no ha sido renovada por una tercera temporada.',
                 'SERIES A aún no ha sido renovado por una tercera temporada.',
                 'SERIES A podría ser renovada para una tercera temporada.',
                 'SERIES A espera ser renovada por una tercera temporada.',
                 'No se ha confirmado una tercera temporada de SERIES A.'):
        check('negation',text,not parse(text)[0])
    for text in ('HBO Max lanza el teaser oficial y las primeras imágenes de la tercera temporada de SERIES A.',
                 'El último episodio de la quinta temporada de SERIES A.',
                 'El último tráiler de la temporada de SERIES A.',
                 'La cuarta temporada de SERIES A comienza su rodaje.'):
        check('teaserTrailer',text,not parse(text)[0])
    facts,dates=parse('SERIES A vuelve con su quinta y última temporada el 10 de abril.')
    check('regionalReleaseDate','regional_date_never_global',len(facts)==1 and facts[0]['status']=='FINAL_SEASON' and facts[0]['releaseDate'] is None and dates[0]['regionalReleaseDate']=='2026-04-10')
    return {g:{'scope':'SYNTHETIC_SAFETY_NOT_REAL_SOURCE_COVERAGE','passed':all(c['passed'] for c in checks),'checks':checks} for g,checks in tests.items()}

def qualification_state(rows,shared,runner,accessible):
    positive=[r for r in rows if r['kind']=='positive']
    content_valid=[r for r in positive if r['contentPassed']]
    valid=[r for r in positive if r['passed'] and r.get('mismatchedFields',[])==[]]
    generic=[r for r in valid if r['discoveryMethod']=='DISCOVERED_FROM_FIRST_PARTY_INDEX']
    reconstructed=[r for r in valid if r['independentReconstruction']['result']=='passed']
    counts={'distinctPositiveSeries':len({r['tmdbId'] for r in valid}),
            'genericDiscoveryPassingSeries':len({r['tmdbId'] for r in generic}),
            'independentReconstructionPassingSeries':len({r['tmdbId'] for r in reconstructed})}
    if any(not x['passed'] for name,x in shared.items() if name!='firstPartyNegatives' and x.get('scope')=='SYNTHETIC_SAFETY_NOT_REAL_SOURCE_COVERAGE' and any(not c['passed'] for c in x.get('checks',[]))):
        state='AMBIGUITY_SAFETY_NEEDS_WORK'
    elif not accessible:
        state='RUNNER_ACCESS_NEEDS_WORK' if runner else 'LOCAL_NETWORK_INCONCLUSIVE'
    elif not all(x['passed'] for x in shared.values()):
        state='AMBIGUITY_SAFETY_NEEDS_WORK'
    elif counts['distinctPositiveSeries']<MIN_SERIES:
        state='INSUFFICIENT_DISTINCT_FIRST_PARTY_CASES'
    elif counts['genericDiscoveryPassingSeries']<MIN_SERIES:
        state='DISCOVERY_NEEDS_WORK'
    elif any(not r['passed'] for r in positive if r.get('requiredPositive')) or any(r['independentReconstruction']['result'] in ('mismatch','failed') for r in content_valid) or counts['independentReconstructionPassingSeries']<MIN_SERIES:
        state='PARSER_COMPATIBILITY_NEEDS_WORK'
    else:
        state='QUALIFIED_ON_GITHUB_RUNNER' if runner else 'LOCAL_DIAGNOSTIC_PASS_PENDING_RUNNER'
    return {**counts,'minimumDistinctSeries':MIN_SERIES,'qualificationState':state,'cases':rows}

def resolve_identity(case,token):
    """Exact TMDB identity search for additional first-party corpus titles only."""
    if not token:
        return None
    url='https://api.themoviedb.org/3/search/tv?'+urlencode({'query':case['title'],'language':'en-US'})
    raw,_=update.fetch(url,['api.themoviedb.org'],{'Authorization':'Bearer '+token,'Accept':'application/json'})
    values=json.loads(raw).get('results',[])[:20]
    aliases={discovery.normalize(a) for a in case['aliases']}
    matches={v['id'] for v in values if isinstance(v.get('id'),int) and not isinstance(v['id'],bool) and v['id']>0 and discovery.normalize(v.get('name','')) in aliases}
    return next(iter(matches)) if len(matches)==1 else None

def run(*,root=update.ROOT,config=None,fetcher=fetch,token='',metadata_loader=discovery.tmdb_metadata,today=None,identity_resolver=resolve_identity):
    before=integrity._hash_protected_files(root)
    baseline=baseline_proof()
    config=config or json.loads(CONFIG.read_text(encoding='utf-8'))
    if config.get('minimumDistinctSeriesPerRoute')!=MIN_SERIES:
        raise ValueError('Three-series threshold cannot be lowered')
    today=today or dt.datetime.now(dt.timezone.utc).date()
    cases=[dict(c) for c in config['cases']]
    observations={};metadata_cache={}
    for c in cases:
        if c.get('tmdbId') is None:
            try:
                c['tmdbId']=identity_resolver(c,token)
            except (update.AutomationError,ValueError,OSError):
                c['tmdbId']=None
        if c.get('tmdbId') is None:
            observations[c['caseId']]={'tmdbId':None,'requestedTitle':c['title'],'result':'UNRESOLVED_TMDB_IDENTITY','networks':None,'routeProposal':None}
        else:
            if c['tmdbId'] not in metadata_cache:
                metadata_cache[c['tmdbId']]=audit.metadata_observations([{'series':c['title'],'aliases':c['aliases'],'tmdbId':c['tmdbId']}],token,metadata_loader)[c['tmdbId']]
            observations[c['caseId']]=metadata_cache[c['tmdbId']]
    cache={};matrix=[];calls=0
    def get(url,fresh=False):
        nonlocal calls
        if not (allowed(url) or audit.allowed(url,['press.wbd.com'])):
            return {'status':'rejected','failureCategory':'OFFICIAL_DOMAIN_REJECTED'},None
        if url in cache and not fresh:
            return cache[url]
        if calls>=MAX_FETCHES:
            return {'status':'failed','failureCategory':'QUALIFICATION_BUDGET_EXHAUSTED'},None
        calls+=1
        result,raw=fetcher(url,[HOST] if allowed(url) else ['press.wbd.com'])
        final=result.get('finalUrl') or url
        if raw and (result.get('status')!='fetched' or not (allowed(final) if allowed(url) else audit.allowed(final,['press.wbd.com']))):
            result,raw={**result,'status':'rejected','failureCategory':'REDIRECT_REJECTED'},None
        access=audit.access_class(result,raw)
        if access=='BOT_BLOCKED':
            result,raw={**result,'status':'rejected','failureCategory':'BOT_BLOCKED'},None
        if raw and '\ufffd' in raw:
            result,raw={**result,'status':'failed','failureCategory':'INVALID_CONTENT_ENCODING'},None
        matrix.append({'url':generic.canonical(url) or audit.public_url(url),'httpStatus':result.get('httpStatus'),'finalUrl':generic.canonical(final) or audit.public_url(final),
                       'redirectChain':result.get('redirectChain'),'accessClassification':access,
                       'failureCategory':result.get('failureCategory'),'bodyExtractable':bool(raw)})
        cache[url]=result,raw
        return result,raw
    # The generic crawler has no access to corpus oracles/known article URLs.
    indexes,discovery_diagnostic=generic.crawl(get,[a for c in cases for a in c['aliases']])
    rows=[]
    for c in cases:
        entry={'tmdbId':c['tmdbId'],'title':c['title'],'aliases':c['aliases'],'allowedDomains':[HOST]}
        metadata=observations[c['caseId']];route_proposal=metadata.get('routeProposal') or {}
        route=route_proposal.get('route')
        selected,filtered_count=generic.candidates(indexes,c['aliases'],route)
        paths={a['url']:a['path'] for a in selected}
        candidates=[a['url'] for a in selected]
        oracles=c.get('knownUrlOracle',c.get('knownUrls',[]))
        # Fixed real negative/regional regression documents remain separate from
        # positive discovery. An oracle is never a positive fetch fallback.
        if c['kind']!='positive':
            candidates=[u for u in oracles if evidence_url(u)]
        attempts=[];chosen=None
        for url in candidates[:generic.MAX_CANDIDATES]:
            result,raw=get(url);final=result.get('finalUrl') or url
            ident={'acceptedForAudit':False,'failure':'FETCH_UNAVAILABLE'};facts=[];regional=[];a=None;brands=[]
            if raw:
                try:
                    ident=audit.identity(raw,url,final,{'aliases':c['aliases']},[HOST])
                    a=article(raw,final);brands=original_brand(a,entry)
                    facts=detector(a,entry,today);regional=spanish.regional_dates(a,entry)
                except (ValueError,update.AutomationError):
                    ident={'acceptedForAudit':False,'failure':'CONTENT_VALIDATION_FAILED'}
            matched=bool(route and route==c['candidateRoute'] and brands==[route])
            content_ok=bool(c.get('tmdbId') and raw and ident['acceptedForAudit'] and matched and facts)
            replay={'result':'not_applicable','mismatchedFields':[]};summary={}
            if content_ok:
                try:
                    update.validate_facts(facts,entry)
                    merged,_=update.merge(None,update.canonical_fact_order(facts),today)
                    summary={k:merged.get(k) for k in promotion.monitored.FACT_FIELDS}
                    fresh_result,fresh=get(final,fresh=True)
                    fresh_url=fresh_result.get('finalUrl') or final
                    fresh_identity=audit.identity(fresh,final,fresh_url,{'aliases':c['aliases']},[HOST]) if fresh else {}
                    if fresh and fresh_identity.get('acceptedForAudit') and original_brand(article(fresh,fresh_url),entry)==[route]:
                        replay=reconstruction(facts,regional,fresh,fresh_url,entry,today,summary)
                    else:
                        replay={'result':'failed','reason':'INDEPENDENT_REFETCH_IDENTITY_FAILED','mismatchedFields':[]}
                except (ValueError,update.AutomationError):
                    replay={'result':'failed','reason':'FACT_VALIDATION_FAILED','mismatchedFields':[]}
            discovered=url in paths
            negative_ok=bool(raw and ident['acceptedForAudit'] and matched and not facts)
            ambiguity_ok=bool(raw and not facts)
            passed=(content_ok and discovered and replay['result']=='passed' and replay.get('mismatchedFields',[])==[]) if c['kind']=='positive' else negative_ok if c['kind']=='negative' else ambiguity_ok if c['kind']=='ambiguity' else bool(raw and ident['acceptedForAudit'] and matched and regional and all(x['productionReleaseDate'] is None for x in regional))
            stage='fetch' if not raw else 'identity' if not ident['acceptedForAudit'] else 'routing' if not matched else 'parser' if c['kind']=='positive' and not facts else 'discovery' if c['kind']=='positive' and not discovered else 'independent_reconstruction' if content_ok and replay['result']!='passed' else None
            row={'caseId':c['caseId'],'tmdbId':c['tmdbId'],'requestedTitle':c['title'],'kind':c['kind'],
                 'route':route,'candidateRouteNotIdentity':c['candidateRoute'],'rawNetworkLabels':route_proposal.get('rawNetworkLabels'),
                 'metadataResult':metadata['result'],'discoverySource':[audit.public_url(u) for u in paths.get(url,[])],
                 'discoveryMethod':'DISCOVERED_FROM_FIRST_PARTY_INDEX' if discovered else 'DIRECT_KNOWN_URL_ONLY',
                 'discoveredUrl':audit.public_url(url) if discovered else None,'evidenceSource':audit.public_url(final),
                 'sourceFamily':'MAX_PRESS','language':'es','territory':'ES','httpStatus':result.get('httpStatus'),
                 'identityResult':ident,'originalBrandLabels':brands,'providerIdentityMatched':matched,
                 'parserFacts':facts,'signalsDiagnosticOnly':[name for name,pattern in (
                     ('renewal',r'\brenuev|\brenovad'),('final_season',r'última temporada|temporada final'),
                     ('production_start',r'\brodaje|\brodarse'),('trailer_teaser',r'\bteaser|\btráiler'),
                     ('release',r'\bestrena|\bvuelve|\bregresa')) if a and re.search(pattern,a['title']+' '+a['body'],re.I)],
                 'relevantEvidenceSnippet':([f['evidenceText'] for f in facts]+[d['evidenceText'] for d in regional]) or ([a['title'][:200]] if a else []),
                 **{k:summary.get(k) for k in promotion.monitored.FACT_FIELDS},
                 'sourceName':SOURCE,'sourceUrl':audit.public_url(final),'announcementDate':a.get('publicationDate') if a else None,
                 'regionalReleaseDateDiagnosticOnly':regional,'independentReconstruction':replay,
                 'mismatchedFields':replay.get('mismatchedFields',[]),'contentPassed':content_ok,
                 'passed':bool(passed),'failureStage':None if passed else stage or 'safety',
                 'failureReason':None if passed else result.get('failureCategory') or ident.get('failure') or 'QUALIFICATION_REQUIREMENT_NOT_MET',
                 'negativeTargetContentValidated':negative_ok if c['kind']=='negative' else None}
            attempts.append({'url':audit.public_url(url),'identityAccepted':ident['acceptedForAudit'],'factCount':len(facts),'failureStage':row['failureStage']})
            # Prefer a qualifying replayable positive over a later failed probe.
            if chosen is None or row['passed'] or row['contentPassed'] and not chosen['contentPassed']:
                chosen=row
            if passed:
                break
        if chosen is None:
            chosen={'caseId':c['caseId'],'tmdbId':c['tmdbId'],'requestedTitle':c['title'],'kind':c['kind'],'candidateRouteNotIdentity':c['candidateRoute'],
                    'route':route,'rawNetworkLabels':route_proposal.get('rawNetworkLabels'),'discoverySource':[],
                    'discoveryMethod':'NO_CANDIDATE','discoveredUrl':None,'evidenceSource':None,'sourceFamily':'MAX_PRESS',
                    'language':'es','territory':'ES','httpStatus':None,'identityResult':{'acceptedForAudit':False},
                    'parserFacts':[],'signalsDiagnosticOnly':[],'relevantEvidenceSnippet':[],
                    **{k:None for k in promotion.monitored.FACT_FIELDS},'regionalReleaseDateDiagnosticOnly':[],
                    'independentReconstruction':{'result':'not_applicable','mismatchedFields':[]},'mismatchedFields':[],
                    'contentPassed':False,'passed':False,'failureStage':'discovery','failureReason':'NO_FIRST_PARTY_CANDIDATE'}
        selected_path=paths.get(chosen.get('discoveredUrl'),[])
        chosen.update({'metadataResult':metadata['result'],'discoveryEntryPoint':selected_path[0] if selected_path else None,
                       'candidateCountBeforeFilter':len(indexes),'candidateCountAfterFilter':filtered_count,
                       'candidateCountSelected':len(selected),'knownUrlOracle':oracles,'knownUrlUsedAsSeed':False,
                       'oracleMatched':any(audit.canonical(u)==audit.canonical(chosen.get('evidenceSource') or '') for u in oracles),
                       'sourceName':SOURCE,'sourceUrl':chosen.get('sourceUrl'),'announcementDate':chosen.get('announcementDate'),
                       'providerIdentityMatched':chosen.get('providerIdentityMatched',False),
                       'failureReason':chosen.get('failureReason') if route else route_proposal.get('result') or metadata['result']})
        chosen['requiredPositive']=c.get('requiredPositive',True)
        chosen['attempts']=attempts;rows.append(chosen)
    # Historical WBD URL is a diagnostic probe only, never regional evidence.
    wbd_url='https://press.wbd.com/ca/media-release/hbo-0/hbo-renews-white-lotus-fourth-season'
    wbd_result,_=get(wbd_url)
    safety=shared_safety()
    monthly=[r for r in rows if r['kind']=='ambiguity']
    safety['multiTitle']['realMonthlyCorpusPassed']=bool(monthly and all(r['passed'] for r in monthly))
    safety['multiTitle']['passed']=safety['multiTitle']['passed'] and safety['multiTitle']['realMonthlyCorpusPassed']
    negatives=[r for r in rows if r['kind']=='negative']
    safety['firstPartyNegatives']={'passed':sum(r['passed'] for r in negatives)>=2,'validatedCases':sum(r['passed'] for r in negatives),'requiredCases':2,'unavailableIsNotPass':True}
    accessible=any(m['bodyExtractable'] and urlsplit(m['url']).hostname==HOST for m in matrix)
    runner=http._is_github_actions(None)
    after=integrity._hash_protected_files(root)
    if before!=after:
        raise RuntimeError('Protected files changed during qualification')
    return {'schemaVersion':1,'generatedAt':dt.datetime.now(dt.timezone.utc).isoformat(),'qualificationOnly':True,'readOnly':True,
            'resultScope':'RUNNER_CONFIRMATION' if runner else 'LOCAL_DIAGNOSTIC_ONLY','baseline':baseline,
            'discovery':discovery_diagnostic,
            'sourcePolicy':{'newQualificationHost':HOST,'language':'es','territory':'ES','productionActivated':False,'wildcardTrust':False,'sourceName':SOURCE},
            'routing':{route:[o for o in observations.values() if (o.get('routeProposal') or {}).get('route')==route] for route in ('HBO','MAX')},
            'currentMetadataObservations':list(observations.values()),'httpAccessMatrix':matrix,
            'hbo':qualification_state([r for r in rows if r['candidateRouteNotIdentity']=='HBO'],safety,runner,accessible),
            'max':qualification_state([r for r in rows if r['candidateRouteNotIdentity']=='MAX'],safety,runner,accessible),
            'sharedSafety':safety,'wbd403':{'historicalRunnerObserved':baseline['wbd403Observed'],'observed':wbd_result.get('httpStatus')==403,
                'currentClassification':'WBD_HTTP_403' if wbd_result.get('httpStatus')==403 else wbd_result.get('failureCategory'),'bypassAttempted':False},
            'cancellationCoverage':'CANCELLATION_FIRST_PARTY_COVERAGE_INSUFFICIENT',
            'budget':{'maximumFetches':MAX_FETCHES,'actualFetches':calls,'indexPages':discovery_diagnostic['indexPages']},
            'productionActivation':{'HBO':False,'MAX':False},'protectedFileIntegrity':integrity.protected_integrity(before,after)}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.name!='hbo-max-provider-qualification.json' or output.is_relative_to(update.ROOT.resolve()) and output.parent!=update.ROOT.resolve():
        raise ValueError('Use dedicated root artifact or external runner temp directory')
    result=run(token=discovery.local_tmdb_token());payload=audit.checked_payload(result)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(payload)
    print('HBO:',result['hbo']['qualificationState'],'MAX:',result['max']['qualificationState'])

if __name__=='__main__':
    main()
