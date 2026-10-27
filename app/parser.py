"""Groq message parser. Adapted from the existing QuoteBot parse prompt, with changes:
- HSN is NOT asked here (separate single call for new items, see matching.detect_hsn)
- returns stated_total separately (the total is never an item)
- per item: line_total, unit_type (measure/count/none), measure_text, raw_line
- measure units (meters, mtrs, ft, kg, litre...) are NOT divided into qty x unit price
- handles multi-line technician text (combining description lines with subsequent price lines)
"""
import re
from difflib import SequenceMatcher

from . import groq_client

PROMPT = """Parse this WhatsApp quotation message from an Indian hardware / electrical / maintenance business.
Return ONLY one JSON object, nothing else.

Message:
---
__MESSAGE__
---

JSON shape:
{"customer_name":"","email":"","notes":"","stated_total":0,"work_description":"","items":[{"name":"","quantity":1,"unit_price":0,"line_total":0,"is_service":false}]}

Rules:
1. customer_name: the store/customer from the header (keep the store code and place words).
2. email: every email address found, comma separated, else "".
3. items: one per line of work/material, INCLUDING lines that have no price (unit_price 0). 
   - CRITICAL (Multi-line & Grouping): If a technician writes a service or part name on one line (e.g., "Ac work", "split AC repair", "Copper pipe and foams") and the quantity/price on the next line (e.g., "R22 gas topup purpose 6kgs- 8000/-" or "2500 rs"), COMBINE them into a single coherent item name and extract the correct total price into line_total.
   - PHRASING (Each one / Unit multiplier): If an item says "8nos" and "Each one 1500", set quantity = 8, unit_price = 1500, and line_total = 12000.
   - Name = corrected proper English (fix typos); keep sizes inside the name ("50mm cable", "2.5 sq mm wire"); never put a quantity, a length, or location/city names (like Vizag, Hyderabad) in the item name.
   - line_total = the amount printed on that line. A price stuck to the name must be split.
   - Counted pieces (no, nos, pcs, sets, bags, rolls, boxes, or "2*650"): quantity = pieces, unit_price = line_total / quantity.
   - Lengths, weights, volumes (meters, mtrs, ft, kg, litre, L, ml): quantity = 1 and unit_price = line_total (never divide).
   - A line with no price: if it's a standalone title, combine it with the priced line below it. Never invent a standalone 0-price item if a price follows immediately.
4. TOTAL lines ("Total", "Tot amount", "Amount", "Grand total", "Cost above listed") are NOT items: put that number in stated_total (0 if none).
5. work_description: 2-6 words, Title Case, the kind of work (e.g. "Electrical Work"). Never include city or location names (like Vizag, hyd, vijayawada, tadepalli) here.
6. notes: only useful work context. Leave "" for totals, "please share/send quotation", urgent/ASAP, etc.
"""

# ── Everything below is worked out in code ──
_QTY = re.compile(r'(?<![A-Za-z0-9])(\d+(?:\.\d+)?\s*(?:mtrs?|meters?|metres?|m|ft|feet|foot|sq\.?\s*ft|sqft|'
                  r'kgs?|gms?|grams?|ltrs?|litres?|liters?|l|ml|'
                  r'nos?|pcs?|pieces?|sets?|bags?|rolls?|boxes?))\b', re.I)


def _segments(message):
    segs = []
    for line in (message or '').splitlines():
        for part in re.split(r'[;,]\s*', line):
            if part.strip():
                segs.append(part.strip())
    return segs


def _words(text):
    return [w for w in re.findall(r'[a-z0-9]+', (text or '').lower()) if len(w) >= 3]


def _best_segment(name, segments, used):
    nw = _words(name)
    if not nw:
        return ''
    best, best_score = '', 0.0
    for idx, seg in enumerate(segments):
        sw = _words(seg)
        if not sw:
            continue
        hit = sum(1 for w in nw if any(w == x or w in x or x in w or SequenceMatcher(None, w, x).ratio() >= 0.8 for x in sw))
        score = hit / len(nw) - (0.05 if idx in used else 0)
        if score > best_score:
            best, best_score, best_idx = seg, score, idx
    if best_score >= 0.4:
        used.add(best_idx)
        return best
    return ''


def _fallback_items(message):
    from .intake import BRAND_RE, CODE_RE, STORE_WORD_RE
    skip = re.compile(r'(total|tot\.?\s*amount|\bamount\b|\bcost\b|quot|quat|question|kindly|please|urgent|asap|advance|@|mail|^work\b|^store\b)', re.I)
    out = []
    for seg in _segments(message):
        if skip.search(seg) or BRAND_RE.search(seg) or CODE_RE.search(seg) or STORE_WORD_RE.search(seg):
            continue
        if re.search(r'[A-Za-z]{3}', seg):
            out.append(seg.strip())
    return out if 2 <= len(out) <= 12 else []


def _enrich(items, message):
    from .rules import MEASURE_RE
    segs, used = _segments(message), set()
    for it in items:
        cleaned = re.sub(r'\s+', ' ', _QTY.sub(' ', MEASURE_RE.sub(' ', it['name']))).strip(' -:.,')
        it['name'] = cleaned or it['name']
        raw = _best_segment(it['name'], segs, used)
        it['raw_line'] = raw
        m = MEASURE_RE.search(raw)
        it['measure_text'] = m.group(0).strip() if m else ''
        it['unit_type'] = 'measure' if m else ('count' if it['quantity'] != 1 else 'none')
        q = _QTY.search(raw)
        it['listed_as'] = f"{it['name']} {q.group(1).replace(' ', '')}".strip() if q else it['name']
    return items


def _num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


def parse_message(message, **groq_kwargs):
    msg = (message or '').strip()
    if len(msg) > 3000:
        msg = msg[:3000]
    lines = msg.count('\n') + 1
    data = groq_client.chat_json(PROMPT.replace('__MESSAGE__', msg), max_tokens=min(900, 200 + 60 * lines),
                                 temperature=0.1, expect=dict, **groq_kwargs)
    items = []
    for it in data.get('items') or []:
        if not isinstance(it, dict):
            continue
        items.append(dict(
            name=str(it.get('name', '')).strip(),
            quantity=_num(it.get('quantity')) or 1.0,
            unit_price=_num(it.get('unit_price')),
            line_total=_num(it.get('line_total')),
            is_service=bool(it.get('is_service', False)),
        ))
    total = _num(data.get('stated_total'))
    if not items and total > 0:
        items = [dict(name=n.title(), quantity=1.0, unit_price=0.0, line_total=0.0, is_service=False)
                 for n in _fallback_items(msg)]
    return dict(
        customer_name=str(data.get('customer_name', '')).strip(),
        email=str(data.get('email', '') or '').strip(),
        notes=str(data.get('notes', '') or '').strip(),
        stated_total=total,
        work_description=str(data.get('work_description', '') or '').strip(),
        items=_enrich(items, msg),
    )