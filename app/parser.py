"""Groq message parser. Adapted from the existing QuoteBot parse prompt, with changes:
- HSN is NOT asked here (separate single call for new items, see matching.detect_hsn)
- returns stated_total separately (the total is never an item)
- per item: line_total, unit_type (measure/count/none), measure_text, raw_line
- measure units (meters, mtrs, ft, kg, litre...) are NOT divided into qty x unit price
Code in rules.py enforces the measure/total rules even if the model slips."""
from . import groq_client

PROMPT = """You are a quotation parser for an Indian hardware/electrical supplies business.

Parse this WhatsApp quotation message and return ONLY a valid JSON object. No markdown, no explanation.

Message:
---
__MESSAGE__
---

Rules:
1. Extract customer/store name from the first line or header (include store code and location words if present).
2. For each item: name, quantity (default 1), unit_price, line_total, unit_type, measure_text, raw_line.
   - line_total = the amount printed for that line in the message (always fill it).
   - raw_line = the original text of that item line, copied exactly.
   - unit_type is one of:
     "count"   -> counted pieces: no, nos, pcs, sets, bags, rolls, boxes, or explicit multiplication like 2*650.
                  Set quantity and unit_price (if a total is given for several pieces, unit_price = total / quantity,
                  e.g. "5nos 1000" -> quantity 5, unit_price 200; "2*650- 1300" -> quantity 2, unit_price 650).
     "measure" -> a length/weight/volume amount: meters, mtrs, m, feet, ft, sq ft, kg, grams, litre, ltr, L, ml.
                  quantity MUST be 1, unit_price = line_total, measure_text = the measure exactly as typed
                  (e.g. "10 meters", "10 mtrs", "15 L"). Do NOT divide the price.
                  "Sofa cloth 10 meters 2100" -> name "Sofa cloth", quantity 1, unit_price 2100, measure_text "10 meters".
     "none"    -> no quantity or measure given.
   - Sizes are part of the item NAME, never a quantity: "50mm cable", "2.5 sq mm wire", "12 inch pipe".
   - Price may be STUCK to the item name with no space — always split it:
     "scolding7500" -> name="Scolding", unit_price=7500; "labour charges11000" -> name="Labour Charges", unit_price=11000
   - Any separator like period(.), dash(-), slash(/), colon between item and price is a separator, not a decimal.
   - If an item has NO price in the message, set unit_price and line_total to 0. Never invent prices.
3. IMPORTANT - Correct item names: fix typos, expand abbreviations, use proper industry names.
   Examples: "brazing rad pipe" -> "Brazing Rod Pipe", "magnitor" -> "Magnetron", "fevi bond" -> "Fevibond Adhesive",
   "R 32 gas" -> "R32 Refrigerant Gas", "led lt" -> "LED Light", "ac servic" -> "AC Service",
   "Bolyer moter repair" -> "Boiler Motor Repair", "heate slevs" -> "Heater Sleeves".
   Always use proper English, correct spelling, full words. Do not put the measure in the name.
4. TOTAL lines ("Total", "Total Cost Above listed", "Amount", "Tot amount", "Grand total") are NOT items.
   Put that number in stated_total (0 if none is stated). Never add a total as an item.
5. listed_as (per item): the item as it should read in a list — corrected name followed by its quantity or
   measure exactly as typed, e.g. "16sq mm Wire 10mtr", "Pin Type Lugs 8nos", "Nuts and Bolts 16nos". No price.
6. work_description: ONLY when NO item has its own price and only a total is given — a short title (2-4 words,
   Title Case) for the kind of work, e.g. "Electrical Work", "AC Repair Work", "Walk-in Freezer Work".
   Otherwise return an empty string. Do not repeat it in notes.

Return this exact JSON:
{
  "customer_name": "name here",
  "email": "email@example.com or empty string if none",
  "notes": "work description or context only — see rules below",
  "stated_total": 0,
  "work_description": "",
  "items": [
    {"name": "item name", "quantity": 1, "unit_price": 0.0, "line_total": 0.0,
     "unit_type": "count|measure|none", "measure_text": "", "raw_line": "", "listed_as": "", "is_service": false}
  ]
}

For email: extract an email address if found anywhere in the message. Empty string if none.
For notes — strict rules:
INCLUDE in notes: work description or context (e.g. "AC repair work", "walk in freezer", "glass door side holders assemble work").
EXCLUDE from notes (set empty string if only these exist):
- Any total / grand total lines (e.g. "Total 3200", "Total Cost 6500", "Grand Total")
- "Please share the quotation" or similar share/send requests, "Kindly share quotation"
- "Urgent", "ASAP" or similar filler words alone
- "Send quotation" type phrases
- Store name or customer name (already captured separately)
If nothing valid for notes -> return empty string "".
ONLY return the JSON. Nothing else."""


def _num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


def parse_message(message, **groq_kwargs):
    msg = (message or '').strip()
    if len(msg) > 3000:  # same guard as the existing QuoteBot
        msg = msg[:3000]
    data = groq_client.chat_json(PROMPT.replace('__MESSAGE__', msg), max_tokens=2000,
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
            unit_type=str(it.get('unit_type') or 'none').lower(),
            measure_text=str(it.get('measure_text') or '').strip(),
            raw_line=str(it.get('raw_line') or '').strip(),
            listed_as=str(it.get('listed_as') or '').strip(),
            is_service=bool(it.get('is_service', False)),
        ))
    return dict(
        customer_name=str(data.get('customer_name', '')).strip(),
        email=str(data.get('email', '') or '').strip(),
        notes=str(data.get('notes', '') or '').strip(),
        stated_total=_num(data.get('stated_total')),
        work_description=str(data.get('work_description', '') or '').strip(),
        items=items,
    )
