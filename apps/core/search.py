"""
Forgiving search shared by every list page and search API.

A plain ``icontains`` on the whole query only finds rows that contain the exact
phrase, so "oil filter" misses "Filter - Oil 5W30" and "john mensah" misses a
customer whose first and last names live in separate fields. Here the query is
split into words instead:

* every word has to appear in at least one of the searched fields, in any order
  and any field ("mensah 024" finds Ama Mensah, 0244...);
* when that finds nothing, a typo-tolerant pass runs over the candidates, so
  "filtr" or "mensha" still find their rows.
"""
import re
from difflib import SequenceMatcher

from django.db.models import Q

# How many rows the typo pass will look at. It runs in Python, so it is bounded;
# the scoped querysets it sees (one shop's products/customers) are far smaller.
FUZZY_CANDIDATE_LIMIT = 3000
# Words shorter than this must match exactly — "ab" is too short to guess at.
FUZZY_MIN_LENGTH = 3
FUZZY_CUTOFF = 0.75
# A three-letter word with one slip ("pda" for "pad") scores 0.67.
SHORT_FUZZY_CUTOFF = 0.66

_WORD = re.compile(r'\w+', re.UNICODE)


def split_terms(query):
    """Lower-cased words of the query; punctuation and extra spaces are ignored."""
    return _WORD.findall((query or '').lower())


def _similar(term, word):
    cutoff = SHORT_FUZZY_CUTOFF if len(term) <= 3 else FUZZY_CUTOFF
    if len(word) > len(term):
        # "filt" vs "filters": compare against the start of the longer word.
        word_start = word[:len(term) + 1]
        matcher = SequenceMatcher(None, term, word_start)
        if matcher.real_quick_ratio() >= cutoff and matcher.ratio() >= cutoff:
            return True
    matcher = SequenceMatcher(None, term, word)
    return matcher.real_quick_ratio() >= cutoff and matcher.quick_ratio() >= cutoff \
        and matcher.ratio() >= cutoff


def _term_matches(term, text, words, fuzzy):
    if term in text:
        return True
    if not fuzzy or len(term) < FUZZY_MIN_LENGTH:
        return False
    return any(_similar(term, word) for word in words)


def text_matches(query, *values, fuzzy=False):
    """True when every word of ``query`` appears in one of ``values`` (Python-side lists)."""
    terms = split_terms(query)
    if not terms:
        return True
    text = ' '.join(str(v) for v in values if v).lower()
    words = _WORD.findall(text)
    return all(_term_matches(term, text, words, fuzzy) for term in terms)


def search_queryset(queryset, query, fields, fuzzy=True):
    """
    Filter ``queryset`` to rows matching ``query`` across ``fields``.

    ``fields`` are ORM lookups paths (``'name'``, ``'customer__phone_number'``).
    The queryset's ordering is kept.
    """
    terms = split_terms(query)
    if not terms:
        return queryset

    condition = Q()
    for term in terms:
        any_field = Q()
        for field in fields:
            any_field |= Q(**{f'{field}__icontains': term})
        condition &= any_field
    matched = queryset.filter(condition)
    if not fuzzy or matched.exists():
        return matched

    # Nothing matched word-for-word — try again allowing small typos.
    hits = []
    for row in queryset.values_list('pk', *fields)[:FUZZY_CANDIDATE_LIMIT]:
        if text_matches(query, *row[1:], fuzzy=True):
            hits.append(row[0])
    return queryset.filter(pk__in=hits)
