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
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from . import config, groq_client
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
    assumed_brand: str = ''          # set when the brand was not in the message (from the sender's contact or earlier message)
    assumed_source: str = ''         # 'sender' (contact name / assigned) or 'context' (earlier message)
    ai_failed: bool = False          # the AI could not be reached while deciding (caller retries later)


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
    if re.search(r'PHD|PIZZA\s*HUT|PIZZAHUT|\bPH\b', up):      # \bPH\b: not the "ph" inside "Joseph "
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


def brand_from_name(name):
    """Brand named in a person's contact / profile name ("KFC Supervisor Ravi", "Pizza Hut - Suresh"), else None."""
    t = name or ''
    if re.search(r'\bkfc\b', t, re.I):
        return 'KFC'
    if re.search(r'\b(phd|pizza\s*hut|pizzahut|ph)\b', t, re.I):
        return 'PHD'
    if re.search(r'\bcosta\b', t, re.I):
        return 'COSTA'
    if re.search(r'\bvaango\b', t, re.I):
        return 'VAANGO'
    return None


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
def _shortlist(header, pool, n=25):
    """Only the most similar customers go to the AI (a prompt with hundreds of names blows the token limit)."""
    if len(pool) <= n:
        return pool
    compact = re.sub(r'[^A-Z0-9]', '', header.upper())
    words = _words(header)

    def score(c):
        total = 0.0
        for t in _cust_tokens(c['contact_name']):
            best = 1.0 if t in compact else max([SequenceMatcher(None, w, t).ratio() for w in words] or [0.0])
            total += best if best >= 0.6 else 0.0
        return total
    return sorted(pool, key=score, reverse=True)[:n]


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


def match_customer(message, ai_name, customers, llm=None, context_brand=None, sender_brand=None):
    """Return a MatchResult. `llm(prompt)` returns the parsed JSON (defaults to Groq).
    `context_brand`: brand from the same sender's earlier message (used only when this message has no
    brand word and no store code)."""
    llm = llm or (lambda p: groq_client.chat_json(p, max_tokens=160, temperature=0, expect=dict))
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
            # Two Zoho customers share this code (e.g. an old and a new record): the place words decide.
            top = _top(_rank(header_text(message, ai_name), hits))
            if len(top) == 1:
                return MatchResult('matched', top[0], [top[0]['contact_name']],
                                   f'store code {valid[0]} (shared by {len(hits)} customers; chosen by the place words)', 'code')
            return MatchResult('ambiguous', None, [h['contact_name'] for h in (top or hits)],
                               f'store code {valid[0]} matches several customers', 'code')

    # 2-4. Brand, then location. Brand priority:
    #   explicit brand word / store-code prefix  >  sender's recent brand (context)  >  KFC default
    header = header_text(message, ai_name)
    explicit = detect_brand(header, (valid or codes or [None])[0])
    if explicit:
        order = [(explicit, '')]
    elif sender_brand:
        order = [(sender_brand, 'sender')]       # known from the sender's contact name: strict, never falls back to another brand
    else:
        order = ([(context_brand, 'context')] if context_brand else []) + ([('KFC', '')] if context_brand != 'KFC' else [])
    res = None
    for b, src in order:
        res = _match_in_pool(header, brand_filter(customers, b), llm, note)
        if res.status != 'none':          # matched, or a genuine tie -> stop here
            if res.status == 'matched' and src and b != 'KFC':
                res.assumed_brand, res.assumed_source = b, src
                res.reason += f"; brand {b} assumed from the sender's {'contact name' if src == 'sender' else 'earlier message'}"
            break
    return res


def _rank(header, pool):
    """[(score, n_matched, customer)] best first. A word that only a few customers have (ELURU) counts more than a
    word many customers share (VIJAYAWADA), so the candidate lists are short and the right store wins more often."""
    toks = [_cust_tokens(c['contact_name']) for c in pool]
    df = Counter(t for tl in toks for t in set(tl))
    n = max(1, len(pool))
    out = []
    for c, tl in zip(pool, toks):
        hit = _matched_tokens(header, tl)
        out.append((sum(math.log(1 + n / df[t]) for t in hit), len(hit), c))
    return sorted(out, key=lambda x: -x[0])


def _top(ranked):
    best = ranked[0][0] if ranked else 0
    return [c for sc, _, c in ranked if best > 0 and sc >= best - 1e-9]


def _match_in_pool(header, pool, llm, note=''):
    top = _top(_rank(header, pool))

    if len(top) == 1:
        return MatchResult('matched', top[0], [top[0]['contact_name']], note + 'location words', 'location')

    # AI only for no match or a tie, with strict acceptance rules
    ai_pool = _shortlist(header, top if len(top) > 1 else pool)       # never more than ~25 names go to the AI
    ai_failed = False
    try:
        res = _ai_pick(header, ai_pool, llm)
    except Exception as e:
        res, ai_failed = {}, True
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
                           note + 'several customers fit equally well', 'location', ai_failed=ai_failed)
    return MatchResult('none', None, [], note + 'no confident customer match', 'ai', ai_failed=ai_failed)


# ── item matching and HSN (single Groq call each, as in the existing QuoteBot) ─
def _norm(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _item_candidates(name, zoho_items, n=6, floor=0.55):
    """The few Zoho items that look like `name`, best first. Cheap string similarity, no AI."""
    q = (name or '').lower()
    qwords = set(re.findall(r'[a-z0-9]+', q))
    scored = []
    for i, it in enumerate(zoho_items):
        z = (it.get('name') or '').lower()
        zwords = set(re.findall(r'[a-z0-9]+', z))
        overlap = len(qwords & zwords) / max(1, len(qwords | zwords))
        score = max(SequenceMatcher(None, q, z).ratio(), overlap)
        if score >= floor:
            scored.append((score, i))
    return [i for _, i in sorted(scored, reverse=True)[:n]]


def match_all_items(item_names, zoho_items, llm=None):
    """Return {index: zoho_item or None}.

    1. identical names (ignoring case/spaces/punctuation) match with no AI call;
    2. items that look like nothing in Zoho are new, with no AI call;
    3. only the rest go to ONE small AI call that sees just a short list of look-alike Zoho items
       (sending the whole Zoho item list would blow the Groq token limit).
    If the AI call fails the error is RAISED, so the caller retries; it never guesses 'everything is new'
    (that would create duplicate items in Zoho)."""
    if not item_names:
        return {}
    result = {i: None for i in range(len(item_names))}
    if not zoho_items:
        return result
    by_norm = {}
    for it in zoho_items:
        by_norm.setdefault(_norm(it.get('name')), it)
    undecided = []                                           # (input index, candidate zoho indexes)
    for i, name in enumerate(item_names):
        hit = by_norm.get(_norm(name))
        if hit:
            result[i] = hit
            continue
        cands = _item_candidates(name, zoho_items)
        if cands:
            undecided.append((i, cands))
    if not undecided:
        return result

    union = []
    for _, cands in undecided:
        for c in cands:
            if c not in union:
                union.append(c)
    union = union[:40]
    llm = llm or (lambda p: groq_client.chat_json(p, max_tokens=min(600, 60 + 40 * len(undecided)),
                                                  temperature=0, expect=list))
    zoho_names = '\n'.join(f"{k+1}. {zoho_items[z]['name']}" for k, z in enumerate(union))
    input_names = '\n'.join(f'{k+1}. {item_names[i]}' for k, (i, _) in enumerate(undecided))
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
    for entry in llm(prompt):
        k, z = int(entry.get('input', 0)) - 1, int(entry.get('zoho', 0)) - 1
        if 0 <= k < len(undecided) and 0 <= z < len(union):
            result[undecided[k][0]] = zoho_items[union[z]]
    return result


def _override_for(name):
    n = (name or '').lower()
    for kw, val in config.HSN_OVERRIDES.items():
        if kw and kw in n:
            if isinstance(val, dict):
                return {'hsn_or_sac': str(val.get('code', '')), 'is_service': bool(val.get('service', False))}
            return {'hsn_or_sac': str(val), 'is_service': False}
    return None


def detect_hsn(item_names, llm=None, contexts=None):
    """Return {index: {hsn_or_sac, is_service}}.
    1. codes you set in HSN_OVERRIDES are used as they are (no AI);
    2. everything else goes to ONE Groq call, which also sees what a supply line includes (`contexts`).
    A failure is raised (the caller retries), never a wrong code."""
    if not item_names:
        return {}
    result, ask = {}, []
    for i, name in enumerate(item_names):
        ov = _override_for(name)
        if ov and ov['hsn_or_sac']:
            result[i] = ov
        else:
            ask.append(i)
    if not ask:
        return result
    llm = llm or (lambda p: groq_client.chat_json(p, max_tokens=min(900, 80 + 70 * len(ask)),
                                                  temperature=0, expect=list))
    contexts = contexts or [''] * len(item_names)
    numbered = '\n'.join(
        f"{k+1}. {item_names[i]}" + (f" (includes: {contexts[i][:200]})" if contexts[i] else '') for k, i in enumerate(ask))
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
3. Always give the most likely REAL code for what the item is. For a supply of mixed goods (the "includes" list), use
   the HSN of the main item. Use 99999999 (goods) / 999999 (services) ONLY if the item cannot be identified at all.

Reply ONLY with a valid JSON array. No markdown, no explanation, nothing else:
[{{"item": 1, "hsn_or_sac": "94054090", "is_service": false}}]"""
    for entry in llm(prompt):                        # a failure is raised: the caller retries, never a wrong HSN
        k = int(entry.get('item', 0)) - 1
        if 0 <= k < len(ask):
            result[ask[k]] = {'hsn_or_sac': str(entry.get('hsn_or_sac', '99999999')),
                              'is_service': bool(entry.get('is_service', False))}
    return result
