"""Opt-in MAX qualification title punctuation; HBO production never calls this.

Only ellipses belonging to an exact configured identity alias are removed before
the frozen Spanish clause splitter. All lifecycle/season/negation rules remain
the existing parser's rules. Canonical TMDB title and fact fields are preserved.
"""
import re

import spanish_qualification_parser as spanish


def scoped(article,entry):
    aliases=entry.get('aliases') or [entry['title']]
    normalized=[]
    result=dict(article)
    for alias in sorted(aliases,key=len,reverse=True):
        clean=re.sub(r'\.{3}|…','',alias)
        normalized.append(clean)
        if clean==alias:
            continue
        pattern=re.escape(alias).replace(r'\.\.\.',r'(?:\.{3}|…)').replace('…',r'(?:\.{3}|…)')
        pattern=r'(?<!\w)'+pattern+r'(?!\w)'
        for key in ('title','body'):
            result[key]=re.sub(pattern,lambda m:re.sub(r'\.{3}|…','',m[0]),result.get(key,''),flags=re.I)
    return result,{**entry,'aliases':normalized}


def detect(article,entry,today,**kwargs):
    article,entry=scoped(article,entry)
    return spanish.detect(article,entry,today,**kwargs)


def regional_dates(article,entry):
    article,entry=scoped(article,entry)
    return spanish.regional_dates(article,entry)
