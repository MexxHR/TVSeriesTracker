"""Generic, opt-in Spanish qualification parser. Never registered in production.

Only explicit local grammatical claims are facts. All extracted dates remain
regional diagnostics; the production-compatible lifecycle summary has null dates.
"""
import datetime as dt
import re
import unicodedata

ORDINALS = dict(zip(('primera','segunda','tercera','cuarta','quinta','sexta','séptima','octava','novena','décima'),range(1,11)))
NUMBER = '(?:'+'|'.join(ORDINALS)+r'|[1-9][0-9]?(?:ª|º)?)'
SEASON = rf'(?:{NUMBER}\s+(?:y\s+última\s+)?temporada|temporada\s+[1-9][0-9]?)'
MONTHS = dict(zip(('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'),range(1,13)))
MONTH = '(?:'+'|'.join(MONTHS)+')'
DATE = rf'(?P<day>\d{{1,2}})\s+de\s+(?P<month>{MONTH})(?:\s+(?:de\s+)?(?P<year>20\d{{2}}))?(?!\d)'
NEGATIVE = re.compile(r'\b(?:no|nunca|todavía|aún|podría|podrían|espera|esperan|posible|posiblemente|rumor|rumores|quizá|quizás|si|ojalá|supuestamente|desmiente|desmentido|falso|falsa|niega)\b',re.I)
APPOSITION = r'(?:,\s*la\s+(?:serie|comedia)(?:\s+(?:original|dramática|ganadora|de|un|una|emmy|premios|varios|hbo|max))*\s*,)?'
CONNECT = r'\s+(?:por|para)\s+(?:(?:una|su|la)\s+)?'
RETURN = r'(?:vuelve|regresa|llegará|llega|se\s+estrena|se\s+estrenará)'
PREFIX = r'(?:la\s+)?(?:serie\s+)?(?:cómica\s+|dramática\s+)?(?:original\s+de\s+(?:hbo|max)\s+)?'
OBJECT = r'(?:(?:su|la)\s+)?(?:nueva\s+)?(?:serie\s+(?:(?:de\s+comedia|dramática|cómica|original)\s+)*)?'

def normalize(text):
    text=re.sub(r'([1-9][0-9]?)\.?[ªº]',r'\1',text)
    text=unicodedata.normalize('NFKC',text).translate(str.maketrans({'‘':"'",'’':"'",'“':'"','”':'"','\xa0':' ','–':' ','—':' ','®':''}))
    return re.sub(r'([1-9][0-9]?)\.([ªº])',r'\1\2',text)

def season_number(text):
    m=re.fullmatch(SEASON,normalize(text).strip(),re.I)
    if not m:
        return None
    for word,n in ORDINALS.items():
        if re.search(r'\b'+word+r'\b',text,re.I):
            return n
    numeric=re.search(r'\b([1-9][0-9]?)(?!\d)',text)
    return int(numeric[1]) if numeric else None

def title_pattern(entry):
    aliases=entry.get('aliases') or [entry['title']]
    return r'(?<!\w)(?:'+'|'.join(re.escape(normalize(a)) for a in sorted(aliases,key=len,reverse=True))+r')(?!\w)'

def clauses(article):
    text=normalize(article.get('title','')+'\n'+article.get('body','')[:25000])
    # U+FFFD is not an accent variant. Fail closed instead of repairing meaning.
    if '\ufffd' in text:
        raise ValueError('INVALID_TEXT_ENCODING')
    return [re.sub(r'\s+',' ',s).strip() for s in re.split(r'[\n.!?;]+',text) if s.strip()]

def lifecycle_claims(sentence,entry):
    title=title_pattern(entry)
    patterns=[
      ('RENEWED',rf'\b(?:hbo\s+max|max|hbo)\s+(?:renueva|ha\s+renovado)\s+{OBJECT}["\']?{title}["\']?{APPOSITION}{CONNECT}(?P<season>{SEASON})'),
      ('RENEWED',rf'{title}\s*,?\s+(?:(?:ha|han)\s+sido\s+|fue\s+)?renovad[ao]{CONNECT}(?P<season>{SEASON})'),
      ('RENEWED',rf'{title}\s+se\s+renueva{CONNECT}(?P<season>{SEASON})'),
      ('FINAL_SEASON',rf'{title}\s+(?:{RETURN}\s+con\s+(?:su|una)\s+|(?:tendrá|anuncia)\s+su\s+)(?P<season>{NUMBER}\s+y\s+última\s+temporada)'),
      ('FINAL_SEASON',rf'(?P<season>{NUMBER}\s+y\s+última\s+temporada)\s+de\s+{PREFIX}{title}'),
      ('FINAL_SEASON',rf'{title}\s+(?:{RETURN}\s+con\s+su\s+|tendrá\s+su\s+)(?P<season>{NUMBER}\s+temporada),?\s+que\s+será\s+la\s+última'),
      ('FINAL_SEASON',rf'{title}\s+(?:{RETURN}\s+con\s+su\s+|tendrá\s+su\s+)(?P<season>temporada\s+[1-9][0-9]?)\s+(?:y\s+)?final'),
    ]
    if NEGATIVE.search(sentence):
        return []
    found=[]
    for status,pattern in patterns:
        for m in re.finditer(pattern,sentence,re.I):
            snippet=m.group(0)
            n=season_number(m['season'])
            if n and len(snippet)<=200:
                # An explicit fifth-and-final renewal is a final season claim.
                actual='FINAL_SEASON' if 'última' in m['season'].casefold() else status
                found.append((actual,n,snippet))
    return found

def detect(article,entry,today,**_kwargs):
    if article.get('language')!='es' or article.get('territory')!='ES':
        raise ValueError('Explicit qualification language/territory required')
    published=article.get('publicationDate')
    if published:
        published=dt.date.fromisoformat(published).isoformat()
    facts=[]
    for sentence in clauses(article):
        for status,n,text in lifecycle_claims(sentence,entry):
            fact={'tmdbId':entry['tmdbId'],'title':entry['title'],'factType':'LIFECYCLE',
                  'status':status,'nextSeasonNumber':n,'releaseDate':None,'releaseYear':None,
                  'sourceName':article['sourceName'],'sourceUrl':article['url'],
                  'announcementDate':published,'rule':'explicit-final-season' if status=='FINAL_SEASON' else 'explicit-renewal',
                  'evidenceText':text}
            if fact not in facts:
                facts.append(fact)
    return facts

def regional_dates(article,entry):
    """Dates are never merged into production fields, even in global-looking copy."""
    title=title_pattern(entry)
    date_prefix=r'\s+(?:el\s+)?(?:próximo\s+)?(?:(?:lunes|martes|miércoles|jueves|viernes|sábado|domingo)\s+)?'
    patterns=[
      rf'{title}\s+{RETURN}\s+con\s+su\s+(?P<season>{SEASON}){date_prefix}{DATE}',
      rf'(?P<season>{SEASON})\s+de\s+{PREFIX}{title}\s+{RETURN}{date_prefix}{DATE}',
      rf'{title}\s+(?P<season>{SEASON})\s+{RETURN}{date_prefix}{DATE}',
    ]
    result=[]
    for sentence in clauses(article):
        if NEGATIVE.search(sentence):
            continue
        for pattern in patterns:
            for m in re.finditer(pattern,sentence,re.I):
                if len(m[0])>200:
                    continue
                year=int(m['year']) if m['year'] else None
                publication=article.get('publicationDate')
                if not year and publication:
                    pub=dt.date.fromisoformat(publication)
                    # Same calendar year only, with a bounded forward interval.
                    try:
                        candidate=dt.date(pub.year,MONTHS[m['month'].casefold()],int(m['day']))
                    except ValueError:
                        continue
                    if 0<=(candidate-pub).days<=183:
                        year=pub.year
                date=None
                if year:
                    try:
                        date=dt.date(year,MONTHS[m['month'].casefold()],int(m['day'])).isoformat()
                    except ValueError:
                        continue
                diagnostic={'scope':'REGIONAL_RELEASE_DATE','language':'es','territory':'ES',
                    'nextSeasonNumber':season_number(m['season']),'regionalReleaseDate':date,
                    'releaseYear':year,'productionReleaseDate':None,'productionReleaseYear':None,
                    'evidenceText':m[0]}
                if diagnostic not in result:
                    result.append(diagnostic)
    return result
