"""Customer, item and HSN matching.

Customer matching follows the existing QuoteBot order (store code first, then brand filter, then
location) but is deterministic first and never guesses:
  1. store code from the message (exact customer-name match; two different codes -> ambiguous)
  2. brand filter (KFC / PHD / COSTA / VAANGO) from header text or code prefix
  3. location scoring on distinctive words of Zoho customer names (handles joined words like
     "Shankar pally" and small spelling slips like "gurunank")
  4. AI only when the deterministic step finds nothing or a tie; accepted only if it says HIGH
     confidence, names no alternatives, and its evidence words really appear in the message.
Anything else returns status 'ambiguous' or 'none' so the caller flags it to Telegram.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from . import groq_client
from .aliases import ALIASES
from .intake import CODE_RE, PRICE_RE, extract_codes, GENERIC_TOKENS

BRAND_PREFIXES = {'K': 'KFC', 'Y': 'KFC', 'P': 'PHD', 'Z': 'PHD', 'C': 'COSTA', 'V': 'VAANGO'}
STOP_WORDS = GENERIC_TOKENS | {
    'PLEASE', 'SHARE', 'QUOTATION', 'QUOTE', 'QUESTION', 'SEND', 'NAME', 'CODE', 'KINDLY', 'WORK',
    'AMOUNT', 'TOTAL', 'URGENT', 'FOR', 'ASAP', 'MINI',
}
# 'MINI' is generic-ish noise in customer names only when alone; keep it out of scoring words.
STOP_WORDS.discard('MINI')


@dataclass
class MatchResult:
    status: str                      # matched | ambiguous | none
    customer: dict = None
    candidates: list = field(default_factory=list)   # customer names, for the Telegram alert
    reason: str = ''
    method: str = ''                 # code | location | ai
    assumed_brand: str = ''          # set when the brand came from the sender's earlier message


# ── helpers ───────────────────────────────────────────────────────────────────
def _has_code(cname, code):
    c = cname.upper()
    return c.endswith(f'-{code}') or f'-{code}-' in c or c == code


def known_codes(customers):
    codes = set()
    for c in customers:
        for m in re.finditer(r'(?:^|-)([KYPZCV]\d{3,4})(?=-|$)', c.get('contact_name', '').upper()):
            codes.add(m.group(1))
    return codes


def detect_brand(text, code=None):
    up = (text or '').upper()
    if 'KFC' in up:
        return 'KFC'
    if any(b in up for b in ['PHD', 'PIZZA HUT', 'PIZZAHUT', 'PH ']):
        return 'PHD'
    if 'COSTA' in up:
        return 'COSTA'
    if 'VAANGO' in up:
        return 'VAANGO'
    if code:
        return BRAND_PREFIXES.get(code[0])
    return None


def customer_brand(name):
    """Brand of a Zoho customer: from the name (KFC / PHD / PH- / COSTA / VAANGO / V-) or, when the name
    has no brand word (e.g. KALPETTA-WAYANAD-Y118, RMZ-SKY-VIEW-HYD-C379), from its store-code prefix."""
    up = (name or '').upper()
    if 'KFC' in up:
        return 'KFC'
    if 'PHD' in up or up.startswith('PH-') or up.startswith('PH ') or 'PIZZA HUT' in up:
        return 'PHD'
    if 'COSTA' in up:
        return 'COSTA'
    if 'VAANGO' in up or up.startswith('V-'):
        return 'VAANGO'
    m = re.search(r'(?:^|-)([KYPZCV])\d{3,4}(?=-|$)', up)
    return BRAND_PREFIXES.get(m.group(1)) if m else None


def brand_filter(customers, brand):
    """Customers of this brand. Customers whose brand cannot be told are kept in every pool."""
    if not brand:
        return customers
    out = [c for c in customers if customer_brand(c.get('contact_name', '')) in (brand, None)]
    return out or customers


def header_text(message, ai_name=''):
    """Store-identifying lines: up to 3 lines before the first priced / item / 'work' line."""
    header = []
    for line in [l.strip() for l in (message or '').splitlines() if l.strip()][:4]:
        if re.match(r'^\s*(work|items?|description|\d+\s*[\.\)\-])', line, re.I):
            break
        if PRICE_RE.search(CODE_RE.sub(' ', line)):
            break
        header.append(line)
    return (' '.join(header) + ' ' + (ai_name or '')).strip()


def _words(s):
    return [w for w in re.findall(r'[A-Za-z]+', (s or '').upper()) if len(w) >= 3 and w not in STOP_WORDS]


def _cust_tokens(name):
    return [t for t in re.findall(r'[A-Za-z]+', name.upper()) if len(t) >= 3 and t not in STOP_WORDS]


def _form_hit(t, up, compact, words):
    if len(t) >= 5 and t in compact:              # joined words: "SHANKAR PALLY" -> SHANKARPALLY
        return True
    if len(t) < 5 and re.search(r'(?<![A-Z])' + re.escape(t) + r'(?![A-Z])', up):
        return True                               # short tokens (MGB, NAD) need a whole-word match
    return len(t) >= 5 and any(len(w) >= 5 and SequenceMatcher(None, w, t).ratio() >= 0.85 for w in words)


def _matched_tokens(header, tokens):
    up = header.upper()
    compact = re.sub(r'[^A-Z0-9]', '', up)
    words = _words(header)
    return {t for t in tokens if any(_form_hit(f, up, compact, words) for f in [t] + ALIASES.get(t, []))}


def _is_subseq(e, t):
    it = iter(t)
    return all(ch in it for ch in e)


def _related(e, t):
    """Evidence word e relates to customer-name token t: equal, near-equal, contained, or an
    abbreviation (VJA ~ VIJAYAWADA, GNT ~ GUNTUR)."""
    return (e == t or (len(e) >= 4 and (e in t or t in e)) or SequenceMatcher(None, e, t).ratio() >= 0.85
            or (len(e) >= 3 and e[0] == t[0] and _is_subseq(e, t)))


def _evidence_ok(evidence, header, customer_name):
    """The AI's evidence words must (a) really appear in the message and (b) relate to the customer it chose."""
    compact = re.sub(r'[^A-Z0-9]', '', header.upper())
    words = _words(header)
    toks = _cust_tokens(customer_name)
    for e in _words(evidence):
        in_message = e in compact or any(SequenceMatcher(None, e, w).ratio() >= 0.85 for w in words)
        if in_message and any(_related(e, t) for t in toks):
            return True
    return False


# ── customer matching ─────────────────────────────────────────────────────────
def _ai_pick(header, pool, llm):
    names = '\n'.join(f"{i+1}. {c['contact_name']}" for i, c in enumerate(pool))
    prompt = f"""Match this store description to the correct customer from the list below.

Input: "{header}"

Customers:
{names}

Rules:
1. Match by LOCATION: find the customer whose location words best match the input.
2. If two or more customers fit equally well, do NOT choose; list them in "alternatives".
3. Reply with ONLY this JSON:
{{"match": <number or 0 if no good match>, "confidence": "high" | "medium" | "low",
  "alternatives": [<numbers of other plausible customers>],
  "evidence": "<words copied from the input that identify the location>"}}"""
    return llm(prompt)


def match_customer(message, ai_name, customers, llm=None, context_brand=None):
    """Return a MatchResult. `llm(prompt)` returns the parsed JSON (defaults to Groq).
    `context_brand`: brand from the same sender's earlier message (used only when this message has no
    brand word and no store code)."""
    llm = llm or (lambda p: groq_client.chat_json(p, max_tokens=300, temperature=0, expect=dict))
    # Store code — attempt 1: the AI-parsed customer name; attempt 2: the raw WhatsApp message.
    known = known_codes(customers)
    name_codes = extract_codes(ai_name or '')
    raw_codes = extract_codes(message)
    codes = raw_codes + [c for c in name_codes if c not in raw_codes]
    valid_name = sorted({c for c in name_codes if c in known})
    valid_raw = sorted({c for c in raw_codes if c in known})
    if valid_name and valid_raw and set(valid_name) != set(valid_raw):
        return MatchResult('ambiguous', None, [], 'parsed name and message show different store codes: '
                           f'{", ".join(valid_name)} vs {", ".join(valid_raw)}', 'code')
    valid = valid_name or valid_raw
    unknown = sorted({c for c in codes if c not in known})
    note = f'code {",".join(unknown)} not found in Zoho; ' if unknown and not valid else ''

    # 1. store code
    if len(valid) > 1:
        return MatchResult('ambiguous', None, [], f'multiple store codes in one message: {", ".join(valid)}', 'code')
    if len(valid) == 1:
        hits = [c for c in customers if _has_code(c['contact_name'], valid[0])]
        if len(hits) == 1:
            return MatchResult('matched', hits[0], [hits[0]['contact_name']], f'store code {valid[0]}', 'code')
        if len(hits) > 1:
            return MatchResult('ambiguous', None, [h['contact_name'] for h in hits],
                               f'store code {valid[0]} matches several customers', 'code')

    # 2-4. Brand, then location. Brand priority:
    #   explicit brand word / store-code prefix  >  sender's recent brand (context)  >  KFC default
    header = header_text(message, ai_name)
    explicit = detect_brand(header, (valid or codes or [None])[0])
    if explicit:
        order = [explicit]
    else:
        order = ([context_brand] if context_brand else []) + (['KFC'] if context_brand != 'KFC' else [])
    res = None
    for b in order:
        res = _match_in_pool(header, brand_filter(customers, b), llm, note)
        if res.status != 'none':          # matched, or a genuine tie -> stop here
            if res.status == 'matched' and not explicit and b == context_brand and b != 'KFC':
                res.assumed_brand = b
                res.reason += f'; brand {b} assumed from the sender\'s earlier message'
            break
    return res


def _match_in_pool(header, pool, llm, note=''):
    scored = sorted(((len(_matched_tokens(header, _cust_tokens(c['contact_name']))), c) for c in pool),
                    key=lambda x: -x[0])
    best = scored[0][0] if scored else 0
    top = [c for s, c in scored if s == best] if best >= 1 else []

    if best >= 1 and len(top) == 1:
        return MatchResult('matched', top[0], [top[0]['contact_name']], note + 'location words', 'location')

    # AI only for no match or a tie, with strict acceptance rules
    ai_pool = top if len(top) > 1 else pool
    try:
        res = _ai_pick(header, ai_pool, llm)
    except Exception as e:
        res = {}
        note += f'AI unavailable ({e}); '
    try:
        idx = int(res.get('match', 0)) - 1
    except (TypeError, ValueError):
        idx = -1
    alts = res.get('alternatives') or []
    if (0 <= idx < len(ai_pool) and str(res.get('confidence', '')).lower() == 'high'
            and not alts and _evidence_ok(res.get('evidence', ''), header, ai_pool[idx]['contact_name'])):
        c = ai_pool[idx]
        return MatchResult('matched', c, [c['contact_name']], note + 'AI match (high confidence)', 'ai')

    if len(top) > 1:
        return MatchResult('ambiguous', None, [c['contact_name'] for c in top][:8],
                           note + 'several customers fit equally well', 'location')
    return MatchResult('none', None, [], note + 'no confident customer match', 'ai')


# ── item matching and HSN (single Groq call each, as in the existing QuoteBot) ─
def match_all_items(item_names, zoho_items, llm=None):
    """Return {index: zoho_item or None}. One Groq call for all items together."""
    llm = llm or (lambda p: groq_client.chat_json(p, max_tokens=500, temperature=0, expect=list))
    if not item_names:
        return {}
    if not zoho_items:
        return {i: None for i in range(len(item_names))}
    zoho_names = '\n'.join(f"{i+1}. {itm['name']}" for i, itm in enumerate(zoho_items))
    input_names = '\n'.join(f'{i+1}. {n}' for i, n in enumerate(item_names))
    prompt = f"""Match each input item to the closest item in the Zoho list.

Input items:
{input_names}

Zoho items:
{zoho_names}

Rules:
1. For each input item, find the best matching Zoho item number.
2. Consider abbreviations, plurals, alternate spellings (e.g. "LED Light" matches "LED Lights").
3. Only match if similarity is at least 70%. Otherwise use 0.
4. Each input item gets exactly one result.

Reply ONLY with a valid JSON array, one entry per input item, in order:
[{{"input": 1, "zoho": 5}}, {{"input": 2, "zoho": 0}}]
0 means no match found. Nothing else in your response."""
    result = {i: None for i in range(len(item_names))}
    try:
        for entry in llm(prompt):
            i, z = int(entry.get('input', 0)) - 1, int(entry.get('zoho', 0)) - 1
            if 0 <= i < len(item_names) and 0 <= z < len(zoho_items):
                result[i] = zoho_items[z]
    except Exception:
        return {i: None for i in range(len(item_names))}   # caller will create the items
    return result


def detect_hsn(item_names, llm=None):
    """Return {index: {hsn_or_sac, is_service}}. One Groq call for all new items together."""
    llm = llm or (lambda p: groq_client.chat_json(p, max_tokens=1000, temperature=0, expect=list))
    if not item_names:
        return {}
    numbered = '\n'.join(f'{i+1}. {n}' for i, n in enumerate(item_names))
    prompt = f"""You are an Indian GST expert. For each item below, provide the correct HSN code (for goods) or SAC code (for services).

Items:
{numbered}

Rules:
1. Goods -> 8-digit HSN code. Services -> 6-digit SAC code.
2. Use these exact codes where applicable:
   LED lights/lamps -> 94054090, goods
   Tape/adhesive/Fevicol/Fevibond -> 35069900, goods
   PVC pipe/fittings -> 39172300, goods
   Wire/cable -> 85444290, goods
   Switch/socket -> 85363010, goods
   MCB/breaker -> 85362000, goods
   Paint/primer/putty -> 32089090, goods
   Cement -> 25232900, goods
   Steel/iron -> 72142000, goods
   Screws/bolts/nuts -> 73181500, goods
   Plywood/wood -> 44121000, goods
   Fan -> 84145100, goods
   Pump -> 84137090, goods
   Tools (hammers, drills) -> 82051000, goods
   Safety equipment (gloves, helmets) -> 39262090, goods
   Cleaning materials/chemicals -> 34029090, goods
   Cleaning/housekeeping service -> 998531, service
   Maintenance/repair service -> 995469, service
   Labour charges -> 995511, service
   Security service -> 998523, service
3. If genuinely unsure -> HSN 99999999 for goods, SAC 999999 for services.

Reply ONLY with a valid JSON array. No markdown, no explanation, nothing else:
[{{"item": 1, "hsn_or_sac": "94054090", "is_service": false}}]"""
    result = {}
    try:
        for entry in llm(prompt):
            idx = int(entry.get('item', 0)) - 1
            if idx >= 0:
                result[idx] = {'hsn_or_sac': str(entry.get('hsn_or_sac', '99999999')),
                               'is_service': bool(entry.get('is_service', False))}
    except Exception:
        return {}
    return result
