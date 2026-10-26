"""Sends the quote PDF to the WhatsApp group through the Node bridge (which holds the WhatsApp session)."""
import base64
import requests
from . import config


def _base(url):
    """Accept 'my-bridge.up.railway.app' as well as 'https://my-bridge.up.railway.app/'."""
    url = url.strip().rstrip('/')
    return url if url.startswith(('http://', 'https://')) else 'https://' + url


class Bridge:
    def __init__(self, http=None):
        self.http = http or requests

    def send_document(self, filename, data, caption, mentions=None, is_paradise=False):
        target_url = config.BRIDGE_URL_PARADISE if is_paradise else config.BRIDGE_URL_DEVYANI
        # Fallback to general BRIDGE_URL if specific ones aren't set
        if not target_url:
            target_url = config.BRIDGE_URL
            
        if not target_url or not config.BRIDGE_SECRET:
            raise RuntimeError('Bridge URLs / BRIDGE_SECRET not set')
            
        r = self.http.post(_base(target_url) + '/send',
                           headers={'X-Bridge-Secret': config.BRIDGE_SECRET},
                           json={'filename': filename, 'caption': caption, 'mentions': list(mentions or []),
                                 'data_b64': base64.b64encode(data).decode()}, timeout=60)
        if getattr(r, 'status_code', 200) != 200:
            raise RuntimeError(f'bridge returned {r.status_code}')
        return True