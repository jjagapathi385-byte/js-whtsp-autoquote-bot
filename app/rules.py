"""Total handling, measure-vs-count, and safety checks. Code decides, not the AI."""
import re
from . import config

TOTAL_NAME_RE = re.compile(
    r'\b(grand\s*total|sub\s*total|total(\s*cost|\s*amount)?|tot\.?\s*amount|'
    r'cost\s*above(\s*listed)?|above\s*listed|amount)\b', re.I)

MEASURE_RE = re.compile(
    r'(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*(mtrs?|meters?|metres?|m|ft|feet|foot|sq\.?\s*ft|sqft|'
    r'kgs?|gms?|grams?|ltrs?|litres?|liters?|l|ml)\b', re.I)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_total_line(name):
    n = (name or '').strip()
    return bool(n) and len(n.split()) <= 5 and bool(TOTAL_NAME_RE.search(n))


def _clean_name(name, measure_text):
    n = name or ''
    if measure_text:
        n = re.sub(re.escape(measure_text), ' ', n, flags=re.I)
    n = re.sub(r'\s+', ' ', n).strip(' -:.,')
    return n


def _normalise_item(item):
    name = (item.get('name') or '').strip()
    qty = _num(item.get('quantity'), 1.0) or 1.0
    unit_price = _num(item.get('unit_price'))
    line_total = _num(item.get('line_total'))
    raw = item.get('raw_line') or ''
    unit_type = (item.get('unit_type') or 'none').lower()
    measure_text = (item.get('measure_text') or '').strip()

    m = MEASURE_RE.search(raw) or MEASURE_RE.search(name)
    if m:
        unit_type, measure_text = 'measure', m.group(0).strip()

    if unit_type == 'measure':
        amount = line_total or (unit_price * qty)
        line = dict(name=_clean_name(name, measure_text), description=measure_text,
                    quantity=1.0, rate=round(amount, 2))
        return _finish(line, item), amount

    if unit_price <= 0 and line_total > 0:
        unit_price = line_total / qty
    amount = unit_price * qty
    line = dict(name=name, description='', quantity=qty, rate=round(unit_price, 2))
    return _finish(line, item), amount


def _finish(line, item):
    line['is_service'] = bool(item.get('is_service', False))
    line['hsn_or_sac'] = item.get('hsn_or_sac', item.get('hsn_code', ''))
    return line


def apply_rules(parsed, markup=None):
    markup = markup or config.MARKUP
    stated_total = _num(parsed.get('stated_total'))
    notes = (parsed.get('notes') or '').strip()
    out = dict(status='ok', mode='normal', lines=[], issues=[], warnings=[],
               item_sum=0.0, stated_total=stated_total, notes=notes)

    items = [i for i in (parsed.get('items') or []) if (i.get('name') or '').strip()
             and not _is_total_line(i.get('name'))]
    if not items:
        work = (parsed.get('work_description') or '').strip()
        if stated_total > 0 and work:
            out['mode'] = 'single_line'
            out['lines'] = [dict(name=work, description='', quantity=1.0, rate=round(stated_total * markup, 2),
                                 is_service=True, hsn_or_sac='')]
            out['item_sum'] = stated_total
            n, w = notes.lower(), work.lower()
            out['notes'] = '' if (not n or n in w or w in n) else notes
            return out
        out['status'] = 'flag'; out['issues'].append('no items found')
        return out

    # Intelligent Pre-processing: If we encounter adjacent unpriced lines (headers like "Ac work") 
    # followed immediately by a priced line, merge them so they don't trigger unpriced item flags.
    merged_items = []
    pending_prefix = []
    for item in items:
        _, a = _normalise_item(item)
        if a <= 0 and len(items) > 1:
            pending_prefix.append(item.get('name'))
        else:
            if pending_prefix and item.get('name'):
                prefix_str = " ".join(pending_prefix)
                item['name'] = f"{prefix_str} - {item['name']}"
                pending_prefix = []
            merged_items.append(item)
    if pending_prefix and merged_items:
        # If any trailing unpriced items remain, attach them to the last item
        merged_items[-1]['name'] += " (" + ", ".join(pending_prefix) + ")"
    elif not merged_items:
        merged_items = items

    normalised = [_normalise_item(i) for i in merged_items]
    priced = [(l, a) for (l, a) in normalised if a > 0]
    unpriced = [i for i, (l, a) in zip(merged_items, normalised) if a <= 0]

    if not priced:
        if stated_total > 0:
            work = (parsed.get('work_description') or '').strip()
            if not work:
                work = 'Work as per request'
                out['warnings'].append('No work description found in the message; used a generic line name')
            listed = ', '.join(
                ((i.get('listed_as') or '').strip()
                 or ' '.join(x for x in [(i.get('name') or '').strip(), (i.get('measure_text') or '').strip()] if x))
                for i in merged_items)
            out['mode'] = 'single_line'
            out['lines'] = [dict(name=work, description=listed, quantity=1.0,
                                 rate=round(stated_total * markup, 2), is_service=False, hsn_or_sac='')]
            out['item_sum'] = stated_total
            n, w = notes.lower(), work.lower()
            out['notes'] = '' if (not n or n in w or w in n) else notes
            return out
        out['status'] = 'flag'; out['issues'].append('no prices and no total')
        return out

    # If minor unpriced items still exist after merging, handle them as single-line fallback or descriptions rather than hard flags
    if unpriced and stated_total > 0:
        # If a grand total is stated, treat unpriced text lines as part of the job description
        extra_desc = ", ".join(i['name'] for i in unpriced)
        out['warnings'].append(f'Unpriced descriptive headers absorbed: {extra_desc}')
    elif unpriced:
        out['status'] = 'flag'
        out['issues'].append('some items have no price: ' + ', '.join(i['name'] for i in unpriced))
        return out

    amounts = [a for (_, a) in priced]
    item_sum = sum(amounts)
    out['item_sum'] = round(item_sum, 2)

    if stated_total > 0:
        tol = config.MONEY_TOLERANCE
        if len(amounts) > 1:
            for idx, a in enumerate(amounts):
                if abs(a - stated_total) <= tol and abs((item_sum - a) - stated_total) <= tol:
                    out['status'] = 'flag'
                    out['issues'].append('a line equals the stated total and the others already add up '
                                         'to it (total counted as an item?)')
                    return out
        if config.TOTAL_RATIO_HIGH > 0 and item_sum >= stated_total * config.TOTAL_RATIO_HIGH:
            out['status'] = 'flag'
            out['issues'].append(f'item sum {item_sum:g} is at least {config.TOTAL_RATIO_HIGH:g}x the '
                                 f'stated total {stated_total:g}')
            return out
        if config.TOTAL_RATIO_LOW > 0 and item_sum <= stated_total * config.TOTAL_RATIO_LOW:
            out['status'] = 'flag'
            out['issues'].append(f'item sum {item_sum:g} is at most {config.TOTAL_RATIO_LOW:g}x the '
                                 f'stated total {stated_total:g}')
            return out
        if abs(item_sum - stated_total) > tol:
            out['warnings'].append(f'Total in message: {stated_total:g}, items add up to: {item_sum:g} '
                                   '(used item prices)')

    lines = []
    for line, _ in priced:
        line = dict(line)
        line['rate'] = round(line['rate'] * markup, 2)
        lines.append(line)
    out['lines'] = lines
    return out