// js/mrLocks.js — the League Assistant mascot ("Mr. Locks").
//
// Floating anthropomorphic football in the bottom-right corner of the
// leaderboard. Hidden unless the viewer is signed in. Click him to open the
// chat panel; it streams answers from the local Ollama on the mini through
// a Cloudflare tunnel (pickem-chat.dunecreek.ai → :8766). Occasionally he
// pops a one-liner tip based on page state, dismissible, per-session.
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
const TIP_MIN_MS = 30_000;
const TIP_MAX_MS = 60_000;

const MUTE_KEY = 'mrLocks.muted';
const TIP_DISMISS_KEY = 'mrLocks.tipsDismissed';   // per-session, sessionStorage

// --- entry point ---------------------------------------------------------

export async function initMrLocks() {
  const info = await sessionInfo();
  if (!info.signedIn) return;             // hidden to logged-out visitors

  const ui = mountUI();
  const ctx = await loadPageContext(info);
  wireTips(ui, ctx);
  wireChat(ui, info);
}

// --- UI mount ------------------------------------------------------------

function mountUI() {
  const root = document.createElement('div');
  root.id = 'mr-locks-root';
  root.innerHTML = STYLE + MASCOT_HTML + PANEL_HTML + TIP_HTML;
  document.body.appendChild(root);
  return {
    root,
    mascot:  root.querySelector('.ml-mascot'),
    panel:   root.querySelector('.ml-panel'),
    body:    root.querySelector('.ml-body'),
    form:    root.querySelector('.ml-form'),
    input:   root.querySelector('.ml-input'),
    close:   root.querySelector('.ml-close'),
    mute:    root.querySelector('.ml-mute'),
    tip:     root.querySelector('.ml-tip'),
    tipText: root.querySelector('.ml-tip-text'),
    tipClose:root.querySelector('.ml-tip-close'),
  };
}

// --- chat wiring ---------------------------------------------------------

function wireChat(ui, info) {
  // conversation history kept in memory only — spec says do not persist
  const history = [];

  // mute toggle
  if (localStorage.getItem(MUTE_KEY) === '1') ui.mute.classList.add('muted');
  ui.mute.addEventListener('click', () => {
    const next = ui.mute.classList.toggle('muted');
    localStorage.setItem(MUTE_KEY, next ? '1' : '0');
    hideTip(ui);
  });

  ui.mascot.addEventListener('click', () => openPanel(ui));
  ui.close .addEventListener('click', () => closePanel(ui));

  ui.form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const text = ui.input.value.trim();
    if (!text) return;
    ui.input.value = '';
    history.push({ role: 'user', content: text });
    appendMsg(ui, 'user', text);
    const trimmed = history.slice(-MAX_TURNS * 2);
    const bubble = appendMsg(ui, 'assistant', '');
    bubble.classList.add('ml-typing');

    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        bubble.classList.remove('ml-typing');
        bubble.textContent = 'Session expired — refresh the page.';
        return;
      }
      await streamReply(trimmed, session.access_token, bubble, history);
    } catch (e) {
      bubble.classList.remove('ml-typing');
      bubble.textContent = 'Mr. Locks is on a timeout: ' + (e.message || e);
    }
  });
}

async function streamReply(messages, jwt, bubble, history) {
  const r = await fetch(CHAT_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, supabase_jwt: jwt }),
  });
  if (!r.ok) {
    bubble.classList.remove('ml-typing');
    bubble.textContent =
      r.status === 429 ? "Easy — ten questions a minute. Breathe." :
      r.status === 401 ? "Session kicked. Refresh and sign back in." :
      r.status === 502 ? "Model's not answering. Try me in a minute." :
                         "Something went sideways. Try again.";
    return;
  }

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '', full = '';
  bubble.classList.remove('ml-typing');
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
          bubble.textContent = full;
          bubble.parentElement.scrollTop = bubble.parentElement.scrollHeight;
        } else if (obj.error) {
          bubble.textContent = full || "Model hiccup — try again.";
        }
      } catch { /* ignore partials */ }
    }
  }
  if (full) history.push({ role: 'assistant', content: full });
}

function openPanel(ui) {
  ui.panel.classList.add('open');
  ui.input.focus();
  hideTip(ui);
}
function closePanel(ui) {
  ui.panel.classList.remove('open');
}

function appendMsg(ui, role, text) {
  const row = document.createElement('div');
  row.className = 'ml-row ml-row-' + role;
  const bubble = document.createElement('div');
  bubble.className = 'ml-bubble ml-bubble-' + role;
  bubble.textContent = text;
  row.appendChild(bubble);
  ui.body.appendChild(row);
  ui.body.scrollTop = ui.body.scrollHeight;
  return bubble;
}

// --- contextual pop-ups --------------------------------------------------
//
// Very light: one-liner based on whether the signed-in user has made picks
// for the current open week. Dismiss-for-session so he's not nagging.

async function loadPageContext(info) {
  const ctx = { teamId: null, pickedThisWeek: null, currentWeek: null };
  try {
    const { data: team } = await supabase
      .from('teams').select('team_id')
      .eq('user_id', info.user.id).maybeSingle();
    ctx.teamId = team?.team_id ?? null;

    const { data: seasonRow } = await supabase
      .from('all_games').select('cfb_season')
      .order('cfb_season', { ascending: false }).limit(1);
    const season = seasonRow?.[0]?.cfb_season;

    const { data: openRow } = await supabase
      .from('all_games').select('week')
      .eq('cfb_season', season).eq('picked', true).neq('Status', 'final')
      .order('week', { ascending: true }).limit(1);
    ctx.currentWeek = openRow?.[0]?.week ?? null;

    if (ctx.teamId && ctx.currentWeek != null) {
      const { data: picks } = await supabase
        .from('picks').select('game_id')
        .eq('team_id', ctx.teamId).eq('week', ctx.currentWeek).limit(1);
      ctx.pickedThisWeek = !!(picks && picks.length);
    }
  } catch (_) { /* context is optional — tips just go generic */ }
  return ctx;
}

function wireTips(ui, ctx) {
  ui.tipClose.addEventListener('click', () => {
    hideTip(ui);
    sessionStorage.setItem(TIP_DISMISS_KEY, '1');
  });

  const schedule = () => {
    if (localStorage.getItem(MUTE_KEY) === '1') return;
    if (sessionStorage.getItem(TIP_DISMISS_KEY) === '1') return;
    if (ui.panel.classList.contains('open')) return;
    const delay = TIP_MIN_MS + Math.random() * (TIP_MAX_MS - TIP_MIN_MS);
    setTimeout(() => { showTip(ui, pickTip(ctx)); schedule(); }, delay);
  };
  schedule();
}

function pickTip(ctx) {
  const generic = [
    "Been staring at these spreads all morning. Ask me who's on upset alert.",
    "Need a lock? I got takes.",
    "Chalk or dog this week? Pick my brain.",
  ];
  if (ctx.currentWeek != null && ctx.pickedThisWeek === false) {
    return `Yo — you still need to lock in your Week ${ctx.currentWeek} picks. Want me to walk the slate?`;
  }
  return generic[Math.floor(Math.random() * generic.length)];
}

function showTip(ui, text) {
  ui.tipText.textContent = text;
  ui.tip.classList.add('open');
}
function hideTip(ui) {
  ui.tip.classList.remove('open');
}

// --- markup + styles ----------------------------------------------------

const MASCOT_HTML = `
<button class="ml-mascot" aria-label="Open Mr. Locks">
  <svg viewBox="0 0 120 80" width="60" height="40" aria-hidden="true">
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
    <path class="ml-mouth" d="M52 52 Q60 58 68 52" fill="none" stroke="#2a1708" stroke-width="2.5" stroke-linecap="round"/>
  </svg>
</button>`;

const TIP_HTML = `
<div class="ml-tip" role="status">
  <span class="ml-tip-text"></span>
  <button class="ml-tip-close" aria-label="Dismiss">×</button>
</div>`;

const PANEL_HTML = `
<section class="ml-panel" role="dialog" aria-label="Chat with Mr. Locks">
  <header class="ml-head">
    <span class="ml-title">Mr. Locks</span>
    <button class="ml-mute" title="Mute pop-ups">
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path d="M4 10v4h4l5 4V6L8 10H4z" fill="currentColor"/>
        <path class="ml-mute-x" d="M16 9l5 6M21 9l-5 6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/>
      </svg>
    </button>
    <button class="ml-close" aria-label="Close">×</button>
  </header>
  <div class="ml-body">
    <div class="ml-row ml-row-assistant">
      <div class="ml-bubble ml-bubble-assistant">What's shakin', pickem fam? I been staring at these spreads all morning. What you need?</div>
    </div>
  </div>
  <form class="ml-form">
    <input class="ml-input" type="text" placeholder="Ask Mr. Locks…" autocomplete="off"/>
    <button class="ml-send" type="submit" aria-label="Send">
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M3 20l18-8L3 4l3 8-3 8z" fill="currentColor"/></svg>
    </button>
  </form>
</section>`;

const STYLE = `
<style>
#mr-locks-root { position: fixed; right: 18px; bottom: 18px; z-index: 9000;
                 font-family: inherit; color: #e7e7e7; }
#mr-locks-root * { box-sizing: border-box; }

.ml-mascot { background: none; border: 0; padding: 6px; cursor: pointer;
             filter: drop-shadow(0 4px 10px rgba(0,0,0,.55));
             animation: ml-bounce 2.6s ease-in-out infinite; }
.ml-mascot:hover { animation-play-state: paused; transform: translateY(-2px); }
@keyframes ml-bounce { 0%,100%{transform:translateY(0)}
                       50%   {transform:translateY(-6px)} }

.ml-tip { position: absolute; right: 72px; bottom: 14px;
          max-width: 240px; padding: 10px 28px 10px 12px;
          background: #0b0b0b; border: 1px solid rgba(218,165,32,.5);
          border-radius: 10px; font-size: 13px; line-height: 1.35;
          box-shadow: 0 6px 18px rgba(0,0,0,.55);
          opacity: 0; transform: translateY(6px); pointer-events: none;
          transition: opacity .25s, transform .25s; }
.ml-tip.open { opacity: 1; transform: translateY(0); pointer-events: auto; }
.ml-tip::after{ content:""; position:absolute; right:-7px; bottom:14px;
                border:7px solid transparent; border-left-color:#0b0b0b; }
.ml-tip-close{ position:absolute; top:2px; right:4px; background:none;
               border:0; color:#888; font-size:18px; cursor:pointer; line-height:1; }

.ml-panel { position: absolute; right: 0; bottom: 0; width: 360px;
            max-width: calc(100vw - 24px); height: 440px; max-height: 70vh;
            background: #0b0b0b; border: 1px solid rgba(218,165,32,.4);
            border-radius: 12px; display: none; flex-direction: column;
            overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,.6); }
.ml-panel.open { display: flex; }

.ml-head { display:flex; align-items:center; gap:8px;
           padding: 10px 12px; background: #141414;
           border-bottom: 1px solid rgba(255,255,255,.06); }
.ml-title { font-weight: 700; letter-spacing: .04em; flex: 1;
            color: #daa520; font-family: 'Oswald', sans-serif; font-size: 15px; }
.ml-mute, .ml-close { background: none; border: 0; cursor: pointer;
                      color: #ccc; padding: 2px 6px; font-size: 18px; }
.ml-mute { color: #ccc; }
.ml-mute .ml-mute-x { display: none; }
.ml-mute.muted .ml-mute-x { display: inline; }
.ml-mute.muted { color: #8a2a2a; }

.ml-body { flex: 1; overflow-y: auto; padding: 10px 12px;
           display: flex; flex-direction: column; gap: 8px; }
.ml-row { display: flex; }
.ml-row-user { justify-content: flex-end; }
.ml-bubble { max-width: 85%; padding: 8px 11px; border-radius: 12px;
             font-size: 14px; line-height: 1.4; white-space: pre-wrap;
             word-wrap: break-word; }
.ml-bubble-user { background: #1e3a2c; color: #d9f5e4; border-bottom-right-radius: 3px; }
.ml-bubble-assistant { background: #1a1a1a; color: #e7e7e7;
                        border: 1px solid rgba(218,165,32,.2);
                        border-bottom-left-radius: 3px; }
.ml-typing::after { content: ' …'; animation: ml-dots 1.1s steps(4,end) infinite; }
@keyframes ml-dots { 0%,20% {content:''} 40% {content:' .'}
                     60% {content:' ..'} 80%,100% {content:' …'} }

.ml-form { display: flex; gap: 6px; padding: 8px; border-top: 1px solid rgba(255,255,255,.06); background: #0f0f0f; }
.ml-input { flex: 1; background: #1a1a1a; border: 1px solid rgba(255,255,255,.1);
            color: #fff; padding: 8px 10px; border-radius: 8px; font-size: 14px; }
.ml-input:focus { outline: none; border-color: #daa520; }
.ml-send { background: #daa520; color: #111; border: 0; width: 38px;
           border-radius: 8px; cursor: pointer; display: grid; place-items: center; }
.ml-send:hover { background: #f1b936; }

@media (max-width: 500px) {
  .ml-panel { width: calc(100vw - 24px); height: 70vh; }
  .ml-tip { right: 60px; }
}
</style>`;
