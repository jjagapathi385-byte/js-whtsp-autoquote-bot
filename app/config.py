"""Settings for JS Automatic QuoteBot (non-secret defaults + env overrides)."""
import os

MARKUP = float(os.environ.get('MARKUP', '1.10'))            # 10% markup, same as existing QuoteBot
TIMER_SECONDS = int(os.environ.get('TIMER_SECONDS', '180'))  # 3-minute wait per message

# Total-mismatch safety limits (see brief section 9)
TOTAL_RATIO_HIGH = float(os.environ.get('TOTAL_RATIO_HIGH', '2.0'))  # sum >= 2x stated total -> flag
TOTAL_RATIO_LOW = float(os.environ.get('TOTAL_RATIO_LOW', '0.5'))    # sum <= 0.5x stated total -> flag
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
POST_CAPTION = os.environ.get('POST_CAPTION', 'Quotation attached.')

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
