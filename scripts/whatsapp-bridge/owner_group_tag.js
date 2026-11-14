// choco local patch (reapply: /opt/data/patches/hermes/whatsapp-owner-group-tag.diff)
//
// Bot mode where the bridge is linked to the OWNER's own WhatsApp account: every
// group message the owner types is `fromMe`. WHATSAPP_OWNER_GROUP_TAG_PATTERN
// opts such messages in when they are addressed to an agent. This module decides
// "is this owner message tagged?" and keeps the owner's untagged attachments
// around briefly so they reach the agent with the tagged message that follows.
//
// Loop safety: the tag regex stays operator-anchored (e.g. ^\s*@(paws|pari)\b)
// and is only tested against the message's OWN text (quoted/contextInfo text is
// never scanned, so quoting a bot reply cannot tag). Attachment stashing and the
// follow-up window apply only to `notify` upserts (the owner's phone); the bot's
// own sends arrive as `append` and are also dropped via recentlySentIds.

const TEXT_KEYS = new Set(['caption', 'text', 'conversation']);

/** Own-message texts (body + captions in any wrapper), excluding quoted/contextInfo text. */
export function collectOwnTexts(message, depth = 0, out = []) {
  if (!message || typeof message !== 'object' || depth > 8) return out;
  for (const [key, value] of Object.entries(message)) {
    if (key === 'contextInfo' || key === 'quotedMessage' || key === 'messageContextInfo') continue;
    if (TEXT_KEYS.has(key) && typeof value === 'string' && value.trim()) out.push(value);
    else if (value && typeof value === 'object' && !ArrayBuffer.isView(value)) collectOwnTexts(value, depth + 1, out);
  }
  return out;
}

/**
 * Decide whether an owner-typed group message is addressed to an agent.
 * Tagged iff the operator regex matches the body or any own caption (including
 * documentWithCaptionMessage / ephemeral / viewOnce wrappers), or the message
 * carries a real @-mention of the bot's own JID/LID.
 */
export function ownerGroupTagMatch({ re, event, msg, allowMention = true }) {
  if (!re) return { tagged: false, via: null, tag: '' };
  const texts = [String(event?.body || ''), ...collectOwnTexts(msg?.message)];
  for (const text of texts) {
    const m = re.exec(text);
    if (m) return { tagged: true, via: text === texts[0] ? 'body' : 'caption', tag: m[0].trim() };
  }
  if (!allowMention) return { tagged: false, via: null, tag: '' };
  const botIds = new Set((event?.botIds || []).filter(Boolean));
  const mentioned = (event?.mentionedIds || []).filter((id) => botIds.has(id));
  if (mentioned.length) return { tagged: true, via: 'mention', tag: `@${mentioned[0].split('@')[0]}` };
  return { tagged: false, via: null, tag: '' };
}

/**
 * Per-chat memory of (a) the owner's recent UNTAGGED attachments and (b) a short
 * window after a tagged owner message during which the owner's next attachment is
 * forwarded too (covers "@paws see this" followed by the file).
 */
export function createOwnerGroupMediaStash({
  ttlMs = 15 * 60 * 1000,
  followWindowMs = 2 * 60 * 1000,
  maxPerChat = 10,
  now = () => Date.now(),
} = {}) {
  const pending = new Map(); // chatId -> [{ at, event }]
  const follow = new Map(); // chatId -> { until, tag }

  function fresh(chatId) {
    const items = (pending.get(chatId) || []).filter((it) => now() - it.at <= ttlMs);
    if (items.length) pending.set(chatId, items); else pending.delete(chatId);
    return items;
  }

  return {
    stash(chatId, event) {
      // Failed / too-large downloads are held too (no file, but the note must reach the agent).
      if (!event?.hasMedia) return false;
      const items = fresh(chatId);
      items.push({ at: now(), event });
      pending.set(chatId, items.slice(-maxPerChat));
      return true;
    },
    take(chatId) {
      const items = fresh(chatId);
      pending.delete(chatId);
      return items.map((it) => it.event);
    },
    size(chatId) { return fresh(chatId).length; },
    openFollowWindow(chatId, tag) {
      if (tag) follow.set(chatId, { until: now() + followWindowMs, tag });
    },
    followTag(chatId) {
      const f = follow.get(chatId);
      if (!f) return '';
      if (now() > f.until) { follow.delete(chatId); return ''; }
      return f.tag;
    },
  };
}

function isPlaceholder(body, mediaType) {
  const b = String(body || '').trim();
  return !b || b === `[${mediaType} received]`;
}

/**
 * Fold earlier untagged owner attachments into the tagged event (mutates and
 * returns it). The adapter classifies one media kind per event, so only
 * attachments of the event's kind (or, for a text event, the first pending
 * attachment's kind) are attached; the rest are listed by path in the text.
 */
export function foldPendingMedia(event, pendingEvents) {
  // A quote-reply to the held attachment already carries it as quoted media.
  const quoted = new Set(event.quotedMediaUrls || []);
  pendingEvents = (pendingEvents || []).filter((p) => !(p.mediaUrls?.length && p.mediaUrls.every((u) => quoted.has(u))));
  if (!pendingEvents.length) return event;
  // Attachments that never downloaded (too large / failed): surface their note, no file.
  const missing = pendingEvents.filter((p) => !p.mediaUrls?.length);
  pendingEvents = pendingEvents.filter((p) => p.mediaUrls?.length);
  const missingNotes = missing.map((p) => `[Owner's previous message had an attachment that did not arrive: ${p.body}]`);
  if (!pendingEvents.length) {
    event.body = [String(event.body || ''), ...missingNotes].filter(Boolean).join('\n');
    return event;
  }
  const kind = event.hasMedia ? event.mediaType : pendingEvents[0].mediaType;
  const same = pendingEvents.filter((p) => p.mediaType === kind);
  const other = pendingEvents.filter((p) => p.mediaType !== kind);
  const notes = [];
  for (const p of same) {
    const cap = isPlaceholder(p.body, p.mediaType) ? '' : ` (caption: ${p.body})`;
    notes.push(`[Attached from the owner's previous message: ${p.mediaType}${p.fileName ? ` "${p.fileName}"` : ''}${cap}]`);
  }
  for (const p of other) {
    notes.push(`[Owner also sent earlier (${p.mediaType}${p.fileName ? ` "${p.fileName}"` : ''}) saved at: ${p.mediaUrls.join(', ')}]`);
  }
  const mimes = new Set([event.hasMedia ? event.mime : null, ...same.map((p) => p.mime)].filter(Boolean));
  event.mediaUrls = [...same.flatMap((p) => p.mediaUrls), ...(event.mediaUrls || [])];
  event.hasMedia = event.mediaUrls.length > 0;
  event.mediaType = kind;
  event.mime = mimes.size === 1 ? [...mimes][0] : '';
  if (!event.fileName && same.length) event.fileName = same[0].fileName || '';
  event.body = [String(event.body || ''), ...notes, ...missingNotes].filter(Boolean).join('\n');
  return event;
}
