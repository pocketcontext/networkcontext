"""Natural keys, deterministic record IDs, content hashes and time handling.

record_id() must stay identical to recordId() in pb_hooks/ingest.js.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata
import urllib.parse

ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'
LINKEDIN = 'https://www.linkedin.com'
EPOCH_MIN = datetime(2003, 1, 1, tzinfo=timezone.utc)


class Invalid(ValueError):
    pass


def record_id(collection, natural_key):
    digest = hashlib.sha256(f'{collection}:{natural_key}'.encode()).digest()
    return ''.join(ALPHABET[byte % 36] for byte in digest[:15])


def clean(text, limit=None):
    """NFC text with collapsed whitespace; the form used in keys and hashes."""
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    value = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip()
    return value[:limit] if limit else value


def verbatim(text, limit):
    """Stored text: NFC, trimmed, otherwise as captured."""
    if text is None:
        return ''
    if not isinstance(text, str):
        raise Invalid('text fields must be strings')
    return unicodedata.normalize('NFC', text).strip()[:limit]


def content_hash(fields):
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _path(url):
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except (AttributeError, ValueError):
        return None
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or not (host == 'linkedin.com' or host.endswith('.linkedin.com')):
        return None
    return [urllib.parse.unquote(part) for part in parsed.path.split('/') if part]


def profile_url(url):
    """Canonical https://www.linkedin.com/in/<slug>/ and the slug, or (None, None)."""
    parts = _path(url) if isinstance(url, str) else None
    if not parts or len(parts) < 2 or parts[0] != 'in':
        return None, None
    slug = parts[1].lower()
    if not re.fullmatch(r'[^\s/?#]{2,200}', slug):
        return None, None
    return f'{LINKEDIN}/in/{urllib.parse.quote(slug, safe="-_.~")}/', slug


def organization_url(url):
    """Canonical company or school URL, its universal name and kind."""
    parts = _path(url) if isinstance(url, str) else None
    if not parts or len(parts) < 2 or parts[0] not in ('company', 'school', 'showcase'):
        return None, None, None
    slug = parts[1].lower()
    kind = 'school' if parts[0] == 'school' else 'company'
    return f'{LINKEDIN}/{parts[0]}/{urllib.parse.quote(slug, safe="-_.~")}/', slug, kind


def profile_urn(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*urn:li:(fsd_profile|fs_miniProfile|fs_profile|member):([A-Za-z0-9_-]{1,100})\s*', value)
    if not match:
        return None
    kind, ident = match.groups()
    return f'urn:li:member:{ident}' if kind == 'member' else f'urn:li:fsd_profile:{ident}'


def organization_urn(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*urn:li:(fsd_company|fs_miniCompany|company|organization|fsd_school|school):(\d{1,20})\s*', value)
    if not match:
        return None
    kind, ident = match.groups()
    return f'urn:li:{"school" if "school" in kind else "organization"}:{ident}'


def post_urn(value):
    """urn:li:activity|ugcPost|share:<id>, also found inside a feed update URL."""
    if not isinstance(value, str):
        return None
    match = re.search(r'urn:li:(activity|ugcPost|share):(\d{6,25})', urllib.parse.unquote(value))
    return f'urn:li:{match.group(1)}:{match.group(2)}' if match else None


def comment_urn(value):
    """Canonical urn:li:comment:(<post kind>:<id>,<comment id>) and its post URN."""
    if not isinstance(value, str):
        return None, None
    match = re.search(r'urn:li:comment:\((activity|ugcPost|share):(\d{6,25}),(\d{6,25})\)', urllib.parse.unquote(value))
    if not match:
        return None, None
    kind, post, comment = match.groups()
    return f'urn:li:comment:({kind}:{post},{comment})', f'urn:li:{kind}:{post}'


def thread_key(urn=None, url=None):
    """A conversation's stable thread identifier and its URL."""
    for value in (urn, url):
        if isinstance(value, str):
            match = re.search(r'(2-[A-Za-z0-9_=+\-]{8,200})', urllib.parse.unquote(value))
            if match:
                ident = match.group(1)
                return 'thread:' + ident, f'{LINKEDIN}/messaging/thread/{urllib.parse.quote(ident, safe="-_=")}/'
    if isinstance(urn, str) and re.fullmatch(r'urn:li:[A-Za-z_]+:\S{1,280}', urn.strip()):
        return urn.strip(), ''
    return None, None


def snowflake_time(urn):
    """LinkedIn activity and comment IDs carry a millisecond timestamp in their top 41 bits."""
    match = re.search(r'(?:activity:|,)(\d{6,25})\)?$', urn or '')
    if not match or not urn.startswith(('urn:li:activity:', 'urn:li:comment:')):
        return None
    try:
        moment = datetime.fromtimestamp((int(match.group(1)) >> 22) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if moment < EPOCH_MIN or moment > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return moment


RELATIVE = {'s': 1, 'sec': 1, 'm': 60, 'min': 60, 'h': 3600, 'hr': 3600, 'd': 86400, 'w': 604800, 'mo': 2592000, 'y': 31536000, 'yr': 31536000}


def parse_instant(value):
    """An aware datetime from ISO 8601 (Z or offset); naive values are UTC."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00').replace(' ', 'T', 1))
        except ValueError:
            raise Invalid(f'not an ISO 8601 time: {value!r}') from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def item_time(value, observed, precision=None):
    """(datetime or None, precision) from ISO text, a date, or a relative label such as '3d'."""
    if value in (None, ''):
        return None, 'unknown'
    if not isinstance(value, str):
        raise Invalid('times must be strings')
    text = value.strip().lower()
    if text in ('now', 'just now'):
        return observed, precision or 'approximate'
    match = re.fullmatch(r'(\d{1,4})\s*(s|sec|m|min|h|hr|d|w|mo|y|yr)', text)
    if match:
        return observed - timedelta(seconds=int(match.group(1)) * RELATIVE[match.group(2)]), precision or 'approximate'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return parse_instant(text + 'T00:00:00+00:00'), precision or 'day'
    moment = parse_instant(value)
    return moment, precision or ('exact' if moment.second or moment.microsecond else 'minute')


def db_time(moment):
    """PocketBase date format."""
    if moment is None:
        return ''
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.') + f'{moment.astimezone(timezone.utc).microsecond // 1000:03d}Z'


def parse_since(value, now=None):
    """--since: a duration such as 7d, 12h or 2w, or an ISO date/time."""
    now = now or datetime.now(timezone.utc)
    match = re.fullmatch(r'(\d{1,4})([hdw])', value.strip().lower())
    if match:
        return now - timedelta(seconds=int(match.group(1)) * {'h': 3600, 'd': 86400, 'w': 604800}[match.group(2)])
    text = value.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        text += 'T00:00:00+00:00'
    return parse_instant(text)
