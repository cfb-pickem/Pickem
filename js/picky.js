// js/picky.js — the League Assistant mascot ("Picky").
//
// Floating anthropomorphic football in the bottom-right corner of the
// leaderboard. Hidden unless the viewer is signed in. Click him to open the
// chat panel; it streams answers from the local Ollama on the mini through
// a Cloudflare tunnel (pickem-chat.dunecreek.ai → :8766). He never pops
// anything up on his own: nothing appears until the launcher is tapped.
//
// He is built from inline SVG so there's no image to host and he scales
// cleanly in both themes. He's a *pure addition* — no other module imports
// anything from here, and if this file fails to load the leaderboard is
// exactly as it was.

import { supabase } from './supabaseClient.js';
import { sessionInfo } from './session.js';

// --- config --------------------------------------------------------------

// The tunnel host. Infra needs to add this to ~/.cloudflared/config.yml
// before the mascot can actually talk.
const CHAT_URL = 'https://pickem-chat.dunecreek.ai/chat';

const MAX_TURNS = 10;

// Phones get a full-screen panel; must match the @media query in STYLE.
const isPhone = () => window.matchMedia('(max-width: 600px)').matches;

// --- entry point ---------------------------------------------------------
//
// Idempotent: both the leaderboard and the picks page may call this, and we
// never want two mascots on-screen. The first caller wins, the second is a
// no-op. mode: "leaderboard" (default) or "picks" (deep-analysis riff).

export async function initPicky(opts = {}) {
  if (window.__pickyMounted) return;
  window.__pickyMounted = true;

  const mode = opts.mode === 'picks' ? 'picks' : 'leaderboard';
  const info = await sessionInfo();
  if (!info.signedIn) return;             // hidden to logged-out visitors

  const ui = mountUI();
  // Rival-prediction producer: given the week + games, return the compact
  // {rival_team_id: {game_id: {pick, confidence}}} shape the backend wants.
  // Only meaningful on the picks page — leaderboard calls return {}.
  const rivalPredictor = opts.rivalPredictions || (async () => ({}));

  wireChat(ui, info, { mode, rivalPredictor });
}

// --- UI mount ------------------------------------------------------------

function mountUI() {
  const root = document.createElement('div');
  root.id = 'picky-root';
  root.innerHTML = STYLE + MASCOT_HTML + PANEL_HTML;
  document.body.appendChild(root);
  return {
    root,
    mascot:  root.querySelector('.pk-mascot'),
    panel:   root.querySelector('.pk-panel'),
    body:    root.querySelector('.pk-body'),
    form:    root.querySelector('.pk-form'),
    input:   root.querySelector('.pk-input'),
    close:   root.querySelector('.pk-close'),
  };
}

// --- chat wiring ---------------------------------------------------------

function wireChat(ui, info, chatOpts) {
  const mode = chatOpts.mode;
  const rivalPredictor = chatOpts.rivalPredictor;
  // conversation history kept in memory only — spec says do not persist
  const history = [];

  ui.mascot.addEventListener('click', () => openPanel(ui));
  ui.close .addEventListener('click', () => closePanel(ui));

  // Mobile keyboard handling: when the input is focused, the on-screen
  // keyboard eats half the screen and iOS pans the visual viewport. Pin the
  // panel to exactly the visible area so the header and input both stay on
  // screen.
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', () => syncViewport(ui, true));
    window.visualViewport.addEventListener('scroll', () => syncViewport(ui, false));
    ui.input.addEventListener('focus', () => setTimeout(() => syncViewport(ui, true), 50));
  }

  ui.form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const text = ui.input.value.trim();
    if (!text) return;
    ui.input.value = '';
    history.push({ role: 'user', content: text });
    appendMsg(ui, 'user', text);
    const trimmed = history.slice(-MAX_TURNS * 2);
    const bubble = appendMsg(ui, 'assistant', '');
    bubble.classList.add('pk-typing');

    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        bubble.classList.remove('pk-typing');
        bubble.textContent = 'Your session expired, dumbass. Refresh the page.';
        return;
      }
      // Rival predictions are opt-in per page. If the model isn't ready yet
      // or the fetch throws, fall back to no-prediction mode — Picky just
      // won't have reads on rivals this turn.
      let rivalPreds = {};
      try { rivalPreds = (await rivalPredictor()) || {}; }
      catch (e) { console.warn('picky: rival predictor failed', e); }
      await streamReply(trimmed, session.access_token, bubble, history, mode, rivalPreds);
    } catch (e) {
      bubble.classList.remove('pk-typing');
      bubble.textContent = 'Picky shit the bed: ' + (e.message || e);
    }
  });
}

async function streamReply(messages, jwt, bubble, history, mode, rivalPreds) {
  const r = await fetch(CHAT_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      messages,
      supabase_jwt: jwt,
      mode,
      rival_predictions: rivalPreds,
    }),
  });
  if (!r.ok) {
    bubble.classList.remove('pk-typing');
    bubble.textContent =
      r.status === 429 ? "Ten questions a minute, max. Slow the fuck down and ask a better one." :
      r.status === 401 ? "Session kicked. Refresh and sign back in, genius." :
      r.status === 502 ? "My brain's not answering. Give me a damn minute." :
                         "Shit went sideways. Try again.";
    return;
  }

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '', full = '';
  bubble.classList.remove('pk-typing');
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    // SSE frames are "data: {...}\n\n"
    let i;
    while ((i = buf.indexOf('\n\n')) !== -1) {
      const raw = buf.slice(0, i).trim();
      buf = buf.slice(i + 2);
      if (!raw.startsWith('data:')) continue;
      const payload = raw.slice(5).trim();
      if (payload === '[DONE]') continue;
      try {
        const obj = JSON.parse(payload);
        if (obj.content) {
          full += obj.content;
          renderMarkdown(bubble, full);
          bubble.parentElement.scrollTop = bubble.parentElement.scrollHeight;
        } else if (obj.image && obj.image.startsWith('data:image/')) {
          const img = document.createElement('img');
          img.className = 'pk-img';
          img.alt = 'Picture from Picky';
          img.src = obj.image;
          img.onload = () => { const pane = bubble.closest('.pk-body'); if (pane) pane.scrollTop = pane.scrollHeight; };
          bubble.appendChild(img);
        } else if (obj.error) {
          bubble.textContent = full || "Brain cramp. Ask again, and make it a better question this time.";
        }
      } catch { /* ignore partials */ }
    }
  }
  if (full) history.push({ role: 'assistant', content: full });
}

// --- tiny markdown -------------------------------------------------------
//
// Model output is untrusted. It never goes near innerHTML: every piece is a
// text node or an element we create, so "<img onerror=...>" just shows up as
// those characters. Handles what Picky actually writes: **bold**, *italic*
// / _italic_, "- " and "1. " lists, and line breaks.

const MD_INLINE = /\*\*([^*\n]+?)\*\*|__([^_\n]+?)__|\*([^*\n]+?)\*|(?<![A-Za-z0-9])_([^_\n]+?)_(?![A-Za-z0-9])/g;

function appendInline(parent, text) {
  let last = 0;
  for (const m of text.matchAll(MD_INLINE)) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const el = document.createElement((m[1] ?? m[2]) != null ? 'strong' : 'em');
    el.textContent = m[1] ?? m[2] ?? m[3] ?? m[4];
    parent.appendChild(el);
    last = m.index + m[0].length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function renderMarkdown(el, text) {
  el.replaceChildren();
  let list = null, listType = '', pendingBreak = false;
  for (const line of text.split('\n')) {
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    const ol = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (ul || ol) {
      const type = ul ? 'ul' : 'ol';
      if (!list || listType !== type) {
        list = document.createElement(type);
        list.className = 'pk-list';
        if (ol) list.start = Number(ol[1]);
        listType = type;
        el.appendChild(list);
      }
      const li = document.createElement('li');
      appendInline(li, ul ? ul[1] : ol[2]);
      list.appendChild(li);
      pendingBreak = false;
      continue;
    }
    if (!line.trim()) {             // blank line ends a list, keeps one gap
      if (!list && el.childNodes.length) pendingBreak = true;
      list = null;
      continue;
    }
    if (list) { list = null; }
    else if (el.childNodes.length) el.appendChild(document.createElement('br'));
    if (pendingBreak) { el.appendChild(document.createElement('br')); pendingBreak = false; }
    appendInline(el, line);
  }
}

// On phones the page behind the panel is scroll-locked (body pinned at its
// current offset, the iOS-safe way) and restored exactly on close. The input
// is not auto-focused there: popping the keyboard on open is what made iOS
// shove the panel off-screen.
let lockedScrollY = null;

function openPanel(ui) {
  ui.panel.classList.add('open');
  if (isPhone()) {
    if (lockedScrollY === null) {
      lockedScrollY = window.scrollY;
      document.body.style.top = `-${lockedScrollY}px`;
      document.documentElement.classList.add('pk-locked');
    }
    syncViewport(ui, true);
  } else {
    ui.input.focus();
  }
  ui.body.scrollTop = ui.body.scrollHeight;
}
function closePanel(ui) {
  ui.panel.classList.remove('open');
  ui.panel.style.top = '';
  ui.panel.style.height = '';
  if (lockedScrollY !== null) {
    document.documentElement.classList.remove('pk-locked');
    document.body.style.top = '';
    window.scrollTo({ top: lockedScrollY, behavior: 'instant' });
    lockedScrollY = null;
  }
}

function syncViewport(ui, toBottom) {
  const vv = window.visualViewport;
  if (!vv || !ui.panel.classList.contains('open') || !isPhone()) return;
  ui.panel.style.top = vv.offsetTop + 'px';
  ui.panel.style.height = vv.height + 'px';
  if (toBottom) ui.body.scrollTop = ui.body.scrollHeight;
}

function appendMsg(ui, role, text) {
  const row = document.createElement('div');
  row.className = 'pk-row pk-row-' + role;
  const bubble = document.createElement('div');
  bubble.className = 'pk-bubble pk-bubble-' + role;
  bubble.textContent = text;
  row.appendChild(bubble);
  ui.body.appendChild(row);
  ui.body.scrollTop = ui.body.scrollHeight;
  return bubble;
}

// --- markup + styles ----------------------------------------------------

const MASCOT_HTML = `
<button class="pk-mascot" aria-label="Open Picky">
  <svg viewBox="0 0 120 80" width="100%" height="100%" aria-hidden="true">
    <!-- football body -->
    <ellipse cx="60" cy="40" rx="52" ry="28" fill="#8B4A1C" stroke="#4a2810" stroke-width="2.5"/>
    <!-- highlight -->
    <ellipse cx="46" cy="30" rx="18" ry="6" fill="#a7622a" opacity="0.55"/>
    <!-- end stripes -->
    <path d="M10 40 Q18 28 18 40 Q18 52 10 40" fill="#f5efe4" opacity="0.9"/>
    <path d="M110 40 Q102 28 102 40 Q102 52 110 40" fill="#f5efe4" opacity="0.9"/>
    <!-- laces -->
    <rect x="56" y="34" width="8" height="12" rx="1.5" fill="#f5efe4"/>
    <line x1="52" y1="38" x2="68" y2="38" stroke="#f5efe4" stroke-width="1.8" stroke-linecap="round"/>
    <line x1="52" y1="42" x2="68" y2="42" stroke="#f5efe4" stroke-width="1.8" stroke-linecap="round"/>
    <!-- eyes -->
    <circle cx="48" cy="26" r="6" fill="#fff"/>
    <circle cx="72" cy="26" r="6" fill="#fff"/>
    <circle cx="49" cy="27" r="2.5" fill="#111"/>
    <circle cx="73" cy="27" r="2.5" fill="#111"/>
    <!-- smirk -->
    <path class="pk-mouth" d="M52 52 Q60 58 68 52" fill="none" stroke="#2a1708" stroke-width="2.5" stroke-linecap="round"/>
  </svg>
</button>`;

const PANEL_HTML = `
<section class="pk-panel" role="dialog" aria-label="Chat with Picky">
  <header class="pk-head">
    <span class="pk-title">Picky</span>
    <button class="pk-close" aria-label="Close">×</button>
  </header>
  <div class="pk-body">
    <div class="pk-row pk-row-assistant">
      <div class="pk-bubble pk-bubble-assistant">Picky here. I've seen your picks. We need to talk. What do you want?</div>
    </div>
  </div>
  <form class="pk-form">
    <input class="pk-input" type="text" placeholder="Ask Picky…" autocomplete="off"/>
    <button class="pk-send" type="submit" aria-label="Send">
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M3 20l18-8L3 4l3 8-3 8z" fill="currentColor"/></svg>
    </button>
  </form>
</section>`;

const STYLE = `
<style>
#picky-root { position: fixed; right: 18px;
                 bottom: calc(18px + env(safe-area-inset-bottom, 0px));
                 z-index: 9000; font-family: inherit; color: #e7e7e7; }
#picky-root * { box-sizing: border-box; }

.pk-mascot { background: none; border: 0; padding: 4px; cursor: pointer;
             width: 72px; height: 48px;
             filter: drop-shadow(0 4px 10px rgba(0,0,0,.55));
             animation: pk-bounce 2.6s ease-in-out infinite;
             -webkit-tap-highlight-color: transparent; }
.pk-mascot:hover { animation-play-state: paused; transform: translateY(-2px); }
@keyframes pk-bounce { 0%,100%{transform:translateY(0)}
                       50%   {transform:translateY(-6px)} }

.pk-panel { position: absolute; right: 0; bottom: 0; width: 360px;
            max-width: calc(100vw - 24px); height: 440px; max-height: 70vh;
            background: #0b0b0b; border: 1px solid rgba(218,165,32,.4);
            border-radius: 12px; display: none; flex-direction: column;
            overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,.6); }
.pk-panel.open { display: flex; }

.pk-head { display:flex; align-items:center; gap:8px;
           padding: 10px 12px; background: #141414;
           border-bottom: 1px solid rgba(255,255,255,.06); }
.pk-title { font-weight: 700; letter-spacing: .04em; flex: 1;
            color: #daa520; font-family: 'Oswald', sans-serif; font-size: 15px; }
.pk-close { background: none; border: 0; cursor: pointer;
            color: #ccc; padding: 2px 6px; font-size: 18px; }

.pk-body { flex: 1; overflow-y: auto; padding: 10px 12px;
           display: flex; flex-direction: column; gap: 8px; }
.pk-row { display: flex; }
.pk-row-user { justify-content: flex-end; }
.pk-bubble { max-width: 85%; padding: 8px 11px; border-radius: 12px;
             font-size: 14px; line-height: 1.4; white-space: pre-wrap;
             word-wrap: break-word; }
.pk-bubble-assistant { white-space: normal; }
.pk-bubble strong { color: #f1c75b; font-weight: 700; }
.pk-list { margin: 4px 0; padding-left: 20px; list-style: decimal outside; }
ul.pk-list { list-style: disc outside; }
.pk-list li { margin: 2px 0; }
.pk-img { display: block; width: 100%; margin-top: 6px; border-radius: 8px; }
.pk-bubble-user { background: #1e3a2c; color: #d9f5e4; border-bottom-right-radius: 3px; }
.pk-bubble-assistant { background: #1a1a1a; color: #e7e7e7;
                        border: 1px solid rgba(218,165,32,.2);
                        border-bottom-left-radius: 3px; }
.pk-typing::after { content: ' …'; animation: pk-dots 1.1s steps(4,end) infinite; }
@keyframes pk-dots { 0%,20% {content:''} 40% {content:' .'}
                     60% {content:' ..'} 80%,100% {content:' …'} }

.pk-form { display: flex; gap: 6px; padding: 8px;
           padding-bottom: calc(8px + env(safe-area-inset-bottom, 0px));
           border-top: 1px solid rgba(255,255,255,.06); background: #0f0f0f; }
.pk-input { flex: 1; background: #1a1a1a; border: 1px solid rgba(255,255,255,.1);
            color: #fff; padding: 8px 10px; border-radius: 8px; font-size: 14px; }
.pk-input:focus { outline: none; border-color: #daa520; }
.pk-send { background: #daa520; color: #111; border: 0; width: 38px;
           border-radius: 8px; cursor: pointer; display: grid; place-items: center; }
.pk-send:hover { background: #f1b936; }

/* Mobile: shrink the mascot, full-screen the chat panel, and give the
   close button a bigger tap target. The mascot floats above the iOS
   bottom chrome via the safe-area inset at the root. The open panel is
   pinned to the viewport (100dvh, 100vh fallback); JS narrows it to the
   visualViewport when the keyboard is up and locks the page behind it. */
@media (max-width: 600px) {
  #picky-root { right: 12px;
                   bottom: calc(12px + env(safe-area-inset-bottom, 0px)); }
  .pk-mascot { width: 56px; height: 40px; padding: 2px; }
  .pk-panel.open {
    position: fixed; inset: 0; z-index: 9001;
    width: 100%; max-width: none;
    height: 100vh; height: 100dvh; max-height: none;
    border-radius: 0; border: 0;
  }
  .pk-body { overscroll-behavior: contain; }
  .pk-head { padding: 14px 14px; padding-top: calc(14px + env(safe-area-inset-top, 0px));
             padding-left: calc(14px + env(safe-area-inset-left, 0px));
             padding-right: calc(14px + env(safe-area-inset-right, 0px)); }
  .pk-form { padding-left: calc(8px + env(safe-area-inset-left, 0px));
             padding-right: calc(8px + env(safe-area-inset-right, 0px)); }
  .pk-close { font-size: 26px; padding: 4px 10px; min-width: 44px; min-height: 44px; }
  .pk-input { font-size: 16px; }  /* 16px stops iOS zoom-on-focus */
  .pk-send  { width: 44px; min-height: 40px; }
}
/* Page behind the open phone panel: frozen in place, restored on close. */
html.pk-locked, html.pk-locked body { overflow: hidden; }
html.pk-locked body { position: fixed; left: 0; right: 0; width: 100%; }
</style>`;
