"""Message intake + per-message 3-minute timers + stop/start (brief sections 5, 6, 13).

Every quote-like message gets its own independent 3-minute timer (due time in a Redis sorted set).
A single worker thread (see main.py) takes due messages one at a time, which gives the same
one-after-another processing as a queue without a second queue system.

Attachments (photos, PDFs, voice notes, video, stickers) are IGNORED completely: the bot never reads
or downloads them and spends no AI tokens on them. The owner handles them manually.
"""
import logging
import re
import threading
import time
from collections import deque

from . import config, pipeline
from .intake import classify, extract_codes
from .matching import detect_brand

CORRECTION_RE = re.compile(
    r'\b(correction|corrected|instead|changed?|wrong|mistake|updated?|revised?|cancel previous|ignore previous)\b', re.I)


class Engine:
    def __init__(self, store, zoho, deps, now=time.time):
        self.store, self.zoho, self.deps, self.now = store, zoho, deps, now
        self.lock = threading.RLock()          # events, admin actions and timer pickup never interleave
        self.events = deque(maxlen=40)         # what happened to the last events (no message text), shown in /admin
        self._info = (0, set(), [])
        if getattr(deps, 'now', None) is None:
            deps.now = now

    # ── helpers ──
    def customer_info(self):
        t, codes, names = self._info
        if self.now() - t > 600 or not names:
            from .matching import known_codes
            custs = self.zoho.customers(refresh=bool(names))
            self._info = (self.now(), known_codes(custs), [c['contact_name'] for c in custs])
        return self._info[1], self._info[2]

    def _notify(self, text):
        try:
            self.deps.notify(text)
        except Exception as e:
            logging.warning('notify failed: %s', e)

    def _sender_pending(self, sender):
        return sorted([m for m in self.store.pending_all() if m.get('sender') == sender], key=lambda m: m['ts'])

    @staticmethod
    def _is_owner(ev):
        """My own number is ignored. Matched by phone number (last 10 digits) or, because WhatsApp groups
        sometimes hide numbers behind an internal id (LID), by the exact sender id in OWNER_JIDS."""
        digits = re.sub(r'\D', '', str(ev.get('sender_number') or ''))
        owners = {re.sub(r'\D', '', n)[-10:] for n in config.OWNER_NUMBERS if re.sub(r'\D', '', n)}
        if digits and digits[-10:] in owners:
            return True
        jid = str(ev.get('sender_jid') or '').split(':')[0].split('@')[0]
        return bool(jid) and jid in {j.split('@')[0] for j in config.OWNER_JIDS}

    # ── events from the bridge ──
    def handle_event(self, ev):
        with self.lock:
            res = self._handle(ev)
            self.events.appendleft({'t': self.now(), 'kind': 'attachment' if ev.get('media') else ev.get('event', 'message'),
                                    'id': str(ev.get('id', ''))[-6:], 'sender': ev.get('sender', ''),
                                    'action': res.get('action', ''), 'why': res.get('why', '') or res.get('via', '')})
            return res

    def _handle(self, ev):
        if self._is_owner(ev):
            return {'action': 'ignored', 'why': 'owner number'}
        if self.store.status() != 'RUNNING':
            return {'action': 'ignored', 'why': 'bot stopped'}
        if float(ev.get('ts', 0)) < self.store.started_at():
            return {'action': 'ignored', 'why': 'sent before the bot was started'}
        kind = ev.get('event', 'message')
        if kind == 'delete':
            return self._on_delete(ev)
        if kind == 'edit':
            return self._on_edit(ev)
        if ev.get('media'):                       # photos, PDFs, voice notes, video, stickers: ignored
            return {'action': 'ignored', 'why': 'attachment'}
        return self._on_message(ev)

    def _record_brand(self, ev, cls):
        """Remember an EXPLICIT brand (brand word or valid store code) so a later message from the same
        sender that names only a store (e.g. 'moosapet store') can continue with the same brand."""
        brand = detect_brand(ev.get('text') or '', cls.code if cls.valid_code else None)
        if brand and ev.get('sender'):
            self.store.add_brand_context(ev['sender'], float(ev['ts']), brand)

    def _schedule(self, ev, cls):
        self._record_brand(ev, cls)
        msg = {'id': ev['id'], 'sender': ev.get('sender', ''), 'sender_jid': ev.get('sender_jid', ''), 'text': ev['text'], 'ts': float(ev['ts']),
               'due': float(ev['ts']) + config.TIMER_SECONDS, 'is_forwarded': bool(ev.get('is_forwarded')),
               'intake_action': cls.action, 'intake_reasons': cls.reasons, 'default_ok': cls.default_ok}
        self.store.add_pending(msg)
        return {'action': 'scheduled', 'id': ev['id'], 'due': msg['due']}

    def _on_message(self, ev):
        text, sender = ev.get('text') or '', ev.get('sender', '')
        if self.store.is_processed(ev['id']) or self.store.get_pending(ev['id']):
            return {'action': 'ignored', 'why': 'already known'}
        codes, names = self.customer_info()
        cls = classify(text, codes, names, is_reply=bool(ev.get('is_reply')))
        pending = self._sender_pending(sender)

        # Possible Type B correction: never guessed, the owner decides
        if pending and CORRECTION_RE.search(text) and cls.action in ('quote', 'flag', 'ignore'):
            if cls.action != 'ignore' or len(text) > 15:
                self._notify('⚠️ Possible correction message\n'
                             f'👤 Sender: {sender}\n📝 New message: {text}\n'
                             f'📝 Pending quote: {pending[-1]["text"]}\n'
                             'The pending quote was NOT changed. Please check manually.')
                return {'action': 'correction_flagged'}

        if cls.action == 'attach_code':
            target = next((m for m in reversed(pending) if not extract_codes(m['text'])), None)
            if not target:
                return {'action': 'ignored', 'why': 'lone code with no pending quote'}
            target['text'] += '\n' + text
            target['due'] = float(ev['ts']) + config.TIMER_SECONDS
            self.store.add_pending(target)
            return {'action': 'code_attached', 'id': target['id']}

        if cls.action == 'ignore':
            if 'payment' in ' '.join(cls.reasons):
                return self._po_followup(ev)
            return {'action': 'ignored', 'why': '; '.join(cls.reasons)}
        return self._schedule(ev, cls)           # quote or flag: the timer runs, then it is processed

    def _po_followup(self, ev):
        """Payment / PO details text: cancel this sender's most recent pending quote (a resent old quote)."""
        pending = self._sender_pending(ev.get('sender', ''))
        if not pending:
            return {'action': 'ignored', 'why': 'payment text, nothing pending'}
        target = pending[-1]
        self.store.remove_pending(target['id'])
        self._notify('ℹ️ Pending quote cancelled: a PO / payment message followed it\n'
                     f'👤 Sender: {target["sender"]}\n📝 Cancelled message: {target["text"]}\n'
                     'If this was a genuine new request, please create it manually.')
        return {'action': 'cancelled_by_po', 'id': target['id']}

    def _on_edit(self, ev):
        mid = ev['id']
        pend = self.store.get_pending(mid)
        if pend:
            codes, names = self.customer_info()
            cls = classify(ev.get('text') or '', codes, names)
            if cls.action in ('quote', 'flag'):
                pend.update(text=ev['text'], due=float(ev['ts']) + config.TIMER_SECONDS,
                            intake_action=cls.action, intake_reasons=cls.reasons, default_ok=cls.default_ok)
                self._record_brand(dict(ev, sender=pend['sender'], ts=pend['ts']), cls)
                self.store.add_pending(pend)
                return {'action': 'edited_timer_reset', 'id': mid}
            self.store.remove_pending(mid)
            self._notify(f'ℹ️ Edited message no longer looks like a quote; pending quote cancelled.\n📝 {ev.get("text")}')
            return {'action': 'edited_cancelled', 'id': mid}
        if self.store.is_processed(mid):
            info = self.store.processed_info(mid) or {}
            self._notify(f'⚠️ A message was edited AFTER its quote {info.get("number", "")} was created.\n'
                         f'📝 New text: {ev.get("text")}\nPlease check the quote manually.')
            return {'action': 'edit_after_processed'}
        # The original was never queued (for example it did not look like a quote yet): the edit may make it one.
        if (ev.get('text') or '').strip():
            res = self._on_message(dict(ev, event='message'))
            res['via'] = 'edit of a message the bot had not queued'
            return res
        return {'action': 'ignored', 'why': 'unknown message'}

    def _on_delete(self, ev):
        mid = ev['id']
        pend = self.store.get_pending(mid)
        if pend:
            self.store.remove_pending(mid)
            self._notify(f'ℹ️ Message deleted — quote cancelled.\n👤 {pend["sender"]}\n📝 {pend["text"]}')
            return {'action': 'deleted_cancelled', 'id': mid}
        if self.store.is_processed(mid):
            info = self.store.processed_info(mid) or {}
            self._notify(f'⚠️ A message was deleted AFTER its quote {info.get("number", "")} was created. '
                         'Please void it in Zoho if it is not needed.')
            return {'action': 'delete_after_processed'}
        return {'action': 'ignored', 'why': 'unknown message'}

    # ── timers: one due message at a time, oldest first ──
    def tick(self):
        results = []
        if self.store.status() != 'RUNNING':
            return results
        for mid in self.store.due_ids(self.now()):
            with self.lock:
                msg = self.store.get_pending(mid)
                if not self.store.claim(mid):         # someone else (an event, another process) took it
                    continue
            if not msg or self.store.is_processed(mid):
                continue
            try:                                       # the slow part runs OUTSIDE the lock
                res = pipeline.process_message(msg, self.deps)
            except Exception as e:
                logging.exception('processing failed')
                self._notify(f'❌ Processing error — please handle manually.\n👤 {msg.get("sender")}\n'
                             f'📝 {msg.get("text")}\nError: {e}')
                res = {'status': 'error', 'error': str(e)}
            results.append(res)
            self.store.add_recent({'ts': self.now(), 'sender': msg.get('sender'), 'status': res.get('status'),
                                   'detail': res.get('number') or res.get('customer') or '; '.join(res.get('issues', [])) or res.get('error', ''),
                                   'text': (msg.get('text') or '')[:120]})
        return results

    # ── bridge watchdog ──
    def check_bridge(self):
        with self.lock:
            if self.store.status() != 'RUNNING':
                return
            silent = self.now() - self.store.last_heartbeat()
            if silent > config.HEARTBEAT_ALERT_SECONDS and not self.store.alerted():
                self._notify('🔌 WhatsApp bridge is not responding. The bot is NOT seeing group messages. '
                             'Please check the bridge / WhatsApp link.')
                self.store.set_alerted(True)
            elif silent <= config.HEARTBEAT_ALERT_SECONDS and self.store.alerted():
                self.store.set_alerted(False)
                self._notify('✅ WhatsApp bridge is back.')

    # ── admin actions ──
    def stop(self):
        with self.lock:
            self.store.set_status('STOPPED')
            removed = self.store.clear_pending()
            for m in removed:
                self._notify('⏹ Bot stopped — this pending message was NOT processed. Please handle it manually.\n'
                             f'👤 Sender: {m["sender"]}\n📝 {m["text"]}')
            return removed

    def start(self):
        """Clean slate: everything before this moment is ignored, no queue carries over."""
        with self.lock:
            self.store.clear_pending()
            self.store.set_started_at(self.now())
            self.store.set_status('RUNNING')
            self.store.set_alerted(False)
            self.store.heartbeat(self.now())

    def cancel(self, mid):
        with self.lock:
            self.store.remove_pending(mid)

    def done_manually(self, mid):
        with self.lock:
            self.store.remove_pending(mid)
            self.store.mark_processed(mid, {'manual': True})
