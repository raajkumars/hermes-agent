// choco local patch test: node --test owner_group_tag.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { extractBridgeEvent } from './bridge_helpers.js';
import { createOwnerGroupMediaStash, foldPendingMedia, ownerGroupTagMatch } from './owner_group_tag.js';

const RE = new RegExp('^\\s*@(paws|pari|kavya|divya)\\b', 'i');
const GROUP = '120363400000000000@g.us';
const BOT_ID = '15550000001:12@s.whatsapp.net';
const BOT_LID = '99990000001:12@lid';
const botIds = ['15550000001@s.whatsapp.net', '99990000001@lid'];

let n = 0;
function fakeMsg(message) {
  return { key: { remoteJid: GROUP, fromMe: true, id: `ID${++n}`, participant: '99990000001@lid' }, message, pushName: 'Owner' };
}

async function build(msg) {
  return extractBridgeEvent({
    msg, chatId: GROUP, senderId: '99990000001@lid', senderNumber: '9999', botIds, isGroup: true,
    downloadMedia: async () => Buffer.from('%PDF-1.3 fake'),
    writeMediaFile: async ({ prefix, ext, fileName }) => `/opt/data/cache/documents/${prefix}_test_${fileName || 'x' + ext}`,
    cacheDirs: { image: '/tmp/img', document: '/tmp/doc', audio: '/tmp/aud' },
  });
}

// Mirrors the bridge.js gate for fromMe group messages (bot mode + pattern set).
function gate({ event, msg, type = 'notify', stash }) {
  const tagMatch = ownerGroupTagMatch({ re: RE, event, msg, allowMention: type === 'notify' });
  const followTag = !tagMatch.tagged && type === 'notify' && event.hasMedia ? stash.followTag(GROUP) : '';
  if (tagMatch.tagged) {
    const pending = type === 'notify' ? stash.take(GROUP) : [];
    if (pending.length) foldPendingMedia(event, pending);
    if (type === 'notify') stash.openFollowWindow(GROUP, tagMatch.tag);
    return { forwarded: true, via: tagMatch.via, folded: pending.length };
  }
  if (followTag) { event.body = `${followTag} ${event.body || ''}`.trim(); return { forwarded: true, via: 'follow' }; }
  const held = type === 'notify' && stash.stash(GROUP, event);
  return { forwarded: false, reason: held ? 'from_me_group_untagged_media_held' : 'from_me_group_untagged' };
}

const doc = (extra = {}) => ({ url: 'https://mmg/x', mimetype: 'application/pdf', fileName: 'MASSACHUSETTS.pdf', ...extra });

test('INCIDENT replay: uncaptioned PDF, then "@paws ..." text 11 min later -> PDF folded into the tagged message', async () => {
  let t = 0; const stash = createOwnerGroupMediaStash({ now: () => t });
  const m1 = fakeMsg({ documentMessage: doc(), messageContextInfo: {} });
  const e1 = await build(m1);
  assert.equal(e1.body, '[document received]');
  assert.deepEqual(gate({ event: e1, msg: m1, stash }), { forwarded: false, reason: 'from_me_group_untagged_media_held' });
  t += 11 * 60 * 1000;
  const m2 = fakeMsg({ conversation: '@paws on Tuesday night ... Action Items: file insurance claims', messageContextInfo: {} });
  const e2 = await build(m2);
  assert.deepEqual(gate({ event: e2, msg: m2, stash }), { forwarded: true, via: 'body', folded: 1 });
  assert.equal(e2.hasMedia, true);
  assert.equal(e2.mediaType, 'document');
  assert.equal(e2.mime, 'application/pdf');
  assert.deepEqual(e2.mediaUrls, ['/opt/data/cache/documents/doc_test_MASSACHUSETTS.pdf']);
  assert.match(e2.body, /^@paws on Tuesday/);
  assert.match(e2.body, /Attached from the owner's previous message: document "MASSACHUSETTS.pdf"/);
  console.log('incident replay event:', JSON.stringify({ body: e2.body, hasMedia: e2.hasMedia, mediaType: e2.mediaType, mime: e2.mime, mediaUrls: e2.mediaUrls }));
});

test('held attachment expires after TTL and is not folded', async () => {
  let t = 0; const stash = createOwnerGroupMediaStash({ now: () => t });
  const m1 = fakeMsg({ documentMessage: doc() }); gate({ event: await build(m1), msg: m1, stash });
  t += 16 * 60 * 1000;
  const m2 = fakeMsg({ conversation: '@paws hi' }); const e2 = await build(m2);
  assert.deepEqual(gate({ event: e2, msg: m2, stash }), { forwarded: true, via: 'body', folded: 0 });
  assert.equal(e2.hasMedia, false);
});

test('captioned document (plain documentMessage.caption) is tagged', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ documentMessage: doc({ caption: '@paws file this claim' }) });
  assert.equal(gate({ event: await build(m), msg: m, stash }).forwarded, true);
});

test('documentWithCaptionMessage wrapper caption is tagged', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ documentWithCaptionMessage: { message: { documentMessage: doc({ caption: '@pari see attached' }) } } });
  const e = await build(m);
  assert.equal(e.body, '@pari see attached');
  assert.equal(gate({ event: e, msg: m, stash }).forwarded, true);
});

test('ephemeral image caption is tagged', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ ephemeralMessage: { message: { imageMessage: { url: 'u', mimetype: 'image/jpeg', caption: '  @Paws what is this?' } } } });
  const r = gate({ event: await build(m), msg: m, stash });
  assert.equal(r.forwarded, true);
});

test('real @-mention of the bot JID/LID (body "@<lid>") counts as a tag', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ imageMessage: { url: 'u', mimetype: 'image/jpeg', caption: 'look @99990000001', contextInfo: { mentionedJid: [BOT_LID.replace(':12', '')] } } });
  const e = await build(m);
  assert.deepEqual(gate({ event: e, msg: m, stash }), { forwarded: true, via: 'mention', folded: 0 });
});

test('mention of someone else is NOT a tag; untagged text is dropped', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ extendedTextMessage: { text: 'hi @12345', contextInfo: { mentionedJid: ['12345@lid'] } } });
  assert.deepEqual(gate({ event: await build(m), msg: m, stash }), { forwarded: false, reason: 'from_me_group_untagged' });
});

test('tag text followed by the attachment within 2 min -> attachment forwarded with the tag', async () => {
  let t = 0; const stash = createOwnerGroupMediaStash({ now: () => t });
  const m1 = fakeMsg({ conversation: '@paws here is the bill' }); gate({ event: await build(m1), msg: m1, stash });
  t += 30 * 1000;
  const m2 = fakeMsg({ documentMessage: doc() }); const e2 = await build(m2);
  assert.deepEqual(gate({ event: e2, msg: m2, stash }), { forwarded: true, via: 'follow' });
  assert.equal(e2.body, '@paws [document received]');
  t += 5 * 60 * 1000;
  const m3 = fakeMsg({ documentMessage: doc() }); const e3 = await build(m3);
  assert.equal(gate({ event: e3, msg: m3, stash }).forwarded, false);
});

test('LOOP SAFETY: quoting a bot reply that starts with @paws does not tag', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ extendedTextMessage: { text: 'ok thanks', contextInfo: { quotedMessage: { conversation: '@paws said hello' }, stanzaId: 'X' } } });
  assert.equal(gate({ event: await build(m), msg: m, stash }).forwarded, false);
});

test('LOOP SAFETY: append upserts (bot echoes) never hold media, follow, or tag via mention', async () => {
  const stash = createOwnerGroupMediaStash();
  const m0 = fakeMsg({ conversation: '@paws go' }); gate({ event: await build(m0), msg: m0, stash });
  const m1 = fakeMsg({ documentMessage: doc() });
  assert.deepEqual(gate({ event: await build(m1), msg: m1, type: 'append', stash }), { forwarded: false, reason: 'from_me_group_untagged' });
  const m2 = fakeMsg({ extendedTextMessage: { text: 'reply @99990000001', contextInfo: { mentionedJid: ['99990000001@lid'] } } });
  assert.equal(gate({ event: await build(m2), msg: m2, type: 'append', stash }).forwarded, false);
  assert.equal(stash.size(GROUP), 0);
});

test('bot reply text ("Paws here...", mid-text @paws) stays untagged by the anchored pattern', async () => {
  const stash = createOwnerGroupMediaStash();
  const m = fakeMsg({ extendedTextMessage: { text: 'Done! I filed it. — ask @paws anytime' } });
  assert.equal(gate({ event: await build(m), msg: m, stash }).forwarded, false);
});

test('quote-reply with tag to the held doc does not attach it twice', async () => {
  const stash = createOwnerGroupMediaStash();
  const e = { body: '@paws this one', hasMedia: false, mediaUrls: [], quotedMediaUrls: ['/c/doc_a.pdf'] };
  foldPendingMedia(e, [{ mediaType: 'document', mediaUrls: ['/c/doc_a.pdf'], body: '[document received]' }]);
  assert.equal(e.hasMedia, false);
  assert.equal(e.body, '@paws this one');
});
