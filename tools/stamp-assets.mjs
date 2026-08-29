// Put a content hash on every local stylesheet link.
//
// WHY THIS EXISTS. GitHub Pages serves this site with Cache-Control: max-age=600
// and the stylesheets were linked with no version on them. index.html and
// css/base.css therefore expire independently, so for up to ten minutes after a
// deploy a browser can hold one of them and not the other - and half a deploy is
// worse than none of it. It bit us on the night: index.html shipped a dot that
// asks for .poss-dot.is-covered while the cached base.css had never heard of the
// class, so the dot that should have been green rendered white. Nothing was
// wrong with either file.
//
// A hash in the query string ends it. The HTML is the only thing that has to
// expire, and when it does it names the exact stylesheet it was built against.
// Unchanged files keep their hash and stay cached; a changed one is a new URL
// and cannot be served stale.
//
// Run by `npm run build`, and checked in CI, so it cannot be forgotten - which
// matters because forgetting it fails silently and only on other people's
// machines.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const hashes = new Map();

const hashOf = (rel) => {
  if (!hashes.has(rel)) {
    let h;
    try {
      h = createHash('sha256').update(readFileSync(join(root, rel))).digest('hex').slice(0, 8);
    } catch {
      h = null;   // a link to something that is not in the repo: leave it alone
    }
    hashes.set(rel, h);
  }
  return hashes.get(rel);
};

// href="./css/base.css" and href="/Pickem/css/base.css", with or without an
// existing ?v=. Only local .css - Google Fonts and anything else absolute is
// somebody else's cache to manage.
const LINK = /href="((?:\.\/|\/Pickem\/)([^"?]+\.css))(?:\?v=[^"]*)?"/g;

let changed = 0;
const files = readdirSync(root).filter(f => f.endsWith('.html'));
for (const f of files) {
  const before = readFileSync(join(root, f), 'utf8');
  const after = before.replace(LINK, (m, href, rel) => {
    const h = hashOf(rel);
    return h ? `href="${href}?v=${h}"` : m;
  });
  if (after !== before) { writeFileSync(join(root, f), after); changed++; }
}
console.log(`stamped ${files.length} html file(s), ${changed} changed`);
for (const [rel, h] of hashes) console.log(`  ${rel} ${h ?? '(missing, left alone)'}`);
