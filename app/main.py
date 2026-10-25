"""Flask app: the routes the WhatsApp bridge calls, the PIN-protected admin panel, and the worker thread.

Run with exactly ONE gunicorn worker process (the timer loop lives in it):
    gunicorn app.wsgi:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
"""
import hmac
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session

from . import config, notify
from .bridge_client import Bridge
from .engine import Engine
from .matching import brand_from_name
from .pipeline import Deps
from .store import Store, connect
from .zoho import Zoho

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JS Auto QuoteBot</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
.wrap{max-width:640px;margin:0 auto;padding:14px}
.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:20px;margin:6px 0 12px}h2{font-size:15px;margin:0 0 8px;color:#555}
.badge{display:inline-block;padding:4px 12px;border-radius:99px;font-weight:700;color:#fff}
.run{background:#1f9d55}.stop{background:#c0392b}.ok{color:#1f9d55}.bad{color:#c0392b}
button{font-size:16px;padding:12px 18px;border:0;border-radius:10px;color:#fff;background:#2d6cdf;margin:4px 4px 4px 0}
button.red{background:#c0392b}button.green{background:#1f9d55}button.grey{background:#6b7280}button.small{font-size:13px;padding:7px 11px}
input[type=password]{font-size:18px;padding:12px;width:100%;box-sizing:border-box;margin-bottom:10px}
.pend{border-top:1px solid #eee;padding:8px 0;font-size:14px}.pend pre{white-space:pre-wrap;margin:4px 0;font-family:inherit}
.muted{color:#777;font-size:13px}form{display:inline}
</style></head><body><div class="wrap">{{ body|safe }}</div></body></html>"""


def build_engine():
    store = Store(connect(config.REDIS_URL))
    zoho = Zoho()
    deps = Deps(zoho=zoho, store=store, notify=notify.send, bridge=Bridge())
    return Engine(store, zoho, deps)


def create_app(engine=None, start_worker=True, async_events=True):
    # Show INFO lines (what happened to each message) in the Railway logs. Message TEXT is never logged.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    app = Flask(__name__)
    app.secret_key = config.FLASK_SECRET_KEY or secrets.token_hex(16)   # set FLASK_SECRET_KEY or logins reset on restart
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    eng = engine or build_engine()
    app.engine = eng
    executor = ThreadPoolExecutor(max_workers=1)      # ONE thread: events from the bridge are handled in order
    fails = {}                                        # ip -> (count, first_time) for PIN lockout

    # ── bridge routes ──
    def bridge_auth():
        sec = config.BRIDGE_SECRET
        if not sec or not hmac.compare_digest(request.headers.get('X-Bridge-Secret', ''), sec):
            abort(403)

    def run_event(ev):
        try:
            res = eng.handle_event(ev)
            logging.info('event %s -> %s', ev.get('id'), res)
        except Exception:
            logging.exception('event failed')

    def redis_problem():
        """None if Redis answers, otherwise the error CLASS name only (never the address or password)."""
        try:
            eng.store.r.get('bot:status')
            return None
        except Exception as e:
            logging.exception('Redis check failed')
            return type(e).__name__

    @app.get('/health')
    def health():
        bad = redis_problem()
        if bad:
            return jsonify(ok=False, redis=False, error=bad), 503
        return jsonify(ok=True, redis=True)

    @app.post('/incoming')
    def incoming():
        bridge_auth()
        ev = request.get_json(silent=True) or {}
        if not ev.get('id'):
            return jsonify(error='missing id'), 400
        eng.store.heartbeat()
        (executor.submit(run_event, ev) if async_events else run_event(ev))
        return jsonify(queued=True), 202

    @app.post('/heartbeat')
    def heartbeat():
        bridge_auth()
        eng.store.heartbeat()
        return jsonify(ok=True)

    # ── admin panel ──
    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(16)
        return session['csrf']

    def guard():
        if not session.get('admin'):
            abort(403)
        if not hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', 'x')):
            abort(400)

    def page(body):
        return render_template_string(PAGE, body=body)

    def esc(s):
        return (str(s or '')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def login_form(msg=''):
        return page(f'<h1>JS Auto QuoteBot</h1><div class="card"><form method="post" action="/admin/login">'
                    f'<input type="password" name="pin" placeholder="PIN" autofocus>'
                    f'<button>Log in</button></form><p class="bad">{esc(msg)}</p></div>')

    @app.get('/admin')
    def admin():
        if not session.get('admin'):
            return login_form()
        bad = redis_problem()
        if bad:
            return page('<h1>JS Auto QuoteBot</h1><div class="card"><h2>Cannot reach Redis</h2>'
                        f'<p class="bad">{esc(bad)}</p><p>Check the <b>REDIS_URL</b> variable of this service in Railway: '
                        'it must be the <b>rediss://</b> connection URL from Upstash with the current password. '
                        'Then redeploy. The app itself is running.</p></div>'), 503
        st = eng.store
        running = st.status() == 'RUNNING'
        age = time.time() - st.last_heartbeat() if st.last_heartbeat() else None
        bridge_ok = age is not None and age < config.HEARTBEAT_ALERT_SECONDS
        tok = csrf()

        def btn(path, label, cls='', extra='', confirm=''):
            c = f' onclick="return confirm(\'{confirm}\')"' if confirm else ''
            return (f'<form method="post" action="{path}"><input type="hidden" name="csrf" value="{tok}">{extra}'
                    f'<button class="{cls}"{c}>{label}</button></form>')

        sw = lambda name, label, default: (
            btn('/admin/switch', f'{label}: {"ON" if st.flag(name, default) else "OFF"} (tap to switch)',
                'green' if st.flag(name, default) else 'grey',
                f'<input type="hidden" name="name" value="{name}"><input type="hidden" name="value" value="{0 if st.flag(name, default) else 1}">'))
        rows = ''
        for m in st.pending_all():
            left = max(0, int(m['due'] - time.time()))
            rows += (f'<div class="pend"><b>{esc(eng.label(m.get("contact_name"), m.get("sender")))}</b> · due in {left}s<pre>{esc(m["text"])}</pre>'
                     + btn('/admin/cancel', 'Cancel', 'red small', f'<input type="hidden" name="id" value="{esc(m["id"])}">')
                     + btn('/admin/done', 'Done manually', 'grey small', f'<input type="hidden" name="id" value="{esc(m["id"])}">') + '</div>')
        ist = lambda t: time.strftime('%H:%M:%S', time.gmtime(t + 19800))
        events = ''.join(f'<div class="pend"><span class="muted">{ist(e["t"])} IST</span> <b>{esc(e["kind"])}</b> '
                         f'· {esc(e["action"])}<div class="muted">{esc(e["sender"])} · id …{esc(e["id"])} {esc(e["why"])}</div></div>'
                         for e in list(eng.events)[:12])
        seen = {k: v for k, v in st.seen_senders()[:30]}
        for k in st.sender_brands():
            seen.setdefault(k, {'sender': k, 'contact': ''})
        brand_rows = ''
        for k, e in list(seen.items())[:20]:
            manual = st.sender_brand(k)
            auto = brand_from_name(e.get('contact')) or brand_from_name(e.get('sender'))
            cur = manual or auto
            label = (f'{cur} ({"assigned here" if manual else "from the contact name"})' if cur else 'no brand known')
            who = esc(e.get('contact') or e.get('sender') or k)
            hid = f'<input type="hidden" name="key" value="{esc(k)}">'
            brand_rows += (f'<div class="pend"><b>{who}</b> <span class="muted">{esc(e.get("sender") if e.get("contact") else "")}</span>'
                           f'<div class="muted">{label}</div>')
                           + btn('/admin/sender-brand', 'KFC', 'small', hid + '<input type="hidden" name="brand" value="KFC">')
           		   + btn('/admin/sender-brand', 'PHD', 'small', hid + '<input type="hidden" name="brand" value="PHD">')
           		   + btn('/admin/sender-brand', 'Costa', 'small', hid + '<input type="hidden" name="brand" value="COSTA">')
           		   + btn('/admin/sender-brand', 'Paradise', 'small', hid + '<input type="hidden" name="brand" value="PARADISE">')
           		   + btn('/admin/sender-brand', 'Clear', 'grey small', hid + '<input type="hidden" name="brand" value="">')
        recent = ''.join(f'<div class="pend"><b>{esc(r.get("status"))}</b> · {esc(r.get("sender"))}'
                         f'<div class="muted">{esc(r.get("detail"))}</div></div>' for r in st.recent()[:10])
        body = (f'<h1>JS Auto QuoteBot</h1>'
                f'<div class="card"><h2>Status</h2><span class="badge {"run" if running else "stop"}">{"RUNNING" if running else "STOPPED"}</span>'
                f' &nbsp; WhatsApp bridge: <b class="{"ok" if bridge_ok else "bad"}">{"connected" if bridge_ok else "NOT responding"}</b><br>'
                + (btn('/admin/stop', 'STOP bot', 'red', confirm='Stop the bot? Pending messages are cleared and sent to Telegram.') if running
                   else btn('/admin/start', 'START bot', 'green', confirm='Start fresh? Only messages from now on are processed.'))
                + '</div>'
                f'<div class="card"><h2>Switches</h2>{sw("dry_run", "Dry run (nothing created)", config.DRY_RUN_DEFAULT)}'
                f'{sw("post_to_group", "Post PDF to WhatsApp group", config.POST_TO_GROUP_DEFAULT)}</div>'
                f'<div class="card"><h2>Waiting ({len(st.pending_all())})</h2>{rows or "<span class=muted>Nothing waiting.</span>"}</div>'
                f'<div class="card"><h2>Who sends which brand</h2>{brand_rows or "<span class=muted>Senders appear here after they post.</span>"}'
                '<div class="muted">Used ONLY when a message names no brand (KFC / PHD) and has no store code. '
                'A brand shown "from the contact name" comes from the name saved in your phone.</div></div>'
                f'<div class="card"><h2>Recent results</h2>{recent or "<span class=muted>None yet.</span>"}</div>'
                f'<div class="card"><h2>Recent events from WhatsApp</h2>{events or "<span class=muted>None yet.</span>"}'
                '<div class="muted">What the bot did with each message, edit, delete or attachment (no message text). '
                'If an edit is missing here, the bridge never sent it.</div></div>'
                + btn('/admin/logout', 'Log out', 'grey small'))
        return page(body)

    @app.post('/admin/login')
    def login():
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
        n, first = fails.get(ip, (0, time.time()))
        if time.time() - first > 600:
            n, first = 0, time.time()
        if n >= 5:
            return login_form('Too many attempts. Try again in 10 minutes.'), 429
        pin = request.form.get('pin', '')
        if config.ADMIN_PIN and hmac.compare_digest(pin, config.ADMIN_PIN):
            session.clear()
            session['admin'] = True
            fails.pop(ip, None)
            return redirect('/admin')
        fails[ip] = (n + 1, first)
        return login_form('Wrong PIN.' if config.ADMIN_PIN else 'ADMIN_PIN is not set.'), 401

    @app.post('/admin/logout')
    def logout():
        guard()
        session.clear()
        return redirect('/admin')

    @app.post('/admin/stop')
    def stop():
        guard(); eng.stop(); return redirect('/admin')

    @app.post('/admin/start')
    def start():
        guard(); eng.start(); return redirect('/admin')

    @app.post('/admin/switch')
    def switch():
        guard()
        name = request.form.get('name', '')
        if name not in ('dry_run', 'post_to_group'):
            abort(400)
        eng.store.set_flag(name, request.form.get('value') == '1')
        return redirect('/admin')

    @app.post('/admin/sender-brand')
    def sender_brand():
        guard()
        brand = request.form.get('brand', '')
        if brand not in ('KFC', 'PHD', 'PARADISE', 'COSTA', 'VAANGO', ''):
            abort(400)
        key = request.form.get('key', '').strip()
        if not key:
            abort(400)
        eng.store.set_sender_brand(key, brand)
        return redirect('/admin')

    @app.post('/admin/cancel')
    def cancel():
        guard(); eng.cancel(request.form.get('id', '')); return redirect('/admin')

    @app.post('/admin/done')
    def done():
        guard(); eng.done_manually(request.form.get('id', '')); return redirect('/admin')

    # ── worker thread: checks due timers one at a time ──
    def worker():
        while True:
            try:
                eng.tick()
                eng.check_bridge()
            except Exception:
                logging.exception('worker loop error')
            time.sleep(config.POLL_SECONDS)

    if start_worker:
        threading.Thread(target=worker, daemon=True, name='quote-worker').start()
    return app
