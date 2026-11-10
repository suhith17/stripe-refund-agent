"""Literal customer date constraints, with no model-invented year or locale."""

import re
from datetime import date

_MONTHS = {name: number for number, names in enumerate((
    ('january', 'jan'), ('february', 'feb'), ('march', 'mar'), ('april', 'apr'),
    ('may',), ('june', 'jun'), ('july', 'jul'), ('august', 'aug'),
    ('september', 'sep', 'sept'), ('october', 'oct'), ('november', 'nov'),
    ('december', 'dec')), 1) for name in names}
_MONTH = '(?:' + '|'.join(sorted(_MONTHS, key=len, reverse=True)) + r')\.?'
_NAMED = re.compile(
    rf'\b(?:(?P<m1>{_MONTH})\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?'
    rf'|(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m2>{_MONTH}))'
    r'(?:,?\s+(?P<year>\d{4}))?\b', re.I)


def date_constraints(text):
    """Return (year-or-None, month, day), retaining invalid dates as nonmatches.

    A missing year matches all candidate years, so two same-day purchases stay
    ambiguous. Numeric locale-dependent dates such as 10/11 are not interpreted.
    """
    found = {(int(y), int(m), int(d)) for y, m, d in
             re.findall(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)}
    for match in _NAMED.finditer(text):
        month = (match['m1'] or match['m2']).lower().rstrip('.')
        found.add((int(match['year']) if match['year'] else None,
                   _MONTHS[month], int(match['d1'] or match['d2'])))
    return found


def matches_date(purchased_on, constraints):
    value = date.fromisoformat(purchased_on)
    return any((year is None or value.year == year) and
               (value.month, value.day) == (month, day)
               for year, month, day in constraints)
