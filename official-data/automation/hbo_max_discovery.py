"""Bounded first-party discovery only. No article oracle input or factual parser."""
from collections import deque
from html import unescape
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode, unquote
import xml.etree.ElementTree as ET

import update

HOST = 'press-spain.max.com'
ENTRY_POINTS = tuple('https://' + HOST + p for p in ('/', '/sitemap', '/post'))
MAX_INDEX_PAGES = 24
MAX_SEARCH_PAGES = 24
MAX_LINKS_PER_PAGE = 4000
MAX_ARTICLES = 4000
MAX_CANDIDATES = 8
MAX_DEPTH = 4


def normalize(text):
    text = unicodedata.normalize('NFKC', unescape(text)).casefold()
    text = text.replace('’', "'").replace('‘', "'")
    # Retain accents; collapse punctuation without fuzzy edits or token loss.
    return ' '.join(re.findall(r"[^\W_]+(?:'[^\W_]+)*", text, re.UNICODE))


def title_matches(text, aliases):
    words = normalize(text)
    return any(re.search(r'(?<!\w)' + re.escape(normalize(a)) + r'(?!\w)', words)
               for a in aliases if normalize(a))


def canonical(url):
    """Canonical discovery key; bounded observed listing queries are preserved."""
    try:
        p = urlsplit(unescape(url))
        if not update.allowed(url, [HOST]) or p.hostname != HOST or p.port not in (None, 443):
            return None
        path = p.path.rstrip('/') or '/'
        if re.search(r'%2f|%5c|%2e|[\\\x00-\x20]', path, re.I) or '..' in path.split('/'):
            return None
        if path.startswith('/post/'):
            return urlunsplit(('https', HOST, path, '', ''))
        if not (path in ('/', '/sitemap', '/post') or re.fullmatch(r'/post/page/[1-9][0-9]{0,2}', path)
                or re.fullmatch(r'/sitemap[^/]*\.xml', path)):
            return None
        query = []
        for key, value in parse_qsl(p.query, keep_blank_values=True, max_num_fields=12):
            if key in ('utm_source', 'utm_medium', 'utm_campaign'):
                continue
            if key not in ('limit', 'offset', 'searchKey', 'tab', 'page'):
                return None
            if key in ('limit', 'offset', 'page') and (not value.isdigit() or int(value) > {'limit':100,'offset':10000,'page':1000}[key]):
                return None
            if key == 'searchKey' and (len(value) > 100 or any(ord(c) < 32 for c in value)):
                return None
            if key == 'tab' and not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', value):
                return None
            if key in [k for k, _ in query]:
                return None
            query.append((key, value))
        return urlunsplit(('https', HOST, path, urlencode(sorted(query)), ''))
    except (ValueError, TypeError):
        return None


class IndexLinks(HTMLParser):
    """Discovery keeps structural nav/footer links; scripts are never executed."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchor = None
        self.skip = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.skip = tag
        if not self.skip and tag == 'a' and attrs.get('href') and len(self.links) < MAX_LINKS_PER_PAGE:
            self.anchor = [attrs['href'], []]

    def handle_data(self, text):
        if self.anchor is not None and not self.skip:
            self.anchor[1].append(text[:1000])

    def handle_endtag(self, tag):
        if tag == self.skip:
            self.skip = None
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor[0], ' '.join(self.anchor[1])[:1000]))
            self.anchor = None


def extract(raw):
    """HTML sitemap/listing anchors or bounded XML sitemap loc/lastmod."""
    if re.search(r'<!DOCTYPE|<!ENTITY', raw, re.I) and raw.lstrip().startswith('<?xml'):
        return [], {'format':'XML_REJECTED', 'lastmodPresent':False}
    if re.search(r'<(?:\w+:)?(?:urlset|sitemapindex)\b', raw[:1000]):
        if re.search(r'<!DOCTYPE|<!ENTITY', raw, re.I):
            return [], {'format':'XML_REJECTED', 'lastmodPresent':False}
        try:
            tree = ET.fromstring(raw)
        except ET.ParseError:
            return [], {'format':'XML_INVALID', 'lastmodPresent':False}
        locs = [(n.text or '', '') for n in tree.iter() if n.tag.split('}')[-1] == 'loc']
        return locs[:MAX_LINKS_PER_PAGE], {'format':'XML', 'lastmodPresent':any(n.tag.split('}')[-1]=='lastmod' for n in tree.iter())}
    parser = IndexLinks(); parser.feed(raw)
    return parser.links, {'format':'HTML', 'lastmodPresent':False}


def crawl(get, titles):
    """Only entry points, observed index links and observed GET search templates."""
    queue = deque((u, [u]) for u in ENTRY_POINTS)
    seen = set(); articles = {}; pages = []; search_templates = {}
    def consume(url, path):
        result, raw = get(url)
        final = canonical(result.get('finalUrl') or url)
        # Redirect into an article cannot turn an index into a discovery proof.
        if not raw or not final or final.startswith('https://' + HOST + '/post/') and not re.search(r'/page/\d+$', urlsplit(final).path):
            pages.append({'url':url,'path':path,'fetched':False,'failure':result.get('failureCategory') or 'INDEX_REDIRECT_OR_UNAVAILABLE'})
            return []
        links, shape = extract(raw)
        accepted = []
        for href, anchor in links:
            link = canonical(urljoin(final, href))
            if not link:
                continue
            accepted.append(link)
            lp = urlsplit(link)
            if lp.path.startswith('/post/') and not re.search(r'/page/\d+$', lp.path):
                if link not in articles and len(articles) < MAX_ARTICLES:
                    articles[link] = {'url':link,'anchor':anchor,'path':path + [link]}
                elif link in articles and anchor not in articles[link]['anchor']:
                    articles[link]['anchor'] = (articles[link]['anchor'] + ' ' + anchor)[:2000]
            else:
                if link not in seen and len(path) < MAX_DEPTH and len(queue) < MAX_INDEX_PAGES * 4:
                    queue.append((link, path + [link]))
                query = dict(parse_qsl(lp.query, keep_blank_values=True))
                if lp.path == '/post' and 'searchKey' in query:
                    search_templates.setdefault(link, path + [link])
        pages.append({'url':url,'path':path,'fetched':True,'linksExtracted':len(links),
                      'acceptedLinks':len(accepted), **shape})
        return accepted
    while queue and len(seen) < MAX_INDEX_PAGES:
        url, path = queue.popleft()
        if url in seen:
            continue
        seen.add(url); consume(url, path)
    searches = []
    if search_templates:
        template = sorted(search_templates)[0]
        p = urlsplit(template); query = dict(parse_qsl(p.query, keep_blank_values=True))
        for title in sorted(set(titles), key=lambda x:(normalize(x),x))[:MAX_SEARCH_PAGES]:
            params = {**query, 'searchKey':title, 'offset':'0'}
            url = canonical(urlunsplit((p.scheme,p.netloc,p.path,urlencode(params),'')))
            if url and url not in seen:
                seen.add(url); consume(url, search_templates[template][:-1] + [url])
                searches.append({'title':title,'url':url,'templateObservedAt':search_templates[template][0]})
    diagnostic = {'entryPoints':list(ENTRY_POINTS),'sitemap':{'pages':[p for p in pages if '/sitemap' in urlsplit(p['url']).path]},
                  'postIndex':{'pages':[p for p in pages if urlsplit(p['url']).path.startswith('/post')], 'paginationFollowed':any(urlsplit(p['url']).query for p in pages)},
                  'search':{'status':'OBSERVED_GET_TEMPLATE' if search_templates else 'NO_PUBLIC_TEMPLATE_OBSERVED', 'queries':searches},
                  'candidateBudget':MAX_CANDIDATES,'maximumIndexPages':MAX_INDEX_PAGES,'maximumSearchPages':MAX_SEARCH_PAGES,
                  'maximumLinksPerPage':MAX_LINKS_PER_PAGE,'maximumArticleCandidates':MAX_ARTICLES,
                  'articleCandidatesExtracted':len(articles),'indexPages':len(pages),'knownUrlSeedingUsed':False,
                  'pages':pages,'truncated':bool(queue) or len(articles)>=MAX_ARTICLES}
    return list(articles.values()), diagnostic


def candidates(articles, aliases, route):
    # Slugs conventionally omit apostrophes; this is punctuation handling only.
    slug_aliases = [a.replace("'", '').replace('’', '').replace('‘', '') for a in aliases]
    relevant = [a for a in articles if title_matches(a['anchor'], aliases) or title_matches(unquote(urlsplit(a['url']).path).replace('-', ' '), aliases + slug_aliases)]
    def rank(a):
        text = normalize(a['anchor'] + ' ' + unquote(urlsplit(a['url']).path).replace('-', ' '))
        lifecycle = bool(re.search(r'\b(?:renueva|renovada|renovado|última|final|cancelada|cancelado)\b',text))
        # Routing still requires fresh metadata/article identity, never this rank.
        brand = bool(re.search(r'\bhbo\b' if route=='HBO' else r'\bmax\b',text))
        return (-int(lifecycle), -int(brand), a['url'])
    return sorted(relevant, key=rank)[:MAX_CANDIDATES], len(relevant)
