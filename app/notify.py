"""Telegram notifications. Plain text on purpose (no Markdown) so customer messages with
special characters can never break a notification. Never raises: a failed notification must
not block quote creation."""
import requests
from . import config


def send(text, post=None):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        r = (post or requests.post)(
            f'https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': config.TELEGRAM_CHAT_ID, 'text': text[:4000]}, timeout=10)
        return getattr(r, 'status_code', 200) == 200
    except Exception:
        return False


def quote_created(quote_number, customer, sender, email_status='not applicable', warnings=None, dry_run=False, group_name=""):
    header = '🧪 DRY RUN — nothing created\n' if dry_run else '📋 New Quote Created\n'
    
    # Always display the group line so we can see what was received
    display_group = group_name if group_name else "⚠️ Not Provided in Payload"
    
    lines = [
        header,
        f'💬 Group: {display_group}',
        f'🔢 Quote No: {quote_number}',
        f'🏪 Customer: {customer}',
        f'👤 Sender: {sender}',
        f'📧 Email: {email_status}'
    ]
    for w in warnings or []:
        lines.append(f'{"⚠️" if w.startswith("DEFAULT CUSTOMER") else "ℹ️"} {w}')
    return '\n'.join(lines)


def quote_flagged(sender, store, issues, original, dry_run=False):
    head = '🧪 DRY RUN\n' if dry_run else ''
    return (f'{head}⚠️ Quote Needs Manual Review\n👤 Sender: {sender}\n🏪 Store: {store}\n'
            f'Issues: {"; ".join(issues)}\n📝 Original message: {original}\n'
            'Please review and create manually.')


def dry_run_preview(sender, customer, mode, lines, item_sum, stated_total, email, warnings,
                    heading='🧪 DRY RUN — would create'):
    body = '\n'.join(f"  • {l['name']}" + (f" ({l['description']})" if l.get('description') else '') +
                     f" — qty {l['quantity']:g} × ₹{l['rate']:g}" for l in lines)
    total = sum(l['quantity'] * l['rate'] for l in lines)
    out = [f'{heading} ({mode})', f'🏪 Customer: {customer}', f'👤 Sender: {sender}', body,
           f'💰 Total (with markup): ₹{total:,.2f}', f'📧 Email: {email or "none"}']
    out += [f'{"⚠️" if w.startswith("DEFAULT CUSTOMER") else "ℹ️"} {w}' for w in warnings or []]
    return '\n'.join(out)