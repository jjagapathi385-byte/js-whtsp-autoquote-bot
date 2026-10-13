"""State in Redis (Upstash). Only simple commands are used: get/set/delete and a sorted set of due times.

Upstash's free plan allows 500,000 commands a month, so the bot must cost ~0 commands while idle.
Everything that is read on every timer check (bot status, switches, bridge heartbeat, and the due
times of waiting messages) is therefore kept in memory by this ONE process and written through to
Redis only when it changes. Redis stays the durable copy: after a restart the cache is rebuilt from it.
This relies on running exactly one gunicorn worker process (see Procfile)."""
import json
import time


def connect(url):
    """The bot needs real Redis (Upstash): without it pending timers would be lost on every restart."""
    if not url:
        raise RuntimeError('REDIS_URL is not set. Set it to your Upstash Redis URL (rediss://...).')
    import redis
    return redis.from_url(url, decode_responses=True)


class Store:
    def __init__(self, r):
        self.r = r
        self._cache = {}        # redis key -> value (status, started_at, switches); loaded once, written through
        self._due = None        # message id -> due time (mirror of the Redis sorted set; loaded lazily)
        self._hb = 0.0          # bridge heartbeat: memory only (nothing worth persisting)
        self._alerted = False

    # ── cached single values ──
    def _get(self, key):
        if key not in self._cache:
            self._cache[key] = self.r.get(key)
        return self._cache[key]

    def _set(self, key, value):
        self.r.set(key, value)
        self._cache[key] = value

    # bot status + switches (persist in Redis)
    def status(self): return self._get('bot:status') or 'STOPPED'      # safe default: stopped
    def set_status(self, s): self._set('bot:status', s)
    def started_at(self): return float(self._get('bot:started_at') or 0)
    def set_started_at(self, ts): self._set('bot:started_at', str(ts))

    def flag(self, name, default):
        v = self._get(f'switch:{name}')
        return default if v is None else v == '1'

    def set_flag(self, name, value): self._set(f'switch:{name}', '1' if value else '0')

    # bridge heartbeat (in memory)
    def heartbeat(self, now=None): self._hb = float(now or time.time())
    def last_heartbeat(self): return self._hb
    def alerted(self): return self._alerted
    def set_alerted(self, v): self._alerted = bool(v)

    # ── pending messages (each has its own due time) ──
    def _ensure_due(self):
        if self._due is None:                     # first use after a (re)start: rebuild the mirror from Redis once
            self._due = {}
            for mid in self.r.zrange('pending', 0, -1):
                v = self.r.get(f'pending:{mid}')
                if v:
                    self._due[mid] = json.loads(v)['due']
        return self._due

    def add_pending(self, msg):
        due = self._ensure_due()
        self.r.set(f'pending:{msg["id"]}', json.dumps(msg))
        self.r.zadd('pending', {msg['id']: msg['due']})
        due[msg['id']] = msg['due']

    def get_pending(self, mid):
        if mid not in self._ensure_due():         # known not to be waiting: no Redis call
            return None
        v = self.r.get(f'pending:{mid}')
        return json.loads(v) if v else None

    def remove_pending(self, mid):
        self._ensure_due().pop(mid, None)
        self.r.zrem('pending', mid)
        self.r.delete(f'pending:{mid}')

    def pending_all(self):
        due = self._ensure_due()
        return [m for m in (self.get_pending(i) for i in sorted(due, key=lambda i: (due[i], i))) if m]

    def due_ids(self, now):
        """Ids whose timer has expired, oldest first. Answered from memory: zero Redis commands."""
        due = self._ensure_due()
        return sorted((i for i, d in due.items() if d <= now), key=lambda i: (due[i], i))

    def clear_pending(self):
        removed = self.pending_all()
        for m in removed:
            self.remove_pending(m['id'])
        return removed

    def claim(self, mid):
        """Atomically take a due message off the queue. True for exactly one caller, even if two
        processes race (ZREM returns how many members it removed)."""
        self._ensure_due().pop(mid, None)
        won = bool(self.r.zrem('pending', mid))
        if won:
            self.r.delete(f'pending:{mid}')
        return won

    # ── processed (duplicate prevention): marked the moment Zoho confirms ──
    def mark_processed(self, mid, info): self.r.set(f'processed:{mid}', json.dumps(info), ex=60 * 60 * 24 * 45)
    def is_processed(self, mid): return self.r.get(f'processed:{mid}') is not None
    def processed_info(self, mid):
        v = self.r.get(f'processed:{mid}')
        return json.loads(v) if v else None

    # ── sender brand context: the brand (KFC/PHD/...) a sender recently used explicitly, with message time ──
    def add_brand_context(self, sender, ts, brand):
        key = f'brand:{sender}'
        entries = json.loads(self.r.get(key) or '[]')
        entries = [e for e in entries if e['ts'] != ts] + [{'ts': ts, 'brand': brand}]
        self.r.set(key, json.dumps(sorted(entries, key=lambda e: e['ts'])[-10:]))

    def brand_context(self, sender, before_ts, window_seconds):
        """Latest explicit brand from this sender sent BEFORE before_ts and within the window."""
        entries = json.loads(self.r.get(f'brand:{sender}') or '[]')
        for e in reversed(entries):
            if e['ts'] < before_ts and before_ts - e['ts'] <= window_seconds:
                return e['brand']
        return None

    # ── recent results shown in the admin panel (last 30) ──
    def add_recent(self, entry):
        items = json.loads(self.r.get('recent') or '[]')
        self.r.set('recent', json.dumps(([entry] + items)[:30]))

    def recent(self):
        return json.loads(self.r.get('recent') or '[]')
