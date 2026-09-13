// js/slotsound.js — the noise a slot machine makes, synthesised.
//
// WHY THERE ARE NO AUDIO FILES. Every sound below is built out of oscillators
// and a noise buffer at runtime. A handful of WAVs would have been less code and
// would also have been a few hundred KB on a page whose whole job is a table,
// downloaded by everybody including the people who never turn sound on. This
// module is a few KB and costs nothing until somebody asks for it.
//
// OFF BY DEFAULT, AND THAT IS NOT TIMIDITY. The reveal plays on page load, and a
// leaderboard that starts making casino noises at you because you opened it is
// hostile. Browsers agree: audio started without a user gesture is blocked
// outright, so an on-by-default setting would mostly produce silence plus a
// console error. So it is opt-in, it persists, and the first gesture after that
// is what actually unlocks the context.
//
// EVERYTHING IS BEST-EFFORT. There is no Web Audio on some browsers, the context
// can be refused, a tab can be throttled. Not one line here is allowed to break
// a spin: every entry point is wrapped, and failure means silence.

const KEY = 'cfb-slots-sound';

let ctx = null;
let master = null;
let enabled = read();

function read() {
  try { return localStorage.getItem(KEY) === '1'; } catch { return false; }
}

export function soundEnabled() { return enabled; }

export function setSoundEnabled(on) {
  enabled = !!on;
  try { localStorage.setItem(KEY, enabled ? '1' : '0'); } catch {}
  if (enabled) unlock();
  return enabled;
}

/**
 * Make or resume the audio context. Safe to call on every gesture.
 *
 * A context created before a user gesture starts in state "suspended" and stays
 * there, so this is called again on the click that turns sound on rather than
 * only once at import.
 */
export function unlock() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!ctx) {
      ctx = new AC();
      master = ctx.createGain();
      master.gain.value = 0.22;        // the whole thing sits well under the page
      master.connect(ctx.destination);
    }
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    return ctx;
  } catch { return null; }
}

function live() {
  if (!enabled) return null;
  const c = unlock();
  return c && c.state === 'running' ? c : null;
}

// One second of white noise, made once and reused. Clicks, thunks and the burst
// are all this buffer through different filters and envelopes - which is also
// how the real thing works, acoustically.
let noiseBuf = null;
function noise(c) {
  if (noiseBuf && noiseBuf.sampleRate === c.sampleRate) return noiseBuf;
  const n = c.sampleRate;
  noiseBuf = c.createBuffer(1, n, n);
  const d = noiseBuf.getChannelData(0);
  for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
  return noiseBuf;
}

function env(c, at, peak, attack, decay) {
  const g = c.createGain();
  g.gain.setValueAtTime(0.0001, at);
  g.gain.exponentialRampToValueAtTime(Math.max(peak, 0.0002), at + attack);
  g.gain.exponentialRampToValueAtTime(0.0001, at + attack + decay);
  return g;
}

/**
 * A reel detent. The click a mechanical reel makes as a symbol passes the stop.
 *
 * `at` is an absolute context time, because these are scheduled ALL AT ONCE at
 * the start of a spin rather than fired from timers. A decelerating click track
 * driven by setTimeout drifts audibly - and drift is the exact thing that stops
 * it sounding mechanical.
 */
export function scheduleClick(at, gain = 1) {
  const c = live(); if (!c) return;
  try {
    const t = c.currentTime + at;
    const src = c.createBufferSource();
    src.buffer = noise(c);
    src.playbackRate.value = 1.6;
    const bp = c.createBiquadFilter();
    bp.type = 'bandpass';
    bp.frequency.value = 2400;
    bp.Q.value = 7;
    const g = env(c, t, 0.5 * gain, 0.001, 0.028);
    src.connect(bp).connect(g).connect(master);
    src.start(t); src.stop(t + 0.05);
  } catch {}
}

/** The reel coming to rest: a low wooden thud with a click on top of it. */
export function scheduleStop(at) {
  const c = live(); if (!c) return;
  try {
    const t = c.currentTime + at;
    const o = c.createOscillator();
    o.type = 'sine';
    o.frequency.setValueAtTime(180, t);
    o.frequency.exponentialRampToValueAtTime(52, t + 0.16);
    const g = env(c, t, 0.85, 0.004, 0.2);
    o.connect(g).connect(master);
    o.start(t); o.stop(t + 0.3);
    scheduleClick(at, 1.3);
  } catch {}
}

// ---------------------------------------------------------------------------
// THE STRAIN. One held drone that climbs while the football is being stretched,
// which is the sound every machine in the world uses to tell you something is
// about to happen. Kept as module state because it has to be stoppable from a
// different beat than the one that started it.

let strainNodes = null;

export function strainStart(seconds) {
  const c = live(); if (!c) return;
  strainStop();
  try {
    const t = c.currentTime;
    const o = c.createOscillator();
    o.type = 'sawtooth';
    o.frequency.setValueAtTime(110, t);
    // Climbing the whole way, so that where it has got to IS how close the ball
    // is. The listener works that out in about two seconds and never loses it.
    o.frequency.exponentialRampToValueAtTime(660, t + seconds);

    const lp = c.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.setValueAtTime(700, t);
    lp.frequency.exponentialRampToValueAtTime(5200, t + seconds);
    lp.Q.value = 6;

    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.16, t + 0.5);
    g.gain.exponentialRampToValueAtTime(0.30, t + seconds);

    o.connect(lp).connect(g).connect(master);
    o.start(t);
    strainNodes = { o, g };
  } catch {}
}

export function strainStop() {
  if (!strainNodes || !ctx) return;
  try {
    const { o, g } = strainNodes;
    const t = ctx.currentTime;
    g.gain.cancelScheduledValues(t);
    g.gain.setValueAtTime(Math.max(g.gain.value, 0.0002), t);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
    o.stop(t + 0.2);
  } catch {}
  strainNodes = null;
}

/** It held. The drone gives up and the air goes out of it. */
export function hold() {
  const c = live(); if (!c) return;
  strainStop();
  try {
    const t = c.currentTime;
    const src = c.createBufferSource();
    src.buffer = noise(c);
    const bp = c.createBiquadFilter();
    bp.type = 'bandpass';
    bp.frequency.setValueAtTime(1800, t);
    bp.frequency.exponentialRampToValueAtTime(320, t + 0.7);   // a descending hiss
    bp.Q.value = 2;
    const g = env(c, t, 0.32, 0.02, 0.75);
    src.connect(bp).connect(g).connect(master);
    src.start(t); src.stop(t + 0.9);
  } catch {}
}

/** It went. A burst, then the bell cascade every machine pays out to. */
export function pop() {
  const c = live(); if (!c) return;
  strainStop();
  try {
    const t = c.currentTime;

    // The burst: full-band noise, gone in a quarter of a second.
    const src = c.createBufferSource();
    src.buffer = noise(c);
    const hp = c.createBiquadFilter();
    hp.type = 'highpass';
    hp.frequency.value = 300;
    const g = env(c, t, 1.4, 0.002, 0.28);
    src.connect(hp).connect(g).connect(master);
    src.start(t); src.stop(t + 0.4);

    // A low thump under it, so it has some weight.
    const o = c.createOscillator();
    o.type = 'sine';
    o.frequency.setValueAtTime(220, t);
    o.frequency.exponentialRampToValueAtTime(38, t + 0.35);
    o.connect(env(c, t, 1.1, 0.004, 0.4)).connect(master);
    o.start(t); o.stop(t + 0.5);

    // The payout: a rising major arpeggio, rung twice, the way a machine keeps
    // going long after you know you have won.
    const notes = [523.25, 659.25, 783.99, 1046.5, 1318.5, 1568, 2093];
    notes.forEach((f, i) => bell(c, t + 0.24 + i * 0.105, f, 0.5));
    notes.slice(0, 5).forEach((f, i) => bell(c, t + 1.2 + i * 0.085, f * 1.5, 0.34));
  } catch {}
}

/** One bell: two detuned sines and a fast decay, which is most of a chime. */
function bell(c, t, freq, peak) {
  try {
    const g = env(c, t, peak, 0.004, 0.55);
    g.connect(master);
    for (const [mult, level] of [[1, 1], [2.01, 0.32], [3.02, 0.12]]) {
      const o = c.createOscillator();
      o.type = 'sine';
      o.frequency.value = freq * mult;
      const gg = c.createGain();
      gg.gain.value = level;
      o.connect(gg).connect(g);
      o.start(t); o.stop(t + 0.6);
    }
  } catch {}
}

/** The lever. A mechanical clunk, for the sandbox's Roll and nothing else yet. */
export function lever() {
  const c = live(); if (!c) return;
  try {
    const t = c.currentTime;
    const o = c.createOscillator();
    o.type = 'square';
    o.frequency.setValueAtTime(300, t);
    o.frequency.exponentialRampToValueAtTime(80, t + 0.12);
    const lp = c.createBiquadFilter();
    lp.type = 'lowpass'; lp.frequency.value = 1400;
    o.connect(lp).connect(env(c, t, 0.6, 0.003, 0.18)).connect(master);
    o.start(t); o.stop(t + 0.25);
  } catch {}
}
