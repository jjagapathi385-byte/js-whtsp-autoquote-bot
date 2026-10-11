// JS Automatic QuoteBot — WhatsApp bridge (Baileys, linked device).
// It does ONE job: watch the group and forward plain-text events to the Flask app, and post PDFs to the
// group when Flask asks. No quote logic here. Attachments are dropped (never downloaded or forwarded).
import makeWASocket, {
  useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import qrcodeTerminal from 'qrcode-terminal';
import QRCode from 'qrcode';
import crypto from 'node:crypto';
import http from 'node:http';

const {
  FLASK_URL = '',            // e.g. https://your-flask-service.up.railway.app
  BRIDGE_SECRET = '',        // same value as in the Flask service
  WHATSAPP_GROUP_ID = '',    // e.g. 120363012345678901@g.us  (leave empty on the first run to list groups)
  AUTH_DIR = '/data/auth',   // Railway volume mount: keeps the login across restarts (no new QR each time)
  PORT = '3000',
  QR_KEY = '',               // TEMPORARY: while set, https://<bridge-domain>/qr?key=<QR_KEY> shows the QR as a real image
  PAIRING_PHONE = '',        // OPTIONAL: number with country code, digits only (e.g. 919876543210) -> prints an 8-character
                             // pairing code to type into WhatsApp (Linked devices > Link with phone number instead)
} = process.env;

const DEBUG = process.env.DEBUG_MESSAGES === '1';   // temporary: logs where each incoming message came from (never its text)
const log = pino({ level: 'warn' });
const say = (...a) => console.log(new Date().toISOString(), ...a);
let sock = null;
let connected = false;
let lastQr = null;           // newest QR text, served at /qr while linking
let pairingRequested = false;

const toNum = (x) => (typeof x === 'number' ? x : (x?.toNumber?.() ?? Number(x)));
const digits = (jid) => (jid && jid.endsWith('@s.whatsapp.net') ? jid.split('@')[0].split(':')[0] : '');

async function post(path, body) {
  if (!FLASK_URL || !BRIDGE_SECRET) { say('FLASK_URL / BRIDGE_SECRET not set'); return false; }
  for (let i = 0; i < 2; i++) {
    try {
      const r = await fetch(FLASK_URL.replace(/\/$/, '') + path, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': BRIDGE_SECRET },
        body: JSON.stringify(body || {}),
      });
      if (r.ok) return true;
      say('Flask answered', r.status, 'for', path);
    } catch (e) { say('post failed:', e.message); }
    await new Promise((r) => setTimeout(r, 1500));
  }
  return false;
}

// Unwrap "ephemeral" / "view once" / "document with caption" wrappers
function unwrap(m) {
  let cur = m;
  for (let i = 0; i < 4 && cur; i++) {
    const inner = cur.ephemeralMessage?.message || cur.viewOnceMessage?.message
      || cur.viewOnceMessageV2?.message || cur.documentWithCaptionMessage?.message || cur.editedMessage?.message;
    if (!inner) break;
    cur = inner;
  }
  return cur || {};
}

const MEDIA_KEYS = ['imageMessage', 'videoMessage', 'audioMessage', 'documentMessage', 'stickerMessage',
  'ptvMessage', 'contactMessage', 'locationMessage', 'liveLocationMessage', 'pollCreationMessage'];

function handleUpsert({ messages, type }) {
  if (DEBUG) say('upsert type=' + type, 'count=' + messages.length);
  if (type !== 'notify') return;                                    // ignore history sync
  for (const msg of messages) {
    try {
      const key = msg.key || {};
      if (DEBUG) say('msg', 'jid=' + key.remoteJid, 'fromMe=' + key.fromMe, 'hasMessage=' + !!msg.message,
        'kinds=' + Object.keys(msg.message || {}).join('|'), 'stub=' + (msg.messageStubType ?? '-'),
        'matchesGroup=' + (key.remoteJid === WHATSAPP_GROUP_ID));
      if (!msg.message || key.fromMe || key.remoteJid !== WHATSAPP_GROUP_ID) continue;   // only our group, never ourselves
      const m = unwrap(msg.message);
      if (m.reactionMessage) continue;

      const pm = m.protocolMessage;
      const senderJid = key.participant || '';
      const base = {
        sender: msg.pushName || digits(senderJid) || digits(key.participantAlt) || senderJid,
        sender_jid: senderJid,
        sender_number: digits(senderJid) || digits(key.participantAlt),
      };
      if (pm) {                                                     // delete / edit of an earlier message
        const t = pm.type;
        const target = pm.key?.id;
        if (!target) continue;
        if (t === 0 || t === 'REVOKE') {
          post('/incoming', { event: 'delete', id: target, ts: Math.floor(Date.now() / 1000), ...base });
        } else if (t === 14 || t === 'MESSAGE_EDIT') {
          const em = unwrap(pm.editedMessage || {});
          const text = em.conversation || em.extendedTextMessage?.text || '';
          post('/incoming', { event: 'edit', id: target, text, ts: Math.floor(Date.now() / 1000), ...base });
        }
        continue;
      }
      if (MEDIA_KEYS.some((k) => m[k])) continue;                   // attachments: dropped here, never forwarded

      const ctx = m.extendedTextMessage?.contextInfo || {};
      const text = m.conversation || m.extendedTextMessage?.text || '';
      if (!text.trim()) continue;
      post('/incoming', {
        event: 'message', id: key.id, text, ts: toNum(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
        is_forwarded: !!ctx.isForwarded,
        is_reply: !!ctx.quotedMessage || (ctx.mentionedJid || []).length > 0,    // replies and tagged messages are ignored by Flask
        ...base,
      });
      // Handy for finding your own sender id (OWNER_JIDS) if WhatsApp hides numbers in groups:
      if (process.env.LOG_SENDERS === '1') say('sender:', base.sender, '| jid:', senderJid, '| alt:', key.participantAlt || '-');
    } catch (e) { say('message handling error:', e.message); }
  }
}

async function listGroups() {
  try {
    const groups = await sock.groupFetchAllParticipating();
    say('--- Groups this number is in (copy the id of yours into WHATSAPP_GROUP_ID) ---');
    for (const g of Object.values(groups)) say(`${g.id}   ${g.subject}   (${g.participants?.length} members)`);
    say('-----------------------------------------------------------------------------');
  } catch (e) { say('could not list groups:', e.message); }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try { ({ version } = await fetchLatestBaileysVersion()); } catch { /* use the library default */ }
  sock = makeWASocket({
    auth: state, version, logger: log, browser: Browsers.macOS('Desktop'),
    markOnlineOnConnect: false, syncFullHistory: false,
  });
  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('messages.upsert', handleUpsert);
  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      lastQr = qr;
      if (QR_KEY) say('QR ready: open  <this-bridge-domain>/qr?key=<your QR_KEY>  in a browser and scan the image.');
      else say('QR ready, but QR_KEY is not set, so no /qr page. Set QR_KEY (any long random value) or use PAIRING_PHONE.');
      if (PAIRING_PHONE && !pairingRequested && !sock.authState.creds.registered) {
        pairingRequested = true;
        try {
          const code = await sock.requestPairingCode(PAIRING_PHONE.replace(/\D/g, ''));
          say('PAIRING CODE:', code, ' -> WhatsApp > Linked devices > Link a device > "Link with phone number instead"');
        } catch (e) { pairingRequested = false; say('could not get a pairing code:', e.message); }
      }
    }
    if (connection === 'open') {
      connected = true; lastQr = null; say('WhatsApp connected');
      if (!WHATSAPP_GROUP_ID) await listGroups();
      else say('Watching group', WHATSAPP_GROUP_ID);
    }
    if (connection === 'close') {
      connected = false;
      pairingRequested = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      say('WhatsApp connection closed, code', code);
      if (code === DisconnectReason.loggedOut) {
        say('LOGGED OUT. Delete the auth folder (' + AUTH_DIR + ') and restart to scan a new QR. Not reconnecting.');
      } else { setTimeout(() => start().catch((e) => say('restart failed', e.message)), 3000); }
    }
  });
}

// ── heartbeat: tells Flask the bridge is alive and connected ──
setInterval(() => { if (connected) post('/heartbeat', {}); }, 60 * 1000);

// ── /send: Flask asks us to post a PDF to the group (one at a time, small random delay) ──
let sendChain = Promise.resolve();
function queueSend(job) {
  const run = sendChain.then(async () => {
    await new Promise((r) => setTimeout(r, 2000 + Math.random() * 4000));   // never in bursts
    await sock.sendMessage(WHATSAPP_GROUP_ID, {
      document: Buffer.from(job.data_b64, 'base64'), mimetype: 'application/pdf',
      fileName: job.filename || 'quotation.pdf', caption: job.caption || '',
    });
  });
  sendChain = run.catch(() => {});
  return run;
}

http.createServer((req, res) => {
  const reply = (code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)); };
  if (req.method === 'GET' && req.url === '/health') return reply(200, { ok: true, connected });
  if (req.method === 'GET' && req.url.startsWith('/qr')) {
    const key = new URL(req.url, 'http://x').searchParams.get('key') || '';
    const a = Buffer.from(key), b = Buffer.from(QR_KEY);
    if (!QR_KEY || a.length !== b.length || !crypto.timingSafeEqual(a, b)) return reply(403, { error: 'forbidden' });
    const page = (inner) => { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(
      '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="10">' +
      '<body style="font-family:system-ui;text-align:center;padding:16px">' + inner + '</body>'); };
    if (connected) return page('<h2>WhatsApp is connected. No QR needed.</h2><p>Remove the QR_KEY variable now.</p>');
    if (!lastQr) return page('<h2>Waiting for a QR code...</h2><p>This page refreshes by itself.</p>');
    QRCode.toString(lastQr, { type: 'svg', margin: 3, width: 360 }).then((svg) =>
      page('<h3>WhatsApp Business &gt; Linked devices &gt; Link a device</h3>' + svg +
           '<p>A new code appears every ~20 seconds; this page refreshes by itself. Do not share this page.</p>'));
    return undefined;
  }
  if (req.method === 'POST' && req.url === '/send') {
    if (!BRIDGE_SECRET || req.headers['x-bridge-secret'] !== BRIDGE_SECRET) return reply(403, { error: 'forbidden' });
    if (!connected || !WHATSAPP_GROUP_ID) return reply(503, { error: 'not connected or no group id' });
    let body = '';
    req.on('data', (c) => { body += c; if (body.length > 30e6) req.destroy(); });
    req.on('end', async () => {
      try { await queueSend(JSON.parse(body)); reply(200, { sent: true }); }
      catch (e) { say('send failed:', e.message); reply(500, { error: e.message }); }
    });
    return undefined;
  }
  return reply(404, { error: 'not found' });
}).listen(Number(PORT), () => say('bridge listening on', PORT));

process.on('unhandledRejection', (e) => say('unhandledRejection', e?.message || e));
start().catch((e) => { say('fatal start error', e); process.exit(1); });
