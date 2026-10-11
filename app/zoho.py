"""Zoho Invoice (India, zoho.in) client — adapted from the existing QuoteBot app.py.
`http` can be injected for tests (anything with get/post/put like `requests`)."""
import logging
import time
from datetime import datetime, timedelta, timezone

import requests
from . import config

TS_STATES = ['telangana', 'ts', '36-telangana']


class ZohoError(Exception):
    pass


class Zoho:
    def __init__(self, http=None):
        self.http = http or requests
        self._token = {'value': None, 'expires_at': 0}
        self._last_seq = 0
        self._customers = None
        self._items = None

    # ── auth + requests ──
    def token(self):
        if self._token['value'] and time.time() < self._token['expires_at'] - 60:
            return self._token['value']
        if not config.ZOHO_REFRESH_TOKEN:
            raise ZohoError('ZOHO_REFRESH_TOKEN is not set')
        d = self.http.post(config.ZOHO_TOKEN_URL, data={
            'grant_type': 'refresh_token', 'client_id': config.ZOHO_CLIENT_ID,
            'client_secret': config.ZOHO_CLIENT_SECRET, 'refresh_token': config.ZOHO_REFRESH_TOKEN}).json()
        if 'access_token' not in d:
            raise ZohoError(f'Token refresh failed: {d}')
        self._token = {'value': d['access_token'], 'expires_at': time.time() + 3300}
        return d['access_token']

    def headers(self):
        return {'Authorization': f'Zoho-oauthtoken {self.token()}',
                'X-com-zoho-invoice-organizationid': config.ZOHO_ORG_ID,
                'Content-Type': 'application/json'}

    def get(self, path, params=None):
        return self.http.get(f'{config.ZOHO_BASE}{path}', headers=self.headers(), params=params or {})

    def post(self, path, data):
        return self.http.post(f'{config.ZOHO_BASE}{path}', headers=self.headers(), json=data)

    def put(self, path, data):
        return self.http.put(f'{config.ZOHO_BASE}{path}', headers=self.headers(), json=data)

    def _paged(self, path, key, params):
        out, page = [], 1
        while True:
            d = self.get(path, dict(params, page=page, per_page=200)).json()
            out.extend(d.get(key, []))
            if not d.get('page_context', {}).get('has_more_page', False):
                return out
            page += 1

    # ── lists (cached; call refresh_* to reload) ──
    def customers(self, refresh=False):
        if self._customers is None or refresh:
            self._customers = self._paged('/contacts', 'contacts', {'contact_type': 'customer'})
        return self._customers

    def items(self, refresh=False):
        if self._items is None or refresh:
            self._items = self._paged('/items', 'items', {})
        return self._items

    # ── GST ──
    def gst18_tax_id(self):
        for tax in self.get('/taxes').json().get('taxes', []):
            pct, name = tax.get('tax_percentage', 0), tax.get('tax_name', '').upper()
            if pct == 18 or ('18' in name and ('GST' in name or 'CGST' in name)):
                return tax['tax_id']
        return None

    @staticmethod
    def customer_state(customer):
        """State from Place of Supply (more reliable than billing address)."""
        pos = customer.get('place_of_supply', '') or ''
        if pos:
            if '-' in pos:
                pos = pos.split('-', 1)[1]
            return pos.strip()
        return (customer.get('billing_address') or {}).get('state', '')

    def is_interstate(self, customer):
        state = self.customer_state(customer).lower()
        return state not in TS_STATES if state else False

    # ── items ──
    def create_item(self, name, hsn_code, rate, tax_id, is_service=False):
        payload = {'name': name, 'rate': rate, 'product_type': 'service' if is_service else 'goods',
                   'hsn_or_sac': str(hsn_code)}
        if not is_service:
            payload['item_type'] = 'inventory'
        if tax_id:
            payload['tax_id'] = tax_id
        resp = self.post('/items', payload).json()
        item = resp.get('item', {})
        if item:
            return item
        if resp.get('code') == 1001 and 'already exists' in resp.get('message', ''):
            existing = self.get('/items', {'search_text': name}).json().get('items', [])
            if existing:
                ex = existing[0]
                ex_hsn = ex.get('hsn_or_sac', '').strip().replace('0', '')
                if not ex_hsn and hsn_code and str(hsn_code).replace('0', ''):
                    self.put(f'/items/{ex["item_id"]}', {
                        'name': ex.get('name', name), 'rate': ex.get('rate', rate), 'hsn_or_sac': str(hsn_code),
                        'product_type': 'service' if is_service else 'goods'})
                return ex
        raise ZohoError(f'Item creation failed: {resp}')

    # ── estimates ──
    def default_notes(self):
        try:
            d = self.get('/settings/estimates').json()
            return (d.get('estimate_settings', d)).get('notes', '').strip()
        except Exception:
            return ''

    def next_seq(self):
        try:
            ests = self.get('/estimates', {'sort_column': 'created_time', 'sort_order': 'D',
                                           'per_page': 10}).json().get('estimates', [])
            zoho_max = 0
            for est in ests:
                digits = ''.join(ch for ch in est.get('estimate_number', '').split('-')[-1] if ch.isdigit())
                if digits:
                    zoho_max = max(zoho_max, int(digits))
            m = max(zoho_max, self._last_seq)
            return m + 1 if m > 0 else 1
        except Exception:
            return self._last_seq + 1 if self._last_seq > 0 else 1

    def create_estimate(self, customer_id, customer_name, line_items, notes=''):
        """Create the estimate with number CUSTOMERNAME-QT-000XXX. Retries on duplicate numbers."""
        clean = customer_name.strip().upper().replace(' ', '_')
        result = {}
        for attempt in range(5):
            seq = self.next_seq() + attempt
            payload = {'customer_id': customer_id, 'estimate_number': f'{clean}-QT-{str(seq).zfill(6)}',
                       'line_items': line_items}
            default = self.default_notes()
            combined = f'{default}\n{notes.strip()}' if (notes and notes.strip() and default) else \
                (notes.strip() if notes and notes.strip() else default)
            if combined:
                payload['notes'] = combined
            result = self.post('/estimates', payload).json()
            if result.get('estimate'):
                self._last_seq = seq
                return result
            if result.get('code') == 1001:
                self._last_seq = seq
                continue
            return result
        return result

    @staticmethod
    def build_line_items(lines, item_ids, tax_id=None):
        """lines from rules.apply_rules; item_ids parallel list (Zoho item_id or '')."""
        out = []
        for line, item_id in zip(lines, item_ids):
            li = {'item_id': item_id, 'name': line['name'], 'quantity': line['quantity'], 'rate': line['rate']}
            if line.get('description'):
                li['description'] = line['description']       # per-line text, e.g. "10 meters"
            if tax_id:
                li['tax_id'] = tax_id
            out.append(li)
        return out

    def send_estimate_email(self, estimate_id, to_email):
        sent = self.post(f'/estimates/{estimate_id}/status/sent', {}).json()
        if sent.get('code', 0) not in [0, 'success']:
            logging.warning(f'Mark as sent response: {sent}')
        return self.post(f'/estimates/{estimate_id}/email', {
            'to_mail_ids': [to_email], 'send_from_org_email_id': True, 'attach_pdf': True}).json()

    def estimate_pdf(self, estimate_id):
        h = self.headers()
        h.pop('Content-Type', None)
        r = self.http.get(f'{config.ZOHO_BASE}/estimates/{estimate_id}?accept=pdf', headers=h)
        if r.status_code == 200 and 'application/pdf' in r.headers.get('Content-Type', ''):
            return r.content
        return None

    # ── duplicate guard ──
    @staticmethod
    def _parse_time(s):
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                t = datetime.strptime(s, fmt)
                return t if t.tzinfo else t.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            except (ValueError, TypeError):
                continue
        return None

    def find_recent_duplicate(self, customer_id, subtotal, gst_rate=0.18, now=None):
        """A recent estimate for the SAME customer with the SAME amount (within the window).
        Same store + different items is a legitimate new quote, so the customer alone is not enough.
        `subtotal` is the pre-GST total of the new quote; it is compared with the estimate's
        sub_total when Zoho returns one, and with subtotal x (1 + gst_rate) against its total."""
        now = now or datetime.now(timezone.utc)
        tol = config.MONEY_TOLERANCE
        ests = self.get('/estimates', {'customer_id': customer_id, 'sort_column': 'created_time',
                                       'sort_order': 'D', 'per_page': 10}).json().get('estimates', [])
        for est in ests:
            t = self._parse_time(est.get('created_time', ''))
            if not t or now - t > timedelta(minutes=config.DUPLICATE_WINDOW_MINUTES):
                continue
            same_sub = 'sub_total' in est and abs(float(est['sub_total']) - subtotal) <= tol
            same_total = 'total' in est and abs(float(est['total']) - subtotal * (1 + gst_rate)) <= tol * 2
            if same_sub or same_total:
                return est
        return None
