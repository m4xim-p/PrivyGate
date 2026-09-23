"""Enhanced address detector for PrivyGate.

Combines the structural power of the reference two-stage detector
(anchor finding + boundary expansion, works without an explicit marker,
handles dirty MDM records) with precise sentence-boundary trimming so that
no trailing prose is captured (no blanket masking).

Returns ``PIIMatch`` objects compatible with the PrivyGate PII core and
exposes ``parse`` to split an address into granules (zip, region, district,
city, street, house, corpus, flat, ...).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- markers

COUNTRY_RE = re.compile(
    r'^(?:RUS|UKR|TJK|UZB|KAZ|BLR|ARM|GEO|AZE|KGY|MDA|Россия|UNDEF)\b\.?$', re.I)

ZIP_RE = re.compile(r'^\d{6}$')

ADMIN_FULL_RE = re.compile(
    r'\b(?:город|обл(?:асть)?|край|р-н|район|респ(?:ублика)?|округ|'
    r'село|деревня|поселок|пгт|станция|муниципальн\w*|'
    r'сельское поселение|городской округ|платформа)\b', re.I)

STREET_TYPES = (
    r'ул(?:ица)?|пр-кт|проспект|просп|пр-т|пр-зд|пр-д|п-д|проезд|пр|'
    r'пер(?:еулок)?|шос(?:се)?|ш|б-р|бульвар|наб(?:ережная)?|пл(?:ощадь)?|'
    r'туп(?:ик)?|линия|аллея|мкр(?:н)?|микрорайон|кв-л|квартал|тракт|'
    r'дор(?:ога)?|въезд|съезд|тупик|вал'
)
STREET_MARK_RE = re.compile(r'\b(' + STREET_TYPES + r')\b\.?', re.I)

GEO_WORDS = re.compile(
    r'\b(?:обл(?:асть)?|край|р-н|район|респ(?:ублика)?|АО|округ|'
    r'г|гор|пос|с|дер|ст-ца|хут|п|гп|рп|стан|мкр(?:н)?|микрорайон|'
    r'аал|аул|слобода|выс|пгт|б/р|п/р|с/с|д)\b\.?;?$|'
    r'\b(?:г|с|п|дер|пос|хут|ст-ца|гп|рп|стан)\.\s*$',
    re.I,
)
ADMIN_WORDS = re.compile(
    r'\b(?:обл(?:асть)?|край|р-н|район|респ(?:ублика)?|округ|обл\.?)\b\.?|\b(?:ОБЛАСТЬ|КРАЙ|РАЙОН|РЕСПУБЛИКА)\b',
    re.I,
)

HOUSE_KEYWORDS = (
    r'(?:дом|д|здание|корпус|корп|кор|к(?!в)|стр(?:оение)?|лит(?:ера)?|'
    r'кв(?:артира)?|офис|оф|пом(?:ещение|ещ)?|комн(?:ата)?|ком|подъезд|'
    r'этаж|в/ч|общ|владение|влд)'
)
NUMBER = (
    r'(?:'
    r'\d+(?:\s*[а-яА-ЯёЁ])?(?:\s*[/\\]\s*\d+)?'
    r'|[а-яА-ЯёЁ]\d+(?:[а-яА-ЯёЁ])?'
    r'|[А-ЯЁа-яё-]{1,2}\b'
    r'|(?:[Нн]е указано(?: в документе)?)'
    r'|[Нн]ет данных'
    r'|б/н|н/д|нет'
    r')'
)
NUMBER_AFTER_KEYWORD = (
    r'(?:'
    r'\d+(?:\s*[а-яА-ЯёЁ])?(?:\s*[/\\]\s*\d+)?'
    r'|[а-яА-ЯёЁ]\d+(?:[а-яА-ЯёЁ])?'
    r'|[А-ЯЁа-яё-]{1,7}\b'
    r'|(?:[Нn]е указано(?: в документе)?)'
    r'|[Нn]ет данных'
    r'|б/н|н/д|нет'
    r')'
)
TAIL_SEG_KW_RE = re.compile(
    r'[\s,;]*(?:' + HOUSE_KEYWORDS + r')\.?\s*(?:№\s*)?(?:д\.\s*)?' + NUMBER_AFTER_KEYWORD,
    re.I,
)
TAIL_SEG_BARE_RE = re.compile(
    r'[\s,;]*(?:№\s*)?(?:д\.\s*)?' + NUMBER,
    re.I,
)
GLUED_RE = re.compile(r'д\.?\s*\d+(?:[а-яА-ЯёЁ]|/\d+)?(?:\s*к\s*\d+)?')

WORD_RE = re.compile(r'[А-ЯЁа-яёA-Za-z][\w\-\.]*')
GLUED_WORDNUM_RE = re.compile(r'^[А-ЯЁа-яё]+\d+[а-яА-ЯёЁ]?$|^\d+[А-ЯЁа-яё]+$')

SEG_GEO_PREFIX = re.compile(
    r'^(?:г|с|п|дер|д|пос|стан|хут|ст-ца|гп|рп|аал|аул|сл|выс|р-н|обл|край|респ|'
    r'ул|ст|пер|пр-т|просп|ш|б-р|наб|пл|туп|линия|аллея|мкр|кв-л|тракт|дор|въезд|съезд)\.?\s+|'
    r'^(?:г|с|п|гор|д)\.[А-ЯЁа-яё]|'
    r'^(?:пгт|сп|тер|мун|п/р|с/с|б/р|с/о|ж/д)\b\.?\s*|'
    r'^(?:им\.?|имени)\b|'
    r'^(?:село|деревня|поселок|станция|город|муниципальный|сельское поселение|'
    r'городской округ|платформа)\b',
    re.I,
)
TOPO_SUFFIX_RE = re.compile(
    r'^[а-яё]+(?:ово|ево|ино|ыно|ая|ий|ый|ое|ский|ская|цое|ское|ск|цк)$'
)

ORG_MARKER_RE = re.compile(
    r'\b(?:ПАО|ОАО|ЗАО|ООО|НКО|АНО|ФГУП|МУП|ОГРН)\b|\bбанк\b|'
    r'\b(?:отделение|отделения|банкомат|офис|пункт выдачи|компания|организация)\b|'
    r'\bюридический адрес\b', re.I)


# ---------------------------------------------------------------- helpers

def _segment_is_address_like(seg: str, loose: bool = False) -> bool:  # noqa: C901
    seg = seg.strip(' ,;•·')
    if not seg:
        return True
    seg = re.sub(r'\s*\([^)]*\)', '', seg).strip()
    if not seg:
        return True
    if ORG_MARKER_RE.search(seg):
        return False
    # a segment that carries an address marker ("Адрес: 101000") is address-like
    if re.match(r'^(?:адрес|адресу|регистрация|регистрации|проживает|'
                r'зарегистрирован|зарегистрирована|прописка)\b', seg, re.I):
        return True
    if loose:
        if re.match(r'^[а-яё][а-яё\s]+$', seg):
            return True
        if re.match(r'^\d+\s*[а-яёА-ЯЁ]', seg) and len(seg) >= 4:
            return True
        if re.match(r'^\d{1,4}$', seg):
            return True
    if COUNTRY_RE.match(seg):
        return True
    if ZIP_RE.match(seg):
        return True
    if SEG_GEO_PREFIX.match(seg):
        return True
    if GEO_WORDS.search(seg + ';') or ADMIN_WORDS.search(seg):
        return True
    if re.match(r'^[А-ЯЁ][\w\-\.]*(?:\s+[А-ЯЁа-яё][\w\-\.]*){0,3}$', seg):
        return True
    if re.match(r'^[А-ЯЁ][А-ЯЁ\- ]*$', seg) and len(seg) >= 3:
        return True
    if re.match(r'^[а-яё]+(?:\s+[а-яё]+)*$', seg) and len(seg) <= 3:
        return True
    return bool(TOPO_SUFFIX_RE.match(seg))


def _name_token(tok: str) -> bool:
    if not tok:
        return False
    if WORD_RE.match(tok):
        return True
    if re.match(r'^\d+([а-яА-ЯёЁ])?$', tok):
        return True
    if re.match(r'^\d+-?[а-яА-ЯёЁя-яё]*', tok):
        return True
    return bool(GLUED_WORDNUM_RE.match(tok))


# ---------------------------------------------------------------- core

def _find_anchor_spans(text: str) -> list[int] | list[tuple[int, int]]:  # noqa: C901
    anchors: list[int] = []
    for m in STREET_MARK_RE.finditer(text):
        anchors.append(m.start())
    for m in re.finditer(r'\b(?:дом|здание)\b\.?\s*(?:д\.?\s*)?\d+', text, re.I):
        anchors.append(m.start())
    for m in re.finditer(r'\b(?:дом|здание)\b\.?\s*[А-ЯЁа-яё]{1,3}\b', text, re.I):
        anchors.append(m.start())
    for m in re.finditer(r'\bд\.?\s*\d+[а-яА-ЯёЁ]?\b', text):
        anchors.append(m.start())
    for m in re.finditer(r'\bкв(?:артира)?\.?\s*\d+\b', text, re.I):
        anchors.append(m.start())
    machine = (re.search(r'\b(?:RUS|Россия|UKR|TJK|UZB|KAZ|BLR)\b', text, re.I)
               or '•' in text or ';;' in text)
    if (re.search(r'\b(?:г|с|дер|пос|стан|р-н|обл|край)\b\.?\s*[А-ЯЁА-ЯЁа-яё]', text, re.I)
            or machine):
        wc = r'[А-ЯЁ][А-ЯЁа-яё]' if not machine else r'[А-ЯЁа-яё]'
        ch = r'[,\s;]+' if not machine else r'[\w\-]*[,\s;]+'
        pat = r'\b' + wc + r'{2,}' + ch + r'\d{1,3}[а-яА-ЯёЁ]?(?:\s*к\s*\d+|[/\\]\d+)?(?![.\d])'
        for m in re.finditer(pat, text):
            anchors.append(m.start())
    geo_hits = list(ADMIN_WORDS.finditer(text))
    geo_hits += list(ADMIN_FULL_RE.finditer(text))
    geo_hits += list(re.finditer(
        r'(?i:\b(?:г|с|дер|пос|стан|хут|ст-ца|гп|рп))\b\.?\s*,?\s*[А-ЯЁ]', text))
    geo_hits += list(re.finditer(r'\bД\.\s*[А-ЯЁ]{3,}', text, re.I))
    geo_hits += list(re.finditer(
        r'(?i:\b(?:г|с|дер|пос|стан|хут|ст-ца|гп|рп))\b\s+[А-ЯЁ]', text))
    has_country = re.search(r'\b(?:RUS|Россия|UKR|TJK|UZB|KAZ|BLR)\b', text, re.I)
    loose = bool(has_country or '•' in text or ';;' in text)
    if ((len(geo_hits) >= 2 or (len(geo_hits) == 1 and has_country and len(text) <= 100))
            and not anchors):
        first = min(geo_hits, key=lambda h: h.start())
        s = _expand_left(text, first.start(), loose)
        e = _expand_right_segments(text, first.end(), loose=loose)
        return [(s, e)]
    # whole line consists of address-like segments and has an admin/geo
    # marker (or country + loose toponyms): "МОСКОВСКАЯ ОБЛАСТЬ",
    # "RUS, Чеченская, Урус-Мартановский". Require a zip or 2+ geo markers
    # so that "Место рождения город Москва" (a birth place, not an address)
    # is not masked.
    has_zip = bool(re.search(r'\b\d{6}\b', text))
    if (geo_hits or has_country) and not anchors and len(text) <= 120 \
            and (has_zip or len(geo_hits) >= 2):
        segs = [s for s in re.split(r'[,;]', text) if s.strip(' ,;.')]
        if segs and all(_segment_is_address_like(s, loose) for s in segs):
            # skip a leading address marker ("Адрес: 101000, ...")
            start = 0
            marker_match = re.match(
                r'^(?:адрес|адресу|регистрация|регистрации|проживает|'
                r'зарегистрирован|зарегистрирована|прописка)\b\s*[:]?\s*',
                text, re.I)
            if marker_match:
                start = marker_match.end()
            return [(start, len(text))]
    # A bare zip preceded by an address marker ("Индекс 101000",
    # "Проживает по адресу 101000") is an address even without a street.
    if not anchors:
        for m in re.finditer(r'\b\d{6}\b', text):
            before = text[:m.start()]
            if re.search(r'\b(?:индекс|адрес|адресу|регистрация|регистрации|'
                         r'проживает|зарегистрирован|зарегистрирована|прописка)\b',
                         before, re.I):
                anchors.append(m.start())
    # English address markers ("Address: Moscow, Tverskaya 7",
    # "Registered at: 101000, Moscow, Tverskaya 7").
    if not anchors:
        for m in re.finditer(r'\b(?:address|registered at|lives at)\b\s*:?\s*',
                             text, re.I):
            if m.end() < len(text):
                anchors.append(m.end())
    return sorted(set(anchors))


def _expand_left(text: str, pos: int, loose: bool = False) -> int:  # noqa: C901
    start = pos
    while start > 0 and text[start - 1] in ' ,;':
        start -= 1
    if start > 0 and text[start - 1] in '•·':
        start -= 1
    while True:
        m = re.compile(r'(?<!\d)(\d{6})\s*$').search(text, 0, start)
        if m and m.start(1) < start:
            start = m.start(1)
            while start > 0 and text[start - 1] in ' ,;':
                start -= 1
            continue
        seg_end = start
        seg_start = seg_end
        while seg_start > 0 and text[seg_start - 1] not in ',;:':
            seg_start -= 1
        seg = text[seg_start:seg_end]
        if seg.strip(' ,;') == '':
            start = seg_start
            if seg_start == 0:
                return 0
            if text[seg_start - 1] in ',;':
                start = seg_start - 1
                continue
            return start
        if _segment_is_address_like(seg, loose):
            # Stop at an address marker ("Индекс", "Проживает по адресу",
            # "Адрес:") — it is a label, not part of the address.
            if re.match(r'^(?:индекс|адрес|адресу|регистрация|регистрации|'
                        r'проживает|зарегистрирован|зарегистрирована|прописка|'
                        r'address|registered at|lives at)\b', seg, re.I):
                return start
            start = seg_start
            if seg_start == 0:
                return 0
            if text[seg_start - 1] == ':':
                return start
            if text[seg_start - 1] in ',;':
                start = seg_start - 1
                continue
            return start
        return start


def _expand_right_segments(text: str, pos: int, max_seg: int = 8, loose: bool = False) -> int:
    i = pos
    while i < len(text) and text[i] not in ',;:':
        i += 1
    for _ in range(max_seg):
        while i < len(text) and text[i] in ' ,;':
            i += 1
        m = re.compile(r'[^\s,;:]+(?:[^\s,;:]*[^\s,;:])?').match(text, i)
        if not m:
            break
        seg = m.group(0)
        if not _segment_is_address_like(seg, loose):
            break
        i = m.end()
    return i


def _right_name_and_tail(text: str, pos: int) -> int:  # noqa: C901
    i = pos
    m = STREET_MARK_RE.match(text, pos)
    if m:
        i = m.end()
    while i < len(text) and text[i] in ' ,;':
        i += 1
    name_tokens = []
    while i < len(text):
        m = re.match(r'[^\s,;]+', text[i:])
        if not m:
            break
        tok = m.group(0)
        if re.match(r'^(?:' + HOUSE_KEYWORDS + r')\.?$', tok, re.I) or re.match(r'^д\.$', tok):
            break
        if re.match(r'^\d{6}$', tok):
            break
        if _name_token(tok):
            name_tokens.append(tok)
            i += len(tok)
            while i < len(text) and text[i] in ' ':
                i += 1
            if i < len(text) and text[i] in ',;':
                j = i
                while j < len(text) and text[j] in ' ,;':
                    j += 1
                m2 = re.match(r'[^\s,;]+', text[j:]) if j < len(text) else None
                if m2 and (re.match(r'^(?:' + HOUSE_KEYWORDS + r')\.?$', m2.group(0), re.I)
                          or re.match(r'^д\.$', m2.group(0))):
                    break
                i = j
            continue
        break
    while True:
        m = re.compile(r'\s*\([^)]*\)\s*').match(text, i)
        if m:
            i = m.end()
            continue
        m = TAIL_SEG_KW_RE.match(text, i)
        if m and m.end() > i:
            seg = m.group(0)
            last_tok = re.search(r'[А-ЯЁа-яё]{1}\.?$', seg)
            if last_tok:
                j = m.end()
                while j < len(text) and text[j] in ' ,;':
                    j += 1
                nxt = re.match(r'[^\s,;]+', text[j:]) if j < len(text) else None
                if nxt:
                    combined = last_tok.group(0).rstrip('.') + nxt.group(0)
                    if re.match(r'^(?:' + HOUSE_KEYWORDS + r')\.?$', combined, re.I):
                        i = m.start() + last_tok.start()
                        continue
            i = m.end()
            continue
        m = TAIL_SEG_BARE_RE.match(text, i)
        if m and m.end() > i:
            i = m.end()
        else:
            break
    while i < len(text) and text[i] in ' ,;':
        i += 1
    if i < len(text) and text[i] in '.,':
        i += 1
        while i < len(text) and text[i] in ' ':
            i += 1
    return i


# ---------------------------------------------------------------- public

def _trim_to_sentence(text: str, start: int, end: int) -> int:
    """Trim end so the span stops at a sentence boundary, not mid-prose.

    A period is a sentence boundary unless it belongs to an address
    abbreviation (г., ул., д., кв., п., обл., ...) — those are inside the
    address and must be kept. A period after a digit (e.g. "д. 1.") is a
    real sentence end and is trimmed.
    """
    i = end
    while i > start:
        ch = text[i - 1]
        if ch in '.!?;':
            if ch == '.' and _is_abbrev_period(text, i - 1):
                i -= 1
                continue
            return i - 1
        i -= 1
    return end


_ABBREV = frozenset({
    "г", "ул", "д", "кв", "п", "обл", "с", "ст", "к", "р-н", "пер",
    "пр-т", "просп", "ш", "б-р", "наб", "пл", "корп", "стр", "оф",
    "в", "о", "мкр", "тер", "уч", "им", "линия",
})


def _is_abbrev_period(text: str, period_index: int) -> bool:
    start = period_index
    while start > 0 and text[start - 1].isalpha():
        start -= 1
    token = text[start:period_index].casefold()
    return token in _ABBREV


def _strip_settlement_prefix(text: str, start: int) -> int:
    """Advance start past a settlement marker (город, г., село, посёлок, ...).

    These are labels, not part of the settlement name, so they are not masked:
    "101000, город Москва" -> masks "101000, Москва".
    """
    m = re.match(
        r'\s*(?:город|г|село|с|посёлок|поселок|пос|деревня|дер|станица|'
        r'ст-ца|пгт|хутор|хут|аул|слобода|сл)\b\.?\s*',
        text[start:],
        re.I,
    )
    return start + m.end() if m else start


# Service words of an address that are labels, not part of the value.
_ADDRESS_SERVICE_WORDS = re.compile(
    r'\b(?:'
    # settlements
    r'город|г|село|с|посёлок|поселок|пос|п|деревня|дер|станица|ст-ца|пгт|'
    r'хутор|хут|аул|слобода|сл|'
    # regions
    r'область|обл|край|район|р-н|республика|респ|округ|'
    # streets
    r'улица|ул|проспект|пр-т|просп|переулок|пер|шоссе|ш|бульвар|б-р|'
    r'набережная|наб|площадь|пл|тупик|линия|аллея|микрорайон|мкр|квартал|'
    r'кв-л|тракт|дорога|въезд|съезд|'
    # houses
    r'дом|д|здание|корпус|корп|кор|строение|стр|литера|лит|владение|влд|'
    # flats
    r'квартира|кв|офис|оф|помещение|пом|комната|комн|ком'
    r')\b\.?',
    re.I,
)


def _split_address_into_names(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split an address span into name-only sub-spans, excluding service words.

    Splits by comma-separated segments; within each segment the leading service
    word (город, ул., д., ...) is stripped, leaving the name.
    "ул. Ленина, д. 135" -> [(4, 10), (15, 18)]  (Ленина, 135)
    "Нижний Новгород" -> [(44, 59)]  (one name, not two)
    """
    result: list[tuple[int, int]] = []
    i = start
    while i < end:
        # find the next comma-separated segment
        seg_end = i
        while seg_end < end and text[seg_end] not in ',;':
            seg_end += 1
        # strip leading separators and service words within the segment
        j = i
        while j < seg_end and (text[j] in ' ,;' or _ADDRESS_SERVICE_WORDS.match(text, j)):
            m = _ADDRESS_SERVICE_WORDS.match(text, j)
            if m:
                j = m.end()
            else:
                j += 1
        # capture the rest of the segment as one name (may be multi-word)
        k = seg_end
        while k > j and text[k - 1] in ' ,;':
            k -= 1
        if k > j:
            result.append((j, k))
        i = seg_end + 1
    return result


def detect(text: str) -> list[dict]:  # noqa: C901
    """Detect address spans. Returns list of {start, end, text}."""
    anchors = _find_anchor_spans(text)
    machine = bool(re.search(r'\b(?:RUS|Россия|UKR|TJK|UZB|KAZ|BLR|KGZ|ABH)\b', text, re.I)
                   or '•' in text or ';;' in text)
    if anchors and isinstance(anchors[0], tuple):
        spans = [(s, _trim_to_sentence(text, s, e)) for s, e in anchors]
    else:
        spans = []
        for a in anchors:
            if not isinstance(a, int):
                continue
            start = _expand_left(text, a, machine)
            end = _right_name_and_tail(text, a)
            if end <= start:
                end = min(len(text), start + 1)
            spans.append((start, end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 2:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out = []
    for s, e in merged:
        j = e
        while j < len(text) and text[j] in ' ,;':
            j += 1
        while j < len(text) and text[j] in '•·':
            tail = text[j + 1:]
            if not tail.strip(' ,;'):
                break
            sub = detect(tail)
            if sub:
                e = j + 1 + sub[-1]['end']
                j = e
                while j < len(text) and text[j] in ' ,;':
                    j += 1
            else:
                break
        out.append((s, e))
    merged = out
    result = []
    for s, e in merged:
        while s < e and text[s] in ' ,;':
            s += 1
        s = _strip_settlement_prefix(text, s)
        e = _trim_to_sentence(text, s, e)
        while e > s and text[e - 1] in ' ,;':
            e -= 1
        if e > s:
            # Skip bare abbreviations ("ул.", "г.", "д.") that are not a
            # real address (no street name / house number).
            if re.fullmatch(r'\s*(?:ул|г|д|кв|п|обл|с|ст|к|пер|пр-т|просп|ш|'
                            r'б-р|наб|пл|мкр|тер|уч)\.?\s*', text[s:e], re.I):
                continue
            # Skip addresses of organizations/branches (not personal PII):
            # "Отделение банка по адресу: ...", "Банкомат по адресу: ...".
            # Not when a personal address marker sits between the org marker
            # and the address ("ОГРН ...; адрес регистрации: ..." is personal).
            before = text[max(0, s - 60):s]
            org_hits = list(ORG_MARKER_RE.finditer(before))
            if org_hits:
                last_org = org_hits[-1].end()
                between = before[last_org:]
                # "по адресу" is not a personal marker; "адрес регистрации"
                # is. So "Отделение банка по адресу: ..." is an org address,
                # while "ОГРН ...; адрес регистрации: ..." is personal.
                if not re.search(r'\b(?:адрес регистрации|регистрация|'
                                 r'регистрации|проживает|зарегистрирован|'
                                 r'зарегистрирована|прописка)\b', between, re.I):
                    continue
            # Split the address into name-only spans, excluding service words
            # (город, улица, дом, ...). "ул. Ленина, д. 135" -> Ленина, 135.
            for ns, ne in _split_address_into_names(text, s, e):
                result.append({'start': ns, 'end': ne, 'text': text[ns:ne]})
    return result


def mask(text: str, placeholder: str = '[АДРЕС]') -> str:
    out = []
    last = 0
    for sp in detect(text):
        out.append(text[last:sp['start']])
        out.append(placeholder)
        last = sp['end']
    out.append(text[last:])
    return ''.join(out)


# ---------------------------------------------------------------- granules

COUNTRY_PHRASE_RE = re.compile(
    r'^(?:RUS|Россия|РОССИЯ|UKR|TJK|UZB|KAZ|BLR|ARM|GEO|AZE|KGZ|ABH|KGY|MDA'
    r'|Республика\s+\S+|Респ\.?\s*\S+'
    r'|Таджикистан|Узбекистан|Азербайджан|Армения|Грузия|Казахстан|Беларусь'
    r'|Киргизия|Кыргызстан|Молдова|Туркменистан)\b\.?,?$',
    re.I,
)
ZIP_FIELD_RE = re.compile(r'^(\d{6})[\s.]*$')
REGION_FIELD_RE = re.compile(
    r'\b(?:обл(?:асть|\.)?|край|КРАЙ|Респ(?:ублика)?|республика|АО|округ|'
    r'РСО-Алания|автономн\w+\s+округ)\b', re.I)
DISTRICT_FIELD_RE = re.compile(r'\b(?:р-н|район|РАЙОН)\b', re.I)
CITY_PREFIX_RE = re.compile(
    r'^(?:г|гор|пос|п|с|дер|д|пгт|ст-ца|рп|гп|стан|хут|аал|аул)\b\.?\s*', re.I)
CITY_SUFFIX_RE = re.compile(r'\b(?:г|гор)\b\.?$', re.I)
STREET_INNER_RE = re.compile(r'\b(' + STREET_TYPES + r')\b', re.I)
HOUSE_FIELD_RE = re.compile(
    r'^(дом|здание|д|корп(?:ус)?|кор|к|стр(?:оение)?|лит(?:ера)?|'
    r'кв(?:артира)?|офис|оф|пом(?:ещение|ещ)?|комн(?:ата)?|ком|влд|владение|'
    r'в/ч|общ)\b\.?', re.I)
HOUSE_FIELD_GLOBAL_RE = re.compile(
    r'(дом|здание|д|корп(?:ус)?|кор|к|стр(?:оение)?|лит(?:ера)?|'
    r'кв(?:артира)?|офис|оф|пом(?:ещение|ещ)?|комн(?:ата)?|ком|влд|владение)\b\.?\s*'
    r'(?:№\s*)?(?:д\.\s*)?', re.I)

HOUSE_TYPES = {
    'дом': 'house', 'д': 'house', 'здание': 'house',
    'корпус': 'corpus', 'корп': 'corpus', 'кор': 'corpus', 'к': 'corpus',
    'строение': 'stroenie', 'стр': 'stroenie',
    'литера': 'litera', 'лит': 'litera',
    'квартира': 'flat', 'кв': 'flat',
    'офис': 'office', 'оф': 'office',
    'помещение': 'room', 'пом': 'room', 'помещ': 'room',
    'комната': 'room', 'комн': 'room', 'ком': 'room',
    'влд': 'possession', 'владение': 'possession',
}


def _clean_street_value(val: str) -> str:
    val = re.sub(r'^(?:ул|улица|им)\.?\s+', '', val, flags=re.I)
    val = re.sub(r'\s*(?:д|дом)\.?\s*\d+\S*$', '', val, flags=re.I)
    m = re.search(r'\.+$', val)
    if m:
        before = val[:m.start()]
        val = before + '.' if before and before[-1].isupper() else before
    return val.strip(' ,;•·')


def _clean_city_value(val: str) -> str:
    val = re.sub(r'\s*\([^)]*\)', '', val)
    val = CITY_SUFFIX_RE.sub('', val)
    return val.strip(' ,.-')


def _classify_segment(seg: str) -> list[dict]:  # noqa: C901
    seg = seg.strip(' ,;•·')
    if not seg:
        return []
    m = ZIP_FIELD_RE.match(seg)
    if m:
        return [{'type': 'zip', 'value': m.group(1)}]
    if COUNTRY_PHRASE_RE.match(seg):
        return [{'type': 'country', 'value': seg}]
    hm = HOUSE_FIELD_RE.match(seg)
    if hm:
        granules = []
        matches = list(HOUSE_FIELD_GLOBAL_RE.finditer(seg))
        for idx, m2 in enumerate(matches):
            kw = m2.group(1).lower().rstrip('.')
            val_start = m2.end()
            val_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(seg)
            val = seg[val_start:val_end].strip(' .,;№')
            ftype = HOUSE_TYPES.get(kw, 'house')
            granules.append({'type': ftype, 'value': val})
        return granules or [{'type': 'house', 'value': seg}]
    sm = STREET_INNER_RE.search(seg)
    if sm is not None:
        granules = []
        pre = seg[:sm.start()].strip(' ,.')
        post = seg[sm.end():].lstrip(' ,.')
        cm = CITY_PREFIX_RE.match(pre)
        if pre and cm is not None:
            granules.append({'type': 'city', 'value': _clean_city_value(pre[cm.end():].strip())})
        elif pre and post and len(pre.split()) <= 2 and not REGION_FIELD_RE.search(pre):
            granules.append({'type': 'city', 'value': _clean_city_value(pre)})
        if post:
            granules.append({'type': 'street', 'value': _clean_street_value(post)})
        elif pre and not any(g['type'] == 'street' for g in granules) \
            and not CITY_PREFIX_RE.match(pre):
            granules.append({'type': 'street', 'value': pre})
        return granules or [{'type': 'street', 'value': seg}]
    if re.match(r'^\d+([а-яА-ЯёЁ]|/\d+)?$', seg):
        return [{'type': 'house', 'value': seg, 'bare': True}]
    cm = CITY_PREFIX_RE.match(seg)
    if cm:
        return [{'type': 'city', 'value': _clean_city_value(seg[cm.end():])}]
    if REGION_FIELD_RE.search(seg):
        return [{'type': 'region', 'value': seg}]
    if DISTRICT_FIELD_RE.search(seg):
        return [{'type': 'district', 'value': seg}]
    if CITY_SUFFIX_RE.search(seg):
        return [{'type': 'city', 'value': _clean_city_value(seg)}]
    return [{'type': 'place', 'value': seg}]


def parse(text: str) -> list[dict]:  # noqa: C901
    """Detect addresses and split each into granules."""
    result: list[dict] = []
    for sp in detect(text):
        inner = sp['text']
        granules: list[dict] = []
        for seg in re.split(r'[,;]', inner):
            for g in _classify_segment(seg):
                if g.get('bare'):
                    has_house = any(p['type'] in ('house', 'corpus',
                                                  'stroenie', 'litera')
                                    for p in granules)
                    g['type'] = 'flat' if has_house else 'house'
                    del g['bare']
                granules.append(g)
        if any(g['type'] in ('house', 'flat') for g in granules):
            for i, g in enumerate(granules):
                if g['type'] == 'place' and i + 1 < len(granules) and \
                        granules[i + 1]['type'] in ('house', 'flat'):
                    g['type'] = 'street'
                    break
        if any(g['type'] == 'street' for g in granules):
            for g in granules:
                if g['type'] == 'place' and re.match(
                        r'^[А-ЯЁ][а-яё\-\s]+$', g.get('value') or ''):
                    g['type'] = 'city'
                    break
        result.append({'span': sp, 'granules': granules})
    return result
