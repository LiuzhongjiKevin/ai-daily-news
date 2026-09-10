"""Conservative quantity equivalence, not semantic or financial fact checking."""

import re
import unicodedata
from collections import Counter
from decimal import Decimal

_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December')
_ZH_MONTHS = ('一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二')
_SCALES = {'': 1, 'k': 1000, 'thousand': 1000, 'm': 1000000, 'million': 1000000,
           'b': 1000000000, 'bn': 1000000000, 'billion': 1000000000,
           'tn': 1000000000000, 'trillion': 1000000000000,
           '千': 1000, '万': 10000, '亿': 100000000, '万亿': 1000000000000}
_UNITS = {'$': 'USD', 'us$': 'USD', 'usd': 'USD', 'dollars': 'USD', '美元': 'USD',
          '£': 'GBP', 'gbp': 'GBP', 'pounds': 'GBP', '英镑': 'GBP',
          '€': 'EUR', 'eur': 'EUR', 'euros': 'EUR', '欧元': 'EUR',
          'cny': 'CNY', '人民币': 'CNY', '元': 'CNY',
          '%': 'percent', 'percent': 'percent', 'per cent': 'percent',
          'percentage points': 'percentage-points', '个百分点': 'percentage-points', '月': 'month'}
_NUMBER = re.compile(
    r'(?P<prefix>US\$|USD\s*|GBP\s*|EUR\s*|CNY\s*|[$£€])?\s*'
    r'(?P<value>[+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*'
    r'(?P<scale>万亿|亿|万|千|(?:trillion|billion|million|thousand|tn|bn|[kmb])(?![a-z]))?\s*'
    r'(?P<unit>美元|英镑|欧元|人民币|元|个百分点|月|%|'
    r'(?:percentage points|per cent|percent|dollars|pounds|euros|USD|GBP|EUR|CNY)(?![a-z]))?',
    re.IGNORECASE,
)


def _quantities(text):
    text = unicodedata.normalize('NFKC', text).replace('−', '-')
    # Capitalized full month names avoid treating the English modal "may" as a date.
    for number, month in enumerate(_MONTHS, 1):
        text = re.sub(r'\b' + month + r'\b', f'{number}月', text)
    for number, month in reversed(list(enumerate(_ZH_MONTHS, 1))):
        text = re.sub(r'(?<![一二三四五六七八九十])' + month + '月', f'{number}月', text)
    values = Counter()
    for match in _NUMBER.finditer(text):
        number = Decimal(match['value'].replace(',', ''))
        number *= _SCALES[(match['scale'] or '').lower()]
        prefix = _UNITS.get((match['prefix'] or '').strip().lower(), '')
        unit = _UNITS.get((match['unit'] or '').lower(), '')
        # Conflicting currency or quantity labels must not be silently collapsed.
        dimension = prefix if not unit or prefix == unit else (prefix + '/' + unit if prefix else unit)
        values[(dimension, number)] += 1
    return values


def numbers_equivalent(original, translated):
    return _quantities(original) == _quantities(translated)
