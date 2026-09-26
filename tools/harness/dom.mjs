// The smallest DOM the felt actually touches. Enough to catch a throw on
// paint, a missing element, or a listener attached to something that isn't
// there - not a browser, just the handful of DOM calls js/blackjack.js makes.
export function installDom() {
  const mk = (tag = 'div') => {
    const el = {
      tagName: tag, _html: '', hidden: false, disabled: false, value: '',
      textContent: '', className: '', dataset: {}, style: {}, children: [],
      _listeners: {},
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = String(v); },
      querySelectorAll(sel) {
        const out = [];
        const attr = /\[data-bj(?:="([a-z]+)")?\]/.exec(sel);
        if (attr) {
          const re = /data-bj="([a-z]+)"/g; let m;
          while ((m = re.exec(this._html))) {
            if (!attr[1] || attr[1] === m[1]) { const b = mk('button'); b.dataset.bj = m[1]; out.push(b); }
          }
        }
        if (/#bj-bet|\.bj-bet/.test(sel) && /id="bj-bet"/.test(this._html)) {
          const i = mk('input'); i.value = '25'; out.push(i);
        }
        if (/bj-stage-inner/.test(sel)) out.push(mk('div'));
        return out;
      },
      querySelector(sel) { return this.querySelectorAll(sel)[0] ?? null; },
      addEventListener(ev, fn) { (this._listeners[ev] ||= []).push(fn); },
      removeEventListener() {},
      appendChild(c) { this.children.push(c); return c; },
      remove() { this._removed = true; },
      classList: { add() {}, remove() {}, contains() { return false; } }
    };
    return el;
  };
  globalThis.document = {
    createElement: mk,
    getElementById: () => mk(),
    body: mk(),
    querySelector: () => null,
    querySelectorAll: () => []
  };
  globalThis.matchMedia = q => ({ matches: /reduce/.test(q) && !!globalThis.__REDUCED });
  const store = {};
  globalThis.localStorage = {
    getItem: k => store[k] ?? null,
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; }
  };
  return { mk, store };
}
