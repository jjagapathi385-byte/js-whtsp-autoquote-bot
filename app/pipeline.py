"""One quote, start to finish (brief section 8). Called once per message when its 3-minute timer expires.

Any doubt -> Telegram flag and NO quote. In dry-run mode nothing is written to Zoho and nothing is
posted to WhatsApp: Telegram just previews what would be created.

When the AI service is only temporarily unavailable (busy / token limit) the message is put back with a
short delay and tried again (up to MAX_AI_RETRIES) instead of being flagged or guessed."""
import logging
import re
import time
from dataclasses import dataclass

from . import config, matching, notify, parser, rules
from .groq_client import GroqError
from .zoho import Zoho

EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')


@dataclass
class Deps:
    zoho: object
    store: object
    notify: callable            # notify(text) -> None
    bridge: object = None       # has send_document(filename, data, caption)
    parse: callable = None      # parse(message) -> dict   (defaults to Groq parser)
    llm: callable = None        # llm(prompt) -> parsed JSON for matching/HSN calls; None = Groq
    now: callable = None        # clock (set by the engine)


def _emails(parsed, text):
    """Every distinct email address given with the request (parser field first, then the raw message)."""
    found, seen = [], set()
    for src in (parsed.get('email') or '', text or ''):
        for e in EMAIL_RE.findall(src):
            if e.lower() not in seen:
                seen.add(e.lower())
                found.append(e)
    return found


def process_message(msg, d):
    text, sender = msg['text'], msg.get('sender', '')
    who = f"{msg['contact_name']} ({sender})" if msg.get('contact_name') and msg['contact_name'] != sender else sender   # for Telegram
    dry = d.store.flag('dry_run', config.DRY_RUN_DEFAULT)
    post = d.store.flag('post_to_group', config.POST_TO_GROUP_DEFAULT)
    clock = d.now or time.time

    def flag(issues, store='unknown'):
        d.notify(notify.quote_flagged(who, store, issues, text, dry_run=dry))
        return {'status': 'flagged', 'issues': issues}

    def retry(reason, store='unknown'):
        """The AI is busy or unreachable: try this message again shortly instead of guessing."""
        n = msg.get('retries', 0)
        if n >= config.MAX_AI_RETRIES:
            return flag([f'AI service unavailable after {n} retries: {reason}'], store)
        d.store.add_pending(dict(msg, retries=n + 1, due=clock() + config.AI_RETRY_DELAY_SECONDS))
        logging.warning('AI unavailable (%s); retry %d of %d in %ds', reason, n + 1, config.MAX_AI_RETRIES,
                        config.AI_RETRY_DELAY_SECONDS)
        return {'status': 'retry', 'retries': n + 1, 'reason': reason}

    # Intake doubted this one. An unclear STORE alone is not a reason to skip it (the default customer is
    # used below); any other doubt (no price, payment words...) is still flagged.
    if msg.get('intake_action') == 'flag' and not msg.get('default_ok'):
        return flag(msg.get('intake_reasons') or ['unclear message'])

    # 1. parse
    try:
        parsed = (d.parse or parser.parse_message)(text)
    except GroqError as e:
        return retry(str(e))
    except Exception as e:
        return flag([f'Could not parse the message: {e}'])

    # -------------------------------------------------------------------------
    # 2. Customer, Brand, Markup, and Robust Fallback Logic
    # -------------------------------------------------------------------------
    ctx = d.store.brand_context(sender, msg.get('ts', 0), config.BRAND_CONTEXT_SECONDS) if sender else None
    sender_brand = (d.store.sender_brand(msg.get('sender_key', '')) or matching.brand_from_name(msg.get('contact_name'))
                    or matching.brand_from_name(sender))
    customers = d.zoho.customers()
    default_name = config.DEFAULT_CUSTOMER_NAME.strip().lower()
    
    is_paradise_msg = (msg.get('brand_override') == 'PARADISE')

    # A. Dynamic Store Pool & Markup Target Selection
    if is_paradise_msg:
        pool = [c for c in customers if 'PARADISE' in c['contact_name'].upper() and c['contact_name'].strip().lower() != default_name]
        target_markup = msg.get('markup_override') or config.MARKUP_PARADISE
    elif msg.get('brand_override') == 'ALL':
        pool = [c for c in customers if c['contact_name'].strip().lower() != default_name]
        target_markup = msg.get('markup_override') or config.MARKUP
    else:
        pool = [c for c in customers if 'PARADISE' not in c['contact_name'].upper() and c['contact_name'].strip().lower() != default_name]
        target_markup = msg.get('markup_override') or config.MARKUP

    name_in_message = (parsed.get('customer_name') or matching.header_text(text)).strip()
    text_has_explicit_brand = bool(matching.brand_from_name(text) or matching.brand_from_name(name_in_message))

    # B. Strict Unassigned User Guard vs. Assigned User Matching
    if not sender_brand and not ctx and not is_paradise_msg and not text_has_explicit_brand:
        # Unassigned user without explicit brand -> Force immediate default customer fallback
        class ForcedMatch:
            status = 'forced_default'
            reason = f"Unassigned user provided store '{name_in_message}' without explicit brand prefix"
            ai_failed = False
        mr = ForcedMatch()
    else:
        # Assigned user or explicit brand message -> Run normal Zoho customer pool matching
        mr = matching.match_customer(text, parsed.get('customer_name', ''), pool, d.llm, context_brand=ctx,
                                     sender_brand=sender_brand)

    used_default, unclear = False, ''

    # C. Final Customer Resolution (Catches unmatched stores for BOTH assigned & unassigned users)
    if getattr(mr, 'status', 'matched') == 'matched':
        customer = mr.customer
    else:
        # If matching failed because the store doesn't exist in Zoho (or was forced), handle fallback gracefully
        if getattr(mr, 'ai_failed', False):
            return retry(mr.reason, name_in_message or 'unknown')
        unclear = getattr(mr, 'reason', 'store not found in customer list')
        
        # Fall back to default customer ("STORE") with the correct group markup (10% or 5%)
        customer = next((c for c in customers if c['contact_name'].strip().lower() == default_name), None)
        if not customer:
            return flag([unclear, f"the default customer '{config.DEFAULT_CUSTOMER_NAME}' does not exist in Zoho"], f"{name_in_message or 'unknown'} — low confidence")
        used_default = True
    cname = customer['contact_name']

    # 3. totals / measures / safety checks (code, not AI)
    res = rules.apply_rules(parsed, markup=target_markup)
    if res['status'] != 'ok':
        return flag(res['issues'], cname)
    emails = _emails(parsed, text)
    # Notes: work notes, then the email(s) for future reference, then (default customer only) the notice,
    # and the store/customer name exactly as given in the message as the very LAST line.
    parts = [res['notes']]
    if emails:
        parts.append('Email: ' + ', '.join(emails))
    if used_default:
        parts += [config.DEFAULT_CUSTOMER_NOTE, f'Store/customer name given in the message: {name_in_message}']
        res['warnings'].insert(0, f'DEFAULT CUSTOMER USED — store unclear: {unclear}. Name in message: {name_in_message}')
    notes = '\n'.join(x for x in parts if x)
    if mr.assumed_brand:
        src = "the sender's contact name" if mr.assumed_source == 'sender' else "this sender's earlier message"
        res['warnings'].append(f'Brand {mr.assumed_brand} was assumed from {src} — please verify')
    lines = res['lines']
    subtotal = round(sum(l['quantity'] * l['rate'] for l in lines), 2)

    # 4. duplicate guard: same customer AND same amount in the last 30 minutes
    try:
        dup = d.zoho.find_recent_duplicate(customer['contact_id'], subtotal)
    except Exception as e:
        logging.warning('duplicate check failed: %s', e)
        dup = None
    if dup:
        return flag([f"possible duplicate of {dup.get('estimate_number', 'a recent estimate')} "
                     f"(same customer and amount within {config.DUPLICATE_WINDOW_MINUTES} min)"], cname)

    # 5. items: one small Groq call to match, one to detect HSN/SAC for the new ones
    try:
        zitems = d.zoho.items()
    except Exception as e:
        return flag([f'Zoho item lookup failed: {e}'], cname)
    try:
        matches = matching.match_all_items([l['name'] for l in lines], zitems, d.llm)
    except GroqError as e:
        return retry(f'item matching: {e}', cname)
    except Exception as e:
        return flag([f'Item matching failed: {e}'], cname)
    new_idx = [i for i in range(len(lines)) if not matches.get(i)]

    if dry:
        warnings = list(res['warnings'])
        if new_idx:
            warnings.append('New Zoho items would be created: ' + ', '.join(lines[i]['name'] for i in new_idx))
        d.notify(notify.dry_run_preview(who, cname, res['mode'], lines, res['item_sum'],
                                        res['stated_total'], ', '.join(emails), warnings))
        return {'status': 'dry_run', 'customer': cname, 'lines': lines}

    # ── live from here ──
    try:
        hsn = (matching.detect_hsn([lines[i]['name'] for i in new_idx], d.llm,
                                   [lines[i].get('description', '') for i in new_idx]) if new_idx else {})
    except GroqError as e:
        return retry(f'HSN detection: {e}', cname)
    except Exception as e:
        return flag([f'HSN detection failed: {e}'], cname)
    try:
        tax_id = d.zoho.gst18_tax_id()   # one GST 18% tax; Zoho splits CGST+SGST / IGST by place of supply
        item_ids = []
        for i, line in enumerate(lines):
            if matches.get(i):
                item_ids.append(matches[i]['item_id'])
            else:
                h = hsn.get(new_idx.index(i), {})
                code = h.get('hsn_or_sac', '99999999')
                if code in config.GENERIC_HSN:      # a placeholder code must never go unnoticed
                    res['warnings'].append(f"HSN/SAC for '{line['name']}' is the generic placeholder {code}: "
                                           'please set the right code in Zoho (or add it to HSN_OVERRIDES)')
                created = d.zoho.create_item(line['name'], code, line['rate'],
                                             tax_id, h.get('is_service', line.get('is_service', False)))
                item_ids.append(created.get('item_id', ''))
        line_items = Zoho.build_line_items(lines, item_ids, tax_id)
        result = d.zoho.create_estimate(customer['contact_id'], cname, line_items, notes)
    except Exception as e:
        return flag([f'Zoho error: {e}'], cname)
    est = result.get('estimate')
    if not est:
        return flag([f'Zoho did not create the estimate: {str(result)[:200]}'], cname)

    # 6. mark processed the moment Zoho confirms (prevents duplicates)
    est_id, number = est.get('estimate_id', ''), est.get('estimate_number', '')
    d.store.mark_processed(msg['id'], {'estimate_id': est_id, 'number': number, 'customer': cname})

    # 7. email (to every address given) and WhatsApp are independent: one failing never blocks the other
    email_status = 'not applicable'
    if emails and est_id:
        try:
            er = d.zoho.send_estimate_email(est_id, emails)
            email_status = f'sent to {", ".join(emails)}' if er.get('code', -1) == 0 else f'FAILED ({er.get("message", er)})'
        except Exception as e:
            email_status = f'FAILED ({e})'
    wa_status = 'not posted (posting is off)'
    pdf = None
    try:
        pdf = d.zoho.estimate_pdf(est_id)
    except Exception as e:
        wa_status = f'PDF fetch failed ({e})'
    if post and pdf and d.bridge:
        try:
            tag = msg.get('sender_jid') or None
            user = tag.split(':')[0].split('@')[0] if tag else ''
            shown = (name_in_message[:80] or cname) if used_default else cname
            caption = (config.CAPTION_TEMPLATE.replace('{customer}', shown).replace('{number}', number)
                       .replace('@{user}', f'@{user}' if user else '').replace('{user}', user))
            caption = re.sub(r'\s+', ' ', caption).strip()
            
            # Check if this is a Paradise message
            is_paradise = (msg.get('brand_override') == 'PARADISE')
            
            # Dispatch to the correct bridge URL
            d.bridge.send_document(f'{number}.pdf', pdf, caption, mentions=[tag] if tag else None, is_paradise=is_paradise)
                
            wa_status = 'posted to group' + (' (sender tagged)' if tag else ' (sender could not be tagged)')
        except Exception as e:
            wa_status = 'FAILED to post — please forward manually'

    # Extract group name for Telegram notification
    # Extract group name from message payload safely covering all possible key names
    group_name = (
        msg.get('group_name') or 
        msg.get('chat_title') or 
        msg.get('chat_name') or 
        msg.get('group') or 
        msg.get('chat') or 
        ''
    )
    txt = notify.quote_created(number, cname, who, email_status, res['warnings'], group_name=group_name)
    d.notify(txt + f'\n💬 WhatsApp: {wa_status}')
    return {'status': 'created', 'number': number, 'customer': cname, 'email': email_status, 'whatsapp': wa_status}