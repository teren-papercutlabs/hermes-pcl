#!/usr/bin/env node
/**
 * Hermes Agent WhatsApp Bridge
 *
 * Standalone Node.js process that connects to WhatsApp via Baileys
 * and exposes HTTP endpoints for the Python gateway adapter.
 *
 * Endpoints (matches gateway/platforms/whatsapp.py expectations):
 *   GET  /messages       - Long-poll for new incoming messages
 *   POST /send           - Send a message { chatId, message, replyTo? }
 *   POST /edit           - Edit a sent message { chatId, messageId, message }
 *   POST /send-media     - Send media natively { chatId, filePath, mediaType?, caption?, fileName? }
 *   POST /typing         - Send typing indicator { chatId }
 *   POST /create-group   - Create a WhatsApp group { subject, participants }
 *   POST /leave-group    - Leave a WhatsApp group { chatId, expectedSubject?, removeParticipants? }
 *   GET  /chat/:id       - Get chat info
 *   GET  /health         - Health check
 *
 * Usage:
 *   node bridge.js --port 3000 --session ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { appendFileSync, mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, unlinkSync } from 'fs';
import { randomBytes } from 'crypto';
import { execSync } from 'child_process';
import { tmpdir } from 'os';
import QRCode from 'qrcode';
import qrcode from 'qrcode-terminal';
import { matchesAllowedChat, matchesAllowedUser, parseAllowedUsers, parseIdentifierList } from './allowlist.js';

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_DEBUG === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_DEBUG.toLowerCase());
const SYNC_FULL_HISTORY =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_SYNC_FULL_HISTORY === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_SYNC_FULL_HISTORY.toLowerCase());
const HISTORY_TO_LIVE_QUEUE =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_HISTORY_TO_LIVE_QUEUE === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_HISTORY_TO_LIVE_QUEUE.toLowerCase());

const PORT = parseInt(getArg('port', '3000'), 10);
const SESSION_DIR = getArg('session', path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'session'));
const IMAGE_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'image_cache');
const DOCUMENT_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'document_cache');
const AUDIO_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'audio_cache');
const HISTORY_SYNC_PATH = process.env.WHATSAPP_HISTORY_SYNC_PATH ||
  path.join(path.dirname(SESSION_DIR), 'history-sync.jsonl');
const HISTORY_METADATA_PATH = process.env.WHATSAPP_HISTORY_METADATA_PATH ||
  path.join(path.dirname(SESSION_DIR), 'history-metadata.jsonl');
const STORE_FILE = process.env.WHATSAPP_STORE_FILE ||
  path.join(path.dirname(SESSION_DIR), 'message-store.json');
const HERMES_HOME = process.env.HERMES_HOME || path.dirname(path.dirname(SESSION_DIR));
const CHAT_METADATA_FILE = process.env.WHATSAPP_CHAT_METADATA_FILE ||
  path.join(HERMES_HOME, 'whatsapp-chat-metadata.json');
const STORE_WRITE_INTERVAL_MS = parseInt(process.env.WHATSAPP_STORE_WRITE_INTERVAL_MS || '10000', 10);
const MAX_STORE_MESSAGES = parseInt(process.env.WHATSAPP_MAX_STORE_MESSAGES || '20000', 10);
const QR_FILE = process.env.WHATSAPP_QR_FILE || '';
const PAIR_ONLY = args.includes('--pair-only');
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat'); // "bot" or "self-chat"
const OUTBOUND_DISABLED =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_OUTBOUND_DISABLED === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_OUTBOUND_DISABLED.toLowerCase());
const GROUP_CREATE_ENABLED =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_GROUP_CREATE_ENABLED === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_GROUP_CREATE_ENABLED.toLowerCase());
const GROUP_CREATE_MAX_PARTICIPANTS = parseInt(process.env.WHATSAPP_GROUP_CREATE_MAX_PARTICIPANTS || '10', 10);
const OUTBOUND_ALLOWED_CHATS_RAW =
  process.env.WHATSAPP_OUTBOUND_ALLOWED_CHATS ??
  process.env.WHATSAPP_OUTBOUND_ALLOW_CHATS;
const OUTBOUND_CHAT_FILTER_CONFIGURED = OUTBOUND_ALLOWED_CHATS_RAW !== undefined;
const OUTBOUND_ALLOWED_CHATS = parseIdentifierList(OUTBOUND_ALLOWED_CHATS_RAW || '');
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');
const DEFAULT_REPLY_PREFIX = '⚕ *Hermes Agent*\n────────────\n';
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX === undefined
  ? DEFAULT_REPLY_PREFIX
  : process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n');
const MAX_MESSAGE_LENGTH = parseInt(process.env.WHATSAPP_MAX_MESSAGE_LENGTH || '4096', 10);
const CHUNK_DELAY_MS = parseInt(process.env.WHATSAPP_CHUNK_DELAY_MS || '300', 10);
// Per-call timeout for sock.sendMessage(). Baileys occasionally hangs forever
// when uploading media to WhatsApp servers (and, less often, on text sends),
// which pins the bridge's HTTP handler until the upstream aiohttp timeout
// fires. Fail fast instead so the gateway can surface a real error and retry.
const SEND_TIMEOUT_MS = parseInt(process.env.WHATSAPP_SEND_TIMEOUT_MS || '60000', 10);

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function sendWithTimeout(chatId, payload, options = {}, timeoutMs = SEND_TIMEOUT_MS) {
  let timer;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`sendMessage timed out after ${timeoutMs / 1000}s`)),
      timeoutMs,
    );
  });
  return Promise.race([sock.sendMessage(chatId, payload, options), timeoutPromise])
    .finally(() => clearTimeout(timer));
}

function formatOutgoingMessage(message) {
  // In bot mode, messages come from a different number so the prefix is
  // redundant — the sender identity is already clear.  Only prepend in
  // self-chat mode where bot and user share the same number.
  if (WHATSAPP_MODE !== 'self-chat') return message;
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function splitLongMessage(message, maxLength = MAX_MESSAGE_LENGTH) {
  const text = String(message || '');
  if (!text) return [];
  if (!Number.isFinite(maxLength) || maxLength < 1 || text.length <= maxLength) {
    return [text];
  }

  const chunks = [];
  let remaining = text;
  while (remaining.length > maxLength) {
    let splitAt = remaining.lastIndexOf('\n', maxLength);
    if (splitAt < Math.floor(maxLength / 2)) {
      splitAt = remaining.lastIndexOf(' ', maxLength);
    }
    if (splitAt < 1) splitAt = maxLength;

    chunks.push(remaining.slice(0, splitAt).trimEnd());
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

function outboundPolicyDecision(chatId) {
  if (OUTBOUND_DISABLED) {
    return { allowed: false, reason: 'global_disabled' };
  }
  if (!OUTBOUND_CHAT_FILTER_CONFIGURED) {
    return { allowed: true, reason: 'legacy_open' };
  }
  if (matchesAllowedChat(chatId, OUTBOUND_ALLOWED_CHATS, SESSION_DIR)) {
    return { allowed: true, reason: 'explicitly_allowed' };
  }
  return { allowed: false, reason: 'not_in_outbound_allowlist' };
}

function rejectWhenOutboundBlocked(res, action, chatId) {
  const decision = outboundPolicyDecision(chatId);
  if (decision.allowed) return false;
  res.status(403).json({
    error: `WhatsApp outbound blocked: ${action}`,
    chatId,
    reason: decision.reason,
  });
  return true;
}

function rejectWhenGroupAdminBlocked(res, action) {
  if (OUTBOUND_DISABLED) {
    res.status(403).json({
      error: `WhatsApp outbound blocked: ${action}`,
      reason: 'global_disabled',
    });
    return true;
  }
  if (!GROUP_CREATE_ENABLED) {
    res.status(403).json({
      error: `WhatsApp group admin blocked: ${action}`,
      reason: 'group_create_not_enabled',
    });
    return true;
  }
  return false;
}

function appendJsonLine(filePath, payload, label) {
  try {
    appendFileSync(filePath, `${JSON.stringify(payload)}\n`);
    return true;
  } catch (err) {
    console.error(`[bridge] Failed to write ${label}:`, err.message);
    return false;
  }
}

const chatMetadata = new Map();
let chatMetadataDirty = false;
const groupMetadataInflight = new Map();

function chatIdNumber(chatId) {
  return String(chatId || '').split('@', 1)[0];
}

function isNumericChatName(chatId, name) {
  const clean = String(name || '').trim();
  if (!clean) return true;
  return clean === chatIdNumber(chatId) || /^\d{8,}$/.test(clean);
}

function firstTextValue(obj, keys) {
  if (!obj || typeof obj !== 'object') return '';
  for (const key of keys) {
    const value = obj[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function loadChatMetadata() {
  try {
    if (!existsSync(CHAT_METADATA_FILE)) return;
    const parsed = JSON.parse(readFileSync(CHAT_METADATA_FILE, 'utf8'));
    const chats = parsed?.chats || {};
    for (const [id, entry] of Object.entries(chats)) {
      if (entry && typeof entry === 'object') {
        chatMetadata.set(id, entry);
      }
    }
    console.log(`[bridge-v2] Loaded chat metadata: ${CHAT_METADATA_FILE}`);
  } catch (err) {
    console.warn('[bridge-v2] Failed to load chat metadata:', err.message);
  }
}

function flushChatMetadata() {
  if (!chatMetadataDirty) return;
  try {
    mkdirSync(path.dirname(CHAT_METADATA_FILE), { recursive: true });
    const chats = {};
    for (const [id, entry] of chatMetadata.entries()) chats[id] = entry;
    writeFileSync(CHAT_METADATA_FILE, JSON.stringify({
      version: 1,
      updatedAt: new Date().toISOString(),
      chats,
    }, null, 2));
    chatMetadataDirty = false;
  } catch (err) {
    console.warn('[bridge-v2] Failed to write chat metadata:', err.message);
  }
}

function rememberChatMetadata(chatId, patch, source = 'unknown') {
  if (!chatId) return null;
  const current = chatMetadata.get(chatId) || { id: chatId };
  const candidateName = patch?.name || patch?.subject;
  const next = {
    ...current,
    id: chatId,
    type: patch?.type || current.type || (chatId.endsWith('@g.us') ? 'group' : 'dm'),
    updatedAt: new Date().toISOString(),
  };
  if (candidateName && !isNumericChatName(chatId, candidateName)) {
    next.name = String(candidateName).trim();
    if (patch?.subject) next.subject = String(patch.subject).trim();
  } else if (!next.name && candidateName) {
    next.name = String(candidateName).trim();
  }
  if (patch?.participants) next.participants = patch.participants;
  next.sources = Array.from(new Set([...(current.sources || []), source]));
  chatMetadata.set(chatId, next);
  chatMetadataDirty = true;
  return next;
}

function rememberHistoryChat(chat) {
  const chatId = chat?.id || chat?.jid || chat?.remoteJid || chat?.key?.remoteJid;
  if (!chatId) return;
  const name = firstTextValue(chat, ['subject', 'name', 'displayName', 'verifiedName', 'pushName', 'notify']);
  rememberChatMetadata(chatId, {
    name,
    subject: chat.subject,
    type: chatId.endsWith('@g.us') ? 'group' : 'dm',
  }, 'history');
}

async function getGroupChatMetadata(chatId) {
  const cached = chatMetadata.get(chatId);
  if (cached?.name && !isNumericChatName(chatId, cached.name)) {
    return cached;
  }
  if (!sock || !chatId?.endsWith('@g.us')) return cached || null;
  if (groupMetadataInflight.has(chatId)) {
    return groupMetadataInflight.get(chatId);
  }
  const promise = sock.groupMetadata(chatId)
    .then((metadata) => rememberChatMetadata(chatId, {
      name: metadata?.subject,
      subject: metadata?.subject,
      type: 'group',
      participants: (metadata?.participants || []).map(p => p.id),
    }, 'groupMetadata'))
    .catch(() => cached || null)
    .finally(() => groupMetadataInflight.delete(chatId));
  groupMetadataInflight.set(chatId, promise);
  return promise;
}

function resolveChatName(chatId, msg, isGroup, senderNumber, groupMeta) {
  if (isGroup) {
    const cached = groupMeta || chatMetadata.get(chatId);
    if (cached?.name && !isNumericChatName(chatId, cached.name)) return cached.name;
    return chatIdNumber(chatId);
  }
  const name = msg.pushName || senderNumber;
  rememberChatMetadata(chatId, { name, type: 'dm' }, 'message');
  return name;
}

loadChatMetadata();
setInterval(flushChatMetadata, 10000).unref();

function trackSentMessageId(sent) {
  if (sent?.key?.id) {
    recentlySentIds.add(sent.key.id);
    if (recentlySentIds.size > MAX_RECENT_IDS) {
      recentlySentIds.delete(recentlySentIds.values().next().value);
    }
  }
}

function normalizeWhatsAppId(value) {
  if (!value) return '';
  return String(value).trim().replace(/:.*@/, '@');
}

function normalizeParticipantJid(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const compact = raw.replace(/[\s()-]/g, '');
  const jid = compact.replace(/:.*@/, '@').toLowerCase();
  if (/^\d+@s\.whatsapp\.net$/.test(jid) || /^\d+@lid$/.test(jid)) {
    return jid;
  }
  const phone = compact.replace(/^\+/, '');
  if (/^\d{8,20}$/.test(phone)) {
    return `${phone}@s.whatsapp.net`;
  }
  return '';
}

function normalizeGroupJid(value) {
  const jid = String(value || '').trim().replace(/:.*@/, '@').toLowerCase();
  return /^\d+@g\.us$/.test(jid) ? jid : '';
}

function getMessageContent(msg) {
  const content = msg?.message || {};
  if (content.ephemeralMessage?.message) return content.ephemeralMessage.message;
  if (content.viewOnceMessage?.message) return content.viewOnceMessage.message;
  if (content.viewOnceMessageV2?.message) return content.viewOnceMessageV2.message;
  if (content.documentWithCaptionMessage?.message) return content.documentWithCaptionMessage.message;
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

function getContextInfo(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return {};
  for (const value of Object.values(messageContent)) {
    if (value && typeof value === 'object' && value.contextInfo) {
      return value.contextInfo;
    }
  }
  return {};
}

function textFromMessageContent(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return '';
  if (messageContent.conversation) return String(messageContent.conversation);
  if (messageContent.extendedTextMessage?.text) return String(messageContent.extendedTextMessage.text);
  if (messageContent.imageMessage?.caption) return String(messageContent.imageMessage.caption);
  if (messageContent.videoMessage?.caption) return String(messageContent.videoMessage.caption);
  if (messageContent.documentMessage?.caption) return String(messageContent.documentMessage.caption);
  if (messageContent.documentWithCaptionMessage?.message) {
    return textFromMessageContent(getMessageContent({ message: messageContent.documentWithCaptionMessage.message }));
  }
  if (messageContent.audioMessage || messageContent.pttMessage) return '[voice message]';
  if (messageContent.imageMessage) return '[image]';
  if (messageContent.videoMessage) return '[video]';
  if (messageContent.documentMessage) return '[document]';
  return '';
}

mkdirSync(SESSION_DIR, { recursive: true });

// Build LID → phone reverse map from session files (lid-mapping-{phone}.json)
function buildLidMap() {
  const map = {};
  try {
    for (const f of readdirSync(SESSION_DIR)) {
      const m = f.match(/^lid-mapping-(\d+)\.json$/);
      if (!m) continue;
      const phone = m[1];
      const lid = JSON.parse(readFileSync(path.join(SESSION_DIR, f), 'utf8'));
      if (lid) map[String(lid)] = phone;
    }
  } catch {}
  return map;
}
let lidToPhone = buildLidMap();

const logger = pino({ level: 'warn' });
const messageStore = new Map();
let messageStoreDirty = false;

try {
  if (existsSync(STORE_FILE)) {
    const parsed = JSON.parse(readFileSync(STORE_FILE, 'utf8'));
    const entries = Array.isArray(parsed?.messages) ? parsed.messages : [];
    for (const entry of entries) {
      if (entry?.key && entry?.message) {
        messageStore.set(entry.key, entry);
      }
    }
    console.log(`[bridge-v2] Loaded message store: ${STORE_FILE}`);
  }
} catch (err) {
  console.warn('[bridge-v2] Failed to load message store:', err.message);
}

function messageStoreKey(key) {
  if (!key?.remoteJid || !key?.id) return '';
  return `${key.remoteJid}::${key.id}`;
}

function rememberMessage(msg) {
  const key = messageStoreKey(msg?.key);
  if (!key || !msg?.message) return;
  messageStore.set(key, {
    key,
    remoteJid: msg.key.remoteJid,
    id: msg.key.id,
    fromMe: !!msg.key.fromMe,
    participant: msg.key.participant || null,
    message: msg.message,
    messageTimestamp: msg.messageTimestamp || null,
    storedAt: new Date().toISOString(),
  });
  while (messageStore.size > MAX_STORE_MESSAGES) {
    messageStore.delete(messageStore.keys().next().value);
  }
  messageStoreDirty = true;
}

function flushStore() {
  if (!messageStoreDirty) return;
  try {
    mkdirSync(path.dirname(STORE_FILE), { recursive: true });
    writeFileSync(STORE_FILE, JSON.stringify({
      version: 1,
      updatedAt: new Date().toISOString(),
      messages: Array.from(messageStore.values()),
    }));
    messageStoreDirty = false;
  } catch (err) {
    console.warn('[bridge-v2] Failed to write message store:', err.message);
  }
}

setInterval(flushStore, STORE_WRITE_INTERVAL_MS).unref();

// Message queue for polling
const messageQueue = [];
const MAX_QUEUE_SIZE = parseInt(
  process.env.WHATSAPP_MAX_QUEUE_SIZE || (SYNC_FULL_HISTORY ? '5000' : '100'),
  10,
);

// Track recently sent message IDs to prevent echo-back loops with media
const recentlySentIds = new Set();
const MAX_RECENT_IDS = 50;
const recentInboundMessages = new Map();
const MAX_RECENT_INBOUND_MESSAGES = parseInt(process.env.WHATSAPP_RECENT_INBOUND_MESSAGES || '500', 10);

function rememberInboundMessage(msg) {
  const messageId = msg?.key?.id;
  if (!messageId || msg?.key?.fromMe) return;
  recentInboundMessages.set(messageId, msg);
  while (recentInboundMessages.size > MAX_RECENT_INBOUND_MESSAGES) {
    recentInboundMessages.delete(recentInboundMessages.keys().next().value);
  }
}

function sendOptionsForReplyTo(replyTo) {
  if (!replyTo) return {};
  const quoted = recentInboundMessages.get(String(replyTo));
  return quoted ? { quoted } : {};
}

let sock = null;
let connectionState = 'disconnected';
let latestQr = '';
let latestQrAt = null;
let lastDisconnectReason = null;
let connectedAt = null;
let socketStartedAt = null;

async function startSocket() {
  socketStartedAt = new Date().toISOString();
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Papercut Agents', 'Desktop', '1.0'],
    syncFullHistory: SYNC_FULL_HISTORY,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      const stored = messageStore.get(messageStoreKey(key));
      return stored?.message || { conversation: '' };
    },
  });

  sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); flushStore(); });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      latestQr = qr;
      latestQrAt = new Date().toISOString();
      console.log('\n📱 Scan this QR code with WhatsApp on your phone:\n');
      if (QR_FILE) {
        try {
          writeFileSync(QR_FILE, qr);
          console.log(`QR_FILE=${QR_FILE}`);
        } catch (err) {
          console.error('[bridge] Failed to write QR file:', err.message);
        }
      }
      qrcode.generate(qr, { small: true });
      console.log('\nWaiting for scan...\n');
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';
      lastDisconnectReason = reason || null;
      connectedAt = null;

      if (reason === DisconnectReason.loggedOut) {
        console.log('❌ Logged out. Delete session and restart to re-authenticate.');
        process.exit(1);
      } else {
        // 515 = restart requested (common after pairing). Always reconnect.
        if (reason === 515) {
          console.log('↻ WhatsApp requested restart (code 515). Reconnecting...');
        } else {
          console.log(`⚠️  Connection closed (reason: ${reason}). Reconnecting in 3s...`);
        }
        setTimeout(startSocket, reason === 515 ? 1000 : 3000);
      }
    } else if (connection === 'open') {
      connectionState = 'connected';
      connectedAt = new Date().toISOString();
      latestQr = '';
      latestQrAt = null;
      lastDisconnectReason = null;
      console.log('✅ WhatsApp connected!');
      if (PAIR_ONLY) {
        console.log('✅ Pairing complete. Credentials saved.');
        // Give Baileys a moment to flush creds, then exit cleanly
        setTimeout(() => process.exit(0), 2000);
      }
    }
  });

  async function enqueueMessages(messages, type, historyMeta = {}) {
    // In self-chat mode, your own messages commonly arrive as 'append' rather
    // than 'notify'. Accept both and filter agent echo-backs below.
    const isHistory = type === 'history';
    if (!isHistory && type !== 'notify' && type !== 'append') return;

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      if (!msg.message) continue;

      const chatId = msg.key.remoteJid;
      if (chatId?.includes('status')) continue;
      if (WHATSAPP_DEBUG) {
        try {
          console.log(JSON.stringify({
            event: isHistory ? 'history_message' : 'upsert', type,
            fromMe: !!msg.key.fromMe, chatId,
            senderId: msg.key.participant || chatId,
            messageKeys: Object.keys(msg.message || {}),
          }));
        } catch {}
      }
      const senderId = msg.key.participant || chatId;
      const isGroup = chatId.endsWith('@g.us');
      const senderNumber = senderId.replace(/@.*/, '');

      // Handle fromMe messages based on mode
      if (msg.key.fromMe && !isHistory) {
        if (isGroup) continue;
        if (WHATSAPP_MODE === 'bot') {
          // Bot mode: separate number. ALL fromMe are echo-backs of our own replies — skip.
          continue;
        }

        // Self-chat mode: only allow messages in the user's own self-chat
        // WhatsApp now uses LID (Linked Identity Device) format: 67427329167522@lid
        // AND classic format: 34652029134@s.whatsapp.net
        // sock.user has both: { id: "number:10@s.whatsapp.net", lid: "lid_number:10@lid" }
        const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const chatNumber = chatId.replace(/@.*/, '');
        const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
        if (!isSelfChat) continue;
      }

      // Handle !fromMe messages (from other people) based on mode.
      // Self-chat mode only responds to the user's own messages to
      // themselves — stranger DMs / group pings must never reach the
      // Python gateway, otherwise a pairing-code reply fires in response
      // to arbitrary incoming messages (#8389).
      if (!msg.key.fromMe) {
        if (WHATSAPP_MODE === 'self-chat') {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'self_chat_mode_rejects_non_self',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
        if (!matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'allowlist_mismatch',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
      }

      const messageContent = getMessageContent(msg);
      const contextInfo = getContextInfo(messageContent);
      const mentionedIds = Array.from(new Set((contextInfo?.mentionedJid || []).map(normalizeWhatsAppId).filter(Boolean)));
      const quotedMessageId = contextInfo?.stanzaId || null;
      const quotedParticipant = normalizeWhatsAppId(contextInfo?.participant || '') || null;
      const quotedRemoteJid = normalizeWhatsAppId(contextInfo?.remoteJid || '') || null;
      const hasQuotedMessage = !!contextInfo?.quotedMessage;
      const quotedText = hasQuotedMessage
        ? textFromMessageContent(getMessageContent({ message: contextInfo.quotedMessage }))
        : '';
      const quotedFromBot = quotedMessageId ? recentlySentIds.has(quotedMessageId) : false;

      // Extract message body
      let body = '';
      let hasMedia = false;
      let mediaType = '';
      const mediaUrls = [];

      if (messageContent.conversation) {
        body = messageContent.conversation;
      } else if (messageContent.extendedTextMessage?.text) {
        body = messageContent.extendedTextMessage.text;
      } else if (messageContent.imageMessage) {
        body = messageContent.imageMessage.caption || '';
        hasMedia = true;
        mediaType = 'image';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.imageMessage.mimetype || 'image/jpeg';
          const extMap = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif' };
          const ext = extMap[mime] || '.jpg';
          mkdirSync(IMAGE_CACHE_DIR, { recursive: true });
          const filePath = path.join(IMAGE_CACHE_DIR, `img_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download image:', err.message);
        }
      } else if (messageContent.videoMessage) {
        body = messageContent.videoMessage.caption || '';
        hasMedia = true;
        mediaType = 'video';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.videoMessage.mimetype || 'video/mp4';
          const ext = mime.includes('mp4') ? '.mp4' : '.mkv';
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const filePath = path.join(DOCUMENT_CACHE_DIR, `vid_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download video:', err.message);
        }
      } else if (messageContent.audioMessage || messageContent.pttMessage) {
        hasMedia = true;
        mediaType = messageContent.pttMessage ? 'ptt' : 'audio';
        try {
          const audioMsg = messageContent.pttMessage || messageContent.audioMessage;
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = audioMsg.mimetype || 'audio/ogg';
          const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.m4a' : '.ogg';
          mkdirSync(AUDIO_CACHE_DIR, { recursive: true });
          const filePath = path.join(AUDIO_CACHE_DIR, `aud_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download audio:', err.message);
        }
      } else if (messageContent.documentMessage) {
        body = messageContent.documentMessage.caption || '';
        hasMedia = true;
        mediaType = 'document';
        const fileName = messageContent.documentMessage.fileName || 'document';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const safeFileName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
          const filePath = path.join(DOCUMENT_CACHE_DIR, `doc_${randomBytes(6).toString('hex')}_${safeFileName}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download document:', err.message);
        }
      }

      // For media without caption, use a placeholder so the API message is never empty
      if (hasMedia && !body) {
        body = `[${mediaType} received]`;
      }

      // Ignore Hermes' own reply messages in self-chat mode to avoid loops.
      if (msg.key.fromMe && ((REPLY_PREFIX && body.startsWith(REPLY_PREFIX)) || recentlySentIds.has(msg.key.id))) {
        if (WHATSAPP_DEBUG) {
          try { console.log(JSON.stringify({ event: 'ignored', reason: 'agent_echo', chatId, messageId: msg.key.id })); } catch {}
        }
        continue;
      }

      // Skip empty messages
      if (!body && !hasMedia) {
        if (WHATSAPP_DEBUG) {
          try { 
            console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) })); 
          } catch (err) {
            console.error('Failed to log empty message event:', err);
          }
        }
        continue;
      }

      const event = {
        messageId: msg.key.id,
        chatId,
        senderId,
        senderName: msg.pushName || senderNumber,
        chatName: resolveChatName(
          chatId,
          msg,
          isGroup,
          senderNumber,
          isGroup ? await getGroupChatMetadata(chatId) : null,
        ),
        isGroup,
        body,
        hasMedia,
        mediaType,
        mediaUrls,
        mentionedIds,
        quotedMessageId,
        quotedParticipant,
        quotedRemoteJid,
        hasQuotedMessage,
        quotedText,
        quotedFromBot,
    botIds,
    timestamp: msg.messageTimestamp,
    fromMe: !!msg.key.fromMe,
    historySync: isHistory,
    historySyncType: historyMeta.syncType || null,
        historyIsLatest: historyMeta.isLatest ?? null,
      };
      rememberInboundMessage(msg);

      if (isHistory) {
        appendJsonLine(HISTORY_SYNC_PATH, event, 'history sync event');
        if (!HISTORY_TO_LIVE_QUEUE) {
          continue;
        }
      }

      messageQueue.push(event);
      if (messageQueue.length > MAX_QUEUE_SIZE) {
        messageQueue.shift();
      }
    }
  }

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const msg of messages || []) rememberMessage(msg);
    await enqueueMessages(messages || [], type);
  });

  sock.ev.on('messaging-history.set', async ({
    chats = [],
    contacts = [],
    messages = [],
    syncType,
    isLatest,
  }) => {
    appendJsonLine(HISTORY_METADATA_PATH, {
      recordType: 'history_batch',
      capturedAt: new Date().toISOString(),
      syncType,
      isLatest,
      chatCount: chats.length,
      contactCount: contacts.length,
      messageCount: messages.length,
    }, 'history batch metadata');
    for (const chat of chats) {
      rememberHistoryChat(chat);
      appendJsonLine(HISTORY_METADATA_PATH, {
        recordType: 'chat',
        capturedAt: new Date().toISOString(),
        syncType,
        isLatest,
        chat,
      }, 'history chat metadata');
    }
    for (const contact of contacts) {
      appendJsonLine(HISTORY_METADATA_PATH, {
        recordType: 'contact',
        capturedAt: new Date().toISOString(),
        syncType,
        isLatest,
        contact,
      }, 'history contact metadata');
    }
    console.log(JSON.stringify({
      event: 'history_sync',
      chats: chats.length,
      contacts: contacts.length,
      messages: messages.length,
      syncType,
      isLatest,
    }));
    for (const msg of messages || []) rememberMessage(msg);
    await enqueueMessages(messages || [], 'history', { syncType, isLatest });
  });
}

// HTTP server
const app = express();
app.use(express.json());

// Host-header validation — defends against DNS rebinding.
// The bridge binds loopback-only (127.0.0.1) but a victim browser on
// the same machine could be tricked into fetching from an attacker
// hostname that TTL-flips to 127.0.0.1. Reject any request whose Host
// header doesn't resolve to a loopback alias.
// See GHSA-ppp5-vxwm-4cf7.
const _ACCEPTED_HOST_VALUES = new Set([
  'localhost',
  '127.0.0.1',
  '[::1]',
  '::1',
]);

app.use((req, res, next) => {
  const raw = (req.headers.host || '').trim();
  if (!raw) {
    return res.status(400).json({ error: 'Missing Host header' });
  }
  // Strip port suffix: "localhost:3000" → "localhost"
  const hostOnly = (raw.includes(':')
    ? raw.substring(0, raw.lastIndexOf(':'))
    : raw
  ).replace(/^\[|\]$/g, '').toLowerCase();
  if (!_ACCEPTED_HOST_VALUES.has(hostOnly)) {
    return res.status(400).json({
      error: 'Invalid Host header. Bridge accepts loopback hosts only.',
    });
  }
  next();
});

// Poll for new messages (long-poll style)
app.get('/messages', (req, res) => {
  const msgs = messageQueue.splice(0, messageQueue.length);
  res.json(msgs);
});

// Send a message
app.post('/send', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }
  if (rejectWhenOutboundBlocked(res, 'send', chatId)) return;

  try {
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];
    for (let i = 0; i < chunks.length; i += 1) {
      const options = i === 0 ? sendOptionsForReplyTo(replyTo) : {};
      const sent = await sendWithTimeout(chatId, { text: chunks[i] }, options);
      trackSentMessageId(sent);
      if (sent?.key?.id) messageIds.push(sent.key.id);
      if (chunks.length > 1 && i < chunks.length - 1) {
        await sleep(CHUNK_DELAY_MS);
      }
    }

    res.json({
      success: true,
      messageId: messageIds[messageIds.length - 1],
      messageIds,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Edit a previously sent message
app.post('/edit', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }
  if (rejectWhenOutboundBlocked(res, 'edit', chatId)) return;

  try {
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];

    await sendWithTimeout(chatId, { text: chunks[0], edit: key });
    if (chunks.length > 1) {
      for (let i = 1; i < chunks.length; i += 1) {
        const sent = await sendWithTimeout(chatId, { text: chunks[i] });
        trackSentMessageId(sent);
        if (sent?.key?.id) messageIds.push(sent.key.id);
        if (i < chunks.length - 1) {
          await sleep(CHUNK_DELAY_MS);
        }
      }
    }

    res.json({ success: true, messageIds });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// MIME type map and media type inference for /send-media
const MIME_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  webp: 'image/webp', gif: 'image/gif',
  mp4: 'video/mp4', mov: 'video/quicktime', avi: 'video/x-msvideo',
  mkv: 'video/x-matroska', '3gp': 'video/3gpp',
  pdf: 'application/pdf',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

function inferMediaType(ext) {
  if (['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'avi', 'mkv', '3gp'].includes(ext)) return 'video';
  if (['ogg', 'opus', 'mp3', 'wav', 'm4a'].includes(ext)) return 'audio';
  return 'document';
}

// Send media (image, video, document) natively
app.post('/send-media', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName, replyTo } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }
  if (rejectWhenOutboundBlocked(res, 'send-media', chatId)) return;

  try {
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }

    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const type = mediaType || inferMediaType(ext);
    let msgPayload;

    switch (type) {
      case 'image':
        msgPayload = { image: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'image/jpeg' };
        break;
      case 'video':
        msgPayload = { video: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'video/mp4' };
        break;
      case 'audio': {
        // WhatsApp only renders a native voice bubble (ptt) when the file is ogg/opus.
        // If the caller passes mp3, wav, m4a etc. (e.g. from Edge TTS / NeuTTS),
        // silently convert to ogg/opus via ffmpeg so ptt is always honoured.
        let audioBuffer = buffer;
        let audioExt = ext;
        const needsConversion = !['ogg', 'opus'].includes(ext);
        let tmpPath = null;
        if (needsConversion) {
          tmpPath = path.join(tmpdir(), `hermes_voice_${randomBytes(6).toString('hex')}.ogg`);
          try {
            execSync(
              `ffmpeg -y -i ${JSON.stringify(filePath)} -ar 48000 -ac 1 -c:a libopus ${JSON.stringify(tmpPath)}`,
              { timeout: 30000, stdio: 'pipe' }
            );
            audioBuffer = readFileSync(tmpPath);
            audioExt = 'ogg';
          } catch (convErr) {
            // ffmpeg not available or conversion failed — fall back to original format
            console.warn('[bridge] ffmpeg conversion failed, sending as file attachment:', convErr.message);
          } finally {
            try { if (tmpPath && existsSync(tmpPath)) unlinkSync(tmpPath); } catch (_) {}
          }
        }
        const audioMime = (audioExt === 'ogg' || audioExt === 'opus') ? 'audio/ogg; codecs=opus' : 'audio/mpeg';
        msgPayload = { audio: audioBuffer, mimetype: audioMime, ptt: audioExt === 'ogg' || audioExt === 'opus' };
        break;
      }
      case 'document':
      default:
        msgPayload = {
          document: buffer,
          fileName: fileName || path.basename(filePath),
          caption: caption || undefined,
          mimetype: MIME_MAP[ext] || 'application/octet-stream',
        };
        break;
    }

    const sent = await sendWithTimeout(chatId, msgPayload, sendOptionsForReplyTo(replyTo));

    trackSentMessageId(sent);

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { chatId } = req.body;
  if (!chatId) return res.status(400).json({ error: 'chatId required' });
  if (rejectWhenOutboundBlocked(res, 'typing', chatId)) return;

  try {
    await sock.sendPresenceUpdate('composing', chatId);
    res.json({ success: true });
  } catch (err) {
    res.json({ success: false });
  }
});

// Create a WhatsApp group. Explicitly feature-gated because there is no
// existing chat id to run through the per-chat outbound allowlist yet.
app.post('/create-group', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }
  if (rejectWhenGroupAdminBlocked(res, 'create-group')) return;

  const subject = String(req.body?.subject || '').trim();
  const rawParticipants = req.body?.participants;
  if (!subject) {
    return res.status(400).json({ error: 'subject is required' });
  }
  if (subject.length > 100) {
    return res.status(400).json({ error: 'subject must be <= 100 characters' });
  }
  if (!Array.isArray(rawParticipants) || rawParticipants.length === 0) {
    return res.status(400).json({ error: 'participants must be a non-empty array' });
  }

  const participants = [];
  const invalidParticipants = [];
  for (const value of rawParticipants) {
    const jid = normalizeParticipantJid(value);
    if (!jid) {
      invalidParticipants.push(value);
      continue;
    }
    if (!participants.includes(jid)) participants.push(jid);
  }
  if (invalidParticipants.length > 0) {
    return res.status(400).json({
      error: 'participants contains invalid WhatsApp identifiers',
      invalidParticipants,
    });
  }
  if (participants.length === 0) {
    return res.status(400).json({ error: 'participants must include at least one valid user' });
  }
  if (Number.isFinite(GROUP_CREATE_MAX_PARTICIPANTS) && participants.length > GROUP_CREATE_MAX_PARTICIPANTS) {
    return res.status(400).json({
      error: `participants exceeds WHATSAPP_GROUP_CREATE_MAX_PARTICIPANTS=${GROUP_CREATE_MAX_PARTICIPANTS}`,
    });
  }

  try {
    const metadata = await sock.groupCreate(subject, participants);
    const jid = metadata?.id || metadata?.jid || metadata?.gid || '';
    if (jid) {
      rememberChatMetadata(jid, {
        name: metadata?.subject || subject,
        subject: metadata?.subject || subject,
        type: 'group',
        participants: (metadata?.participants || []).map(p => p.id).filter(Boolean),
      }, 'createGroup');
      flushChatMetadata();
    }
    res.json({
      success: true,
      jid,
      subject: metadata?.subject || subject,
      participants: (metadata?.participants || []).map(p => p.id).filter(Boolean),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Cleanup helper for endpoint smoke tests. The caller must name the expected
// subject to reduce accidental leaves from real operator groups. For throwaway
// smoke groups, removeParticipants=true removes non-bot participants first so
// the cleanup does not strand a test phone in an empty group.
app.post('/leave-group', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }
  if (rejectWhenGroupAdminBlocked(res, 'leave-group')) return;

  const chatId = normalizeGroupJid(req.body?.chatId);
  const expectedSubject = String(req.body?.expectedSubject || '').trim();
  if (!chatId) return res.status(400).json({ error: 'valid group chatId is required' });
  if (!expectedSubject) return res.status(400).json({ error: 'expectedSubject is required' });

  try {
    const metadata = await sock.groupMetadata(chatId);
    if (metadata?.subject !== expectedSubject) {
      return res.status(409).json({
        error: 'group subject mismatch',
        expectedSubject,
        actualSubject: metadata?.subject || '',
      });
    }
    const removedParticipants = [];
    if (req.body?.removeParticipants === true) {
      const ownIds = new Set([
        normalizeParticipantJid(sock.user?.id),
        normalizeParticipantJid(sock.user?.lid),
      ].filter(Boolean));
      const removableParticipants = (metadata?.participants || [])
        .map(p => normalizeParticipantJid(p?.id))
        .filter(jid => jid && !ownIds.has(jid));
      if (removableParticipants.length > 0) {
        await sock.groupParticipantsUpdate(chatId, removableParticipants, 'remove');
        removedParticipants.push(...removableParticipants);
      }
    }
    await sock.groupLeave(chatId);
    res.json({ success: true, chatId, subject: metadata?.subject, removedParticipants });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Chat info
app.get('/chat/:id', async (req, res) => {
  const chatId = req.params.id;
  const isGroup = chatId.endsWith('@g.us');

  if (isGroup && sock) {
    try {
      const metadata = await sock.groupMetadata(chatId);
      rememberChatMetadata(chatId, {
        name: metadata.subject,
        subject: metadata.subject,
        type: 'group',
        participants: metadata.participants.map(p => p.id),
      }, 'chatEndpoint');
      flushChatMetadata();
      return res.json({
        name: metadata.subject,
        isGroup: true,
        participants: metadata.participants.map(p => p.id),
      });
    } catch {
      // Fall through to default
    }
  }

  res.json({
    name: chatId.replace(/@.*/, ''),
    isGroup,
    participants: [],
  });
});

// Health check
app.get('/health', (req, res) => {
  res.json({
    status: connectionState,
    queueLength: messageQueue.length,
    paired: connectionState === 'connected',
    hasQr: !!latestQr,
    qrUpdatedAt: latestQrAt,
    connectedAt,
    startedAt: socketStartedAt,
    lastDisconnectReason,
    storeFile: STORE_FILE,
    storeMessages: messageStore.size,
    chatMetadataFile: CHAT_METADATA_FILE,
    chatMetadataCount: chatMetadata.size,
    uptime: process.uptime(),
  });
});

app.get('/state', (req, res) => {
  res.json({
    ok: true,
    status: connectionState,
    paired: connectionState === 'connected',
    hasQr: !!latestQr,
    qrUpdatedAt: latestQrAt,
    connectedAt,
    startedAt: socketStartedAt,
    lastDisconnectReason,
  });
});

app.post('/start', (req, res) => {
  res.json({
    ok: true,
    status: connectionState,
    paired: connectionState === 'connected',
    hasQr: !!latestQr,
    qrUpdatedAt: latestQrAt,
    connectedAt,
    startedAt: socketStartedAt,
    lastDisconnectReason,
  });
});

app.get('/qr', (req, res) => {
  if (!latestQr) {
    return res.status(connectionState === 'connected' ? 409 : 404).json({
      ok: false,
      status: connectionState,
      reason: connectionState === 'connected' ? 'already-paired' : 'qr-not-ready',
    });
  }
  res.json({
    ok: true,
    status: connectionState,
    qr: latestQr,
    qrUpdatedAt: latestQrAt,
  });
});

app.get('/qr.png', async (req, res) => {
  if (!latestQr) {
    return res.status(connectionState === 'connected' ? 409 : 404).json({
      ok: false,
      status: connectionState,
      reason: connectionState === 'connected' ? 'already-paired' : 'qr-not-ready',
    });
  }
  try {
    const png = await QRCode.toBuffer(latestQr, {
      type: 'png',
      width: 720,
      margin: 2,
      errorCorrectionLevel: 'M',
    });
    res.writeHead(200, {
      'Content-Type': 'image/png',
      'Content-Length': png.length,
      'Cache-Control': 'no-store',
    });
    res.end(png);
  } catch (err) {
    res.status(500).json({ ok: false, reason: err.message });
  }
});

// Start
if (PAIR_ONLY) {
  // Pair-only mode: just connect, show QR, save creds, exit. No HTTP server.
  console.log('📱 WhatsApp pairing mode');
  console.log(`📁 Session: ${SESSION_DIR}`);
  console.log();
  startSocket();
} else {
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp bridge listening on port ${PORT} (mode: ${WHATSAPP_MODE})`);
    console.log(`📁 Session stored in: ${SESSION_DIR}`);
    if (ALLOWED_USERS.size > 0) {
      console.log(`🔒 Allowed users: ${Array.from(ALLOWED_USERS).join(', ')}`);
    } else if (WHATSAPP_MODE === 'self-chat') {
      console.log(`🔒 Self-chat mode — only your own messages to yourself are processed.`);
    } else {
      console.log(`🔒 No WHATSAPP_ALLOWED_USERS set — incoming messages are rejected.`);
      console.log(`   Set WHATSAPP_ALLOWED_USERS=<phone> to authorize specific users,`);
      console.log(`   or WHATSAPP_ALLOWED_USERS=* for an explicit open bot.`);
    }
    if (OUTBOUND_DISABLED) {
      console.log('🔒 Outbound WhatsApp send/edit/media/typing endpoints are disabled.');
    } else if (OUTBOUND_CHAT_FILTER_CONFIGURED) {
      const allowed = Array.from(OUTBOUND_ALLOWED_CHATS);
      if (allowed.length > 0) {
        console.log(`🔒 Outbound WhatsApp allowed chats: ${allowed.join(', ')}`);
      } else {
        console.log('🔒 Outbound WhatsApp fail-closed: no allowed chats configured.');
      }
    }
    if (GROUP_CREATE_ENABLED) {
      console.log(`🔒 WhatsApp group admin endpoints enabled (max participants: ${GROUP_CREATE_MAX_PARTICIPANTS}).`);
    }
    if (SYNC_FULL_HISTORY) {
      console.log(`🕰️  Full WhatsApp history sync is enabled (queue max: ${MAX_QUEUE_SIZE}).`);
      console.log(`🕰️  History sync file: ${HISTORY_SYNC_PATH}`);
      console.log(`🕰️  History metadata file: ${HISTORY_METADATA_PATH}`);
      if (!HISTORY_TO_LIVE_QUEUE) {
        console.log('🕰️  History sync will not enter the live message queue.');
      }
    }
    console.log();
    startSocket();
  });
}
