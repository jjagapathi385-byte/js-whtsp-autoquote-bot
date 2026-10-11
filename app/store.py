"""State in Redis (Upstash). Only simple commands are used: get/set/delete and a sorted set of due times."""
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

    # bot status + switches (persist in Redis)
    def status(self): return self.r.get('bot:status') or 'STOPPED'      # safe default: stopped
    def set_status(self, s): self.r.set('bot:status', s)
    def started_at(self): return float(self.r.get('bot:started_at') or 0)
    def set_started_at(self, ts): self.r.set('bot:started_at', str(ts))

    def flag(self, name, default):
        v = self.r.get(f'switch:{name}')
        return default if v is None else v == '1'

    def set_flag(self, name, value): self.r.set(f'switch:{name}', '1' if value else '0')

    # bridge heartbeat
    def heartbeat(self, now=None): self.r.set('bridge:last_seen', str(now or time.time()))
    def last_heartbeat(self): return float(self.r.get('bridge:last_seen') or 0)
    def alerted(self): return self.r.get('bridge:alerted') == '1'
    def set_alerted(self, v): self.r.set('bridge:alerted', '1' if v else '0')

    # pending messages (each has its own due time)
    def add_pending(self, msg):
        self.r.set(f'pending:{msg["id"]}', json.dumps(msg))
        self.r.zadd('pending', {msg['id']: msg['due']})

    def get_pending(self, mid):
        v = self.r.get(f'pending:{mid}')
        return json.loads(v) if v else None

    def remove_pending(self, mid):
        self.r.zrem('pending', mid)
        self.r.delete(f'pending:{mid}')

    def pending_all(self):
        return [m for m in (self.get_pending(i) for i in self.r.zrange('pending', 0, -1)) if m]

    def due_ids(self, now): return self.r.zrangebyscore('pending', 0, now)

    def clear_pending(self):
        removed = self.pending_all()
        for m in removed:
            self.remove_pending(m['id'])
        return removed

    # processed (duplicate prevention): marked the moment Zoho confirms
    def mark_processed(self, mid, info): self.r.set(f'processed:{mid}', json.dumps(info), ex=60 * 60 * 24 * 45)
    def is_processed(self, mid): return self.r.get(f'processed:{mid}') is not None
    def processed_info(self, mid):
        v = self.r.get(f'processed:{mid}')
        return json.loads(v) if v else None

    # sender brand context: the brand (KFC/PHD/...) a sender recently used explicitly, with message time
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


    def claim(self, mid):
        """Atomically take a due message off the queue. True for exactly one caller, even if two
        processes race (ZREM returns how many members it removed)."""
        won = bool(self.r.zrem('pending', mid))
        if won:
            self.r.delete(f'pending:{mid}')
        return won

    # recent results shown in the admin panel (last 30)
    def add_recent(self, entry):
        items = json.loads(self.r.get('recent') or '[]')
        self.r.set('recent', json.dumps(([entry] + items)[:30]))

    def recent(self):
        return json.loads(self.r.get('recent') or '[]')
