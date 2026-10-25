"""Settings for JS Automatic QuoteBot (non-secret defaults + env overrides)."""
import json
import os

MARKUP = float(os.environ.get('MARKUP', '1.10'))            # 10% markup, same as existing QuoteBot
TIMER_SECONDS = int(os.environ.get('TIMER_SECONDS', '180'))  # 3-minute wait per message

# Total-mismatch safety limits (see brief section 9)
TOTAL_RATIO_HIGH = float(os.environ.get('TOTAL_RATIO_HIGH', '0'))    # 0 = off (item prices always win over a wrong total)
TOTAL_RATIO_LOW = float(os.environ.get('TOTAL_RATIO_LOW', '0'))      # 0 = off. e.g. 2.0 / 0.5 would flag sums far from the total
MONEY_TOLERANCE = 1.0  # rupees; differences up to this count as "matches"

# Staged rollout switches (defaults are the safest: dry-run on, posting off)
DRY_RUN_DEFAULT = True
POST_TO_GROUP_DEFAULT = False

# ── Credentials (all from environment variables; never hard-code) ──────────────
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
ZOHO_CLIENT_ID = os.environ.get('ZOHO_CLIENT_ID', '')
ZOHO_CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET', '')
ZOHO_REFRESH_TOKEN = os.environ.get('ZOHO_REFRESH_TOKEN', '')
ZOHO_ORG_ID = os.environ.get('ZOHO_ORG_ID', '')
ZOHO_BASE = 'https://invoice.zoho.in/api/v3'
ZOHO_TOKEN_URL = 'https://accounts.zoho.in/oauth/v2/token'
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Paradise-specific configuration (isolated from KFC/PHD)
PARADISE_GROUP_JID = os.environ.get('PARADISE_GROUP_JID', '')
MARKUP_PARADISE = float(os.environ.get('MARKUP_PARADISE', '1.05'))  # 5% default markup

# Duplicate guard: same customer AND same total within this many minutes (see zoho.find_recent_duplicate)
DUPLICATE_WINDOW_MINUTES = 30

# ── Runtime ────────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get('REDIS_URL', '')
ADMIN_PIN = os.environ.get('ADMIN_PIN', '')                  # admin panel is locked if empty
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', '')
BRIDGE_SECRET = os.environ.get('BRIDGE_SECRET', '')          # shared secret with the Node bridge
BRIDGE_URL = os.environ.get('BRIDGE_URL', '')                # where Flask sends PDFs for the group
POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '15'))     # due-timer check; 15s keeps Upstash usage low
HEARTBEAT_ALERT_SECONDS = 300                                # alert if the bridge is silent this long

# Brand carry-over: a store named without KFC/PHD uses the brand the same sender used recently
BRAND_CONTEXT_SECONDS = int(os.environ.get('BRAND_CONTEXT_SECONDS', str(3 * 3600)))

# Messages from these numbers are ignored completely (the owner posts manual quotes, POs, payments).
# Comma-separated, any format; only the last 10 digits are compared. Set in the OWNER_NUMBERS env var.
OWNER_NUMBERS = [n.strip() for n in os.environ.get('OWNER_NUMBERS', '').split(',') if n.strip()]
OWNER_JIDS = [n.strip() for n in os.environ.get('OWNER_JIDS', '').split(',') if n.strip()]   # exact sender ids (see bridge logs)

# When the store/customer cannot be matched confidently, the quote is created under this Zoho customer
# (create a customer with exactly this name in Zoho) instead of being skipped.
DEFAULT_CUSTOMER_NAME = os.environ.get('DEFAULT_CUSTOMER_NAME', 'DEFAULT-CUSTOMER')
DEFAULT_CUSTOMER_NOTE = os.environ.get(
    'DEFAULT_CUSTOMER_NOTE',
    'Quote created without the proper customer name. The proper customer name will be added while creating the PO.')

# ── Groq: models, token pacing, retries ───────────────────────────────────────
# Models are tried in this order. The first is a normal (non-reasoning) model: this job needs a short JSON answer, and
# reasoning models spend a big part of the token budget on hidden thinking (which can even leave the answer empty).
# Models are tried in this order. Groq limits tokens PER MODEL per minute (free plan: 8000 for gpt-oss-120b and
# gpt-oss-20b, 12000 for llama-3.3-70b-versatile), so a chain of different models spreads the load.
# A model your account cannot use (error "model_not_found") is skipped automatically for an hour.
GROQ_MODELS = [m.strip() for m in os.environ.get(
    'GROQ_MODELS', 'llama-3.3-70b-versatile,openai/gpt-oss-120b,openai/gpt-oss-20b').split(',') if m.strip()]
GROQ_TPM_LIMIT = int(os.environ.get('GROQ_TPM_LIMIT', '8000'))   # tokens per minute per model (lower it if you still see 429s)
GROQ_TPM_SAFETY = float(os.environ.get('GROQ_TPM_SAFETY', '0.85'))
MAX_AI_RETRIES = int(os.environ.get('MAX_AI_RETRIES', '3'))      # a message is retried this many times when the AI is busy
AI_RETRY_DELAY_SECONDS = int(os.environ.get('AI_RETRY_DELAY_SECONDS', '60'))
GROQ_REASONING_EFFORT = os.environ.get('GROQ_REASONING_EFFORT', 'low')   # sent only to openai/gpt-oss-* models; '' = do not send
GROQ_REASONING_HEADROOM = int(os.environ.get('GROQ_REASONING_HEADROOM', '500'))   # extra output tokens reserved for hidden thinking

# Caption of the quote PDF posted in the group, for EVERY quote. {user} = the sender (a real WhatsApp tag),
# {customer} = the customer name (when the store was unclear: the store name as written in the message),
# {number} = the estimate number. Example: '@{user} {customer}' -> '@Ravi KFC-CMR-VIZAG-MA-VSK-K215'.
CAPTION_TEMPLATE = os.environ.get('CAPTION_TEMPLATE', '@{user} {customer}')

# Fixed HSN/SAC codes you trust, applied BEFORE the AI is asked. JSON, keyword in the item name -> code, e.g.
# {"lock": "83014090", "wire": "85444290", "electrical work": {"code": "995469", "service": true}}
def _hsn_overrides():
    try:
        raw = json.loads(os.environ.get('HSN_OVERRIDES', '') or '{}')
        return {str(k).lower(): v for k, v in raw.items()} if isinstance(raw, dict) else {}
    except ValueError:
        return {}


HSN_OVERRIDES = _hsn_overrides()
GENERIC_HSN = ('99999999', '999999')      # placeholder codes: the owner is told to fix them in Zoho

# Estimate numbers: <ESTIMATE_PREFIX>-<last word of the customer name>-QT-<ascending number>, e.g. JSINT-K240-QT-000666
ESTIMATE_PREFIX = os.environ.get('ESTIMATE_PREFIX', 'JSINT')
