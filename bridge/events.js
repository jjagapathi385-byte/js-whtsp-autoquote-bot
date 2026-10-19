// Pure helpers (no network): turn WhatsApp library objects into the small events the Flask app understands.
// Kept separate so they can be tested without WhatsApp.

export const STUB_REVOKE = 1;   // WebMessageInfo.StubType.REVOKE: a message was deleted

export const toNum = (x) => (typeof x === 'number' ? x : (x?.toNumber?.() ?? Number(x)));
export const digits = (jid) => (jid && jid.endsWith('@s.whatsapp.net') ? jid.split('@')[0].split(':')[0] : '');

// Unwrap "ephemeral" / "view once" / "document with caption" / "edited" wrappers
export function unwrap(m) {
  let cur = m;
  for (let i = 0; i < 4 && cur; i++) {
    const inner = cur.ephemeralMessage?.message || cur.viewOnceMessage?.message
      || cur.viewOnceMessageV2?.message || cur.documentWithCaptionMessage?.message || cur.editedMessage?.message;
    if (!inner) break;
    cur = inner;
  }
  return cur || {};
}

export const textOf = (m) => m.conversation || m.extendedTextMessage?.text || '';

export function senderInfo(key, pushName) {
  const senderJid = key.participant || '';
  return {
    sender: pushName || digits(senderJid) || digits(key.participantAlt) || senderJid,
    sender_jid: senderJid,
    sender_number: digits(senderJid) || digits(key.participantAlt),
  };
}

// In Baileys 7 an EDIT or a DELETE of an earlier message arrives as a `messages.update` event:
//   edit:   { key: {remoteJid, participant, id: <edited message id>}, update: { message: { editedMessage: { message } }, messageTimestamp } }
//   delete: { key: {..., id: <deleted message id>}, update: { message: null, messageStubType: 1 } }
// Returns the event for Flask, or null if it is not ours to forward.
export function updateToEvent(upd, groupId, nowSec) {
  const key = upd?.key || {};
  if (!groupId || key.remoteJid !== groupId || key.fromMe || !key.id) return null;
  const u = upd.update || {};
  const base = senderInfo(key, '');
  const edited = u.message?.editedMessage;
  if (edited) {
    const text = textOf(unwrap(edited.message || {}));
    if (!text.trim()) return null;
    return { event: 'edit', id: key.id, text, ts: toNum(u.messageTimestamp) || nowSec, ...base };
  }
  if (u.messageStubType === STUB_REVOKE || (u.message === null && u.messageStubType === STUB_REVOKE)) {
    return { event: 'delete', id: key.id, ts: nowSec, ...base };
  }
  return null;
}

// What we send to WhatsApp for a PDF. `mentions` (sender ids) make the "@name" in the caption a real tag.
export function buildSendPayload(job) {
  const payload = {
    document: Buffer.from(job.data_b64, 'base64'), mimetype: 'application/pdf',
    fileName: job.filename || 'quotation.pdf', caption: job.caption || '',
  };
  const mentions = (job.mentions || []).filter(Boolean);
  if (mentions.length) payload.mentions = mentions;
  return payload;
}

// Saved contact names ("KFC Supervisor Ravi") as the phone's address book gives them to a linked device.
// Looked up by every id form WhatsApp uses (phone number id, LID), device suffixes ignored.
export class ContactBook {
  constructor() { this.names = new Map(); }
  static key(id) { return (id || '').split('@')[0].split(':')[0]; }
  ingest(list) {
    let n = 0;
    for (const c of list || []) {
      const name = (c.name || '').trim();
      if (!name) continue;                                   // only SAVED names; push names are not used here
      for (const id of [c.id, c.lid, c.phoneNumber]) {
        const k = ContactBook.key(id);
        if (k) this.names.set(k, name);
      }
      n++;
    }
    return n;
  }
  lookup(...ids) {
    for (const id of ids) {
      const k = ContactBook.key(id);
      if (k && this.names.has(k)) return this.names.get(k);
    }
    return '';
  }
  load(obj) { for (const [k, v] of Object.entries(obj || {})) this.names.set(k, v); }
  toJSON() { return Object.fromEntries(this.names); }
  get size() { return this.names.size; }
}
