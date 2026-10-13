"""Quote-request detection (rules first, no AI).

classify() decides what to do with one plain-text group message:
  quote        -> store signal + at least one priced line
  flag         -> looks like a quote but the store/price situation is unclear
  attach_code  -> a lone store code (e.g. "Store code:- p443"); caller attaches it to
                  the same sender's pending quote that has no code, or ignores it
  ignore       -> payment/bank text, chat, greetings, no signals

Built on the existing QuoteBot ideas: store code regex [KYPZCV]+3-4 digits, brand words
(KFC, PHD/Pizza Hut, Costa, Vaango). Improvements: tolerant code format ("k 215", "K-215",
"code:- p443"), codes only count when they exist in Zoho, payment-text blocking, and
measure/size numbers (20 meters, 50mm, Hcw12) are never mistaken for prices.
"""
import re
from dataclasses import dataclass, field

BRAND_RE = re.compile(r'\b(kfc|phd|pizza\s*hut|pizzahut|costa|vaango)\b', re.I)

# Letter prefix + 3-4 digits, with optional space/dash/colon/dot between. The lookbehind stops
# matches inside words (e.g. the "k" at the end of "lock 1800").
CODE_RE = re.compile(r'(?<![A-Za-z0-9])([KYPZCV])\s{0,2}[-:.]?\s{0,2}(\d{3,4})(?!\d)', re.I)

PAYMENT_RE = re.compile(
    r'(account|a/c|\bacc\b|ifsc|\bbank\b|state\s*bank|phone\s*pe|phonepe|phone\s*pay|gpay|'
    r'google\s*pay|\bupi\b|po\s*amount|\bpo\b|holder\s*name)', re.I)

QUOTE_KW_RE = re.compile(r'(quot|quat|question|send|share|amount|total|estimate)', re.I)
STRICT_QUOTE_KW_RE = re.compile(r'(quot|quat|question|estimate)', re.I)
STORE_WORD_RE = re.compile(r'\b(store|stor|mall|branch|outlet)\b', re.I)

_UNITS = (r'mtrs?|meters?|metres?|m|ft|feet|foot|sq\.?\s*ft|sqft|kgs?|gms?|grams?|ltrs?|litres?|'
          r'liters?|l|ml|nos?|pcs?|pieces?|sets?|bags?|rolls?|boxes?|mm|cm|inch|inches')
# A price: 2-6 digits (or 1,234 style), not glued to letters, not followed by a unit word.
PRICE_RE = re.compile(
    r'(?<![A-Za-z0-9.])(?<!\d,)(\d{1,3}(?:,\d{3})+|\d{2,6})(?!\d|,\d|\.\d)(?!\s*(?:' + _UNITS + r')\b)', re.I)

GENERIC_TOKENS = {
    'KFC', 'PHD', 'PH', 'COSTA', 'VAANGO', 'PIZZA', 'HUT', 'HYD', 'HYDERABAD', 'STORE', 'MALL',
    'THE', 'AND', 'SEC', 'CITY', 'OLD', 'NEW', 'ROAD', 'MAIN', 'TS', 'AP', 'MAL', 'RD',
}
LABEL_WORDS = {'store', 'code', 'no', 'number', 'name', 'stor', 'kfc', 'phd'}


@dataclass
class Intake:
    action: str                       # quote | flag | attach_code | ignore
    reasons: list = field(default_factory=list)
    code: str = ''                    # store code found (uppercase), if any
    valid_code: bool = False          # code exists in the Zoho customer list
    brand: bool = False
    place: bool = False
    has_price: bool = False
    default_ok: bool = False          # may be quoted under the DEFAULT customer when the store is unclear


def extract_codes(text):
    """All store-code-like tokens, normalised e.g. 'k 215' -> 'K215'."""
    return [(m.group(1) + m.group(2)).upper() for m in CODE_RE.finditer(text)]


def build_place_tokens(customer_names):
    """Distinctive words from Zoho customer names (e.g. NELLORE, BYPASS, MGB) for place matching."""
    tokens = set()
    for n in customer_names:
        for t in re.split(r'[^A-Za-z]+', n.upper()):
            if len(t) >= 3 and t not in GENERIC_TOKENS:
                tokens.add(t)
    return tokens


def _place_match(text, tokens):
    up = text.upper()
    return any(re.search(r'(?<![A-Z])' + re.escape(t) + r'(?![A-Z])', up) for t in tokens)


def _prices(text, valid_codes):
    """Price-like numbers, ignoring payment lines and valid store codes."""
    cleaned = []
    for line in text.splitlines():
        if PAYMENT_RE.search(line):
            continue
        cleaned.append(line)
    body = '\n'.join(cleaned)
    for c in valid_codes:
        body = re.sub(r'(?<![A-Za-z0-9])' + c[0] + r'\s{0,2}[-:.]?\s{0,2}' + c[1:] + r'(?!\d)', ' ', body, flags=re.I)
    return PRICE_RE.findall(body)


def classify(text, known_codes, customer_names, is_reply=False):
    t = (text or '').strip()
    if is_reply:
        return Intake('ignore', ['reply/tagged message'])
    if len(t) < 8:
        return Intake('ignore', ['too short'])

    tokens = build_place_tokens(customer_names)
    codes = extract_codes(t)
    valid = [c for c in codes if c in known_codes]
    unknown = [c for c in codes if c not in known_codes]
    brand = bool(BRAND_RE.search(t))
    place = _place_match(t, tokens)
    payment = bool(PAYMENT_RE.search(t))
    prices = _prices(t, valid)
    base = dict(code=(valid or codes or [''])[0], valid_code=bool(valid),
                brand=brand, place=place, has_price=bool(prices))

    # Payment / bank text without any store info -> ignore silently
    if payment and not (valid or brand):
        return Intake('ignore', ['payment/bank text'], **base)
    if payment and (valid or brand):
        return Intake('flag', ['payment words mixed with store info'], **base)

    # A lone store code, e.g. "Store code:- p443"
    if codes and len(t) <= 40 and not prices:
        rest = CODE_RE.sub(' ', t)
        words = [w for w in re.findall(r'[A-Za-z]+', rest) if w.lower() not in LABEL_WORDS]
        if not words:
            return Intake('attach_code', ['lone store code'], **base)

    store_signal = bool(valid or brand or place)
    if store_signal and prices:
        reasons = ['store signal + priced line']
        if unknown:
            reasons.append('code not in Zoho list: ' + ','.join(unknown))
        return Intake('quote', reasons, default_ok=True, **base)
    if unknown and prices and not store_signal:
        return Intake('flag', ['store code not in Zoho list: ' + ','.join(unknown)], default_ok=True, **base)
    if prices and (STORE_WORD_RE.search(t) or QUOTE_KW_RE.search(t)):
        # Only with a real quotation word or a store word. A bare "send amount 5000" never becomes a quote.
        ok = bool(STORE_WORD_RE.search(t) or STRICT_QUOTE_KW_RE.search(t))
        return Intake('flag', ['looks like a quote but store not recognised'], default_ok=ok, **base)
    if store_signal and STRICT_QUOTE_KW_RE.search(t):
        return Intake('flag', ['quote request without any price'], **base)
    return Intake('ignore', ['no quote signals'], **base)
