// tools/theme-art/build.mjs - builds css/theme-<name>.css from <name>.tpl.css
// and <name>.art.mjs. The art module's default export maps NAME -> SVG string;
// every __NAME__ in the template becomes that SVG as a data: URI.
//
//   node tools/theme-art/build.mjs            (all themes)
//   node tools/theme-art/build.mjs halloween  (one)
//
// Edit the .tpl.css and .art.mjs files, never the generated css/theme-*.css,
// then run this and `npm run stamp`.
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..', '..');

const enc = s => 'url("data:image/svg+xml,' + s.replace(/\s+/g, ' ').replace(/> </g, '><').trim()
  .replace(/%/g, '%25').replace(/#/g, '%23').replace(/"/g, "'").replace(/</g, '%3C').replace(/>/g, '%3E') + '")';

const names = process.argv.slice(2).length
  ? process.argv.slice(2)
  : fs.readdirSync(here).filter(f => f.endsWith('.tpl.css')).map(f => f.replace('.tpl.css', ''));

for (const name of names) {
  const art = (await import(pathToFileURL(path.join(here, `${name}.art.mjs`)).href)).default;
  let css = fs.readFileSync(path.join(here, `${name}.tpl.css`), 'utf8');
  for (const [k, v] of Object.entries(art)) css = css.replaceAll(`__${k}__`, enc(v));
  const left = css.match(/__[A-Z0-9]+__/g);
  if (left) throw new Error(`${name}: no art for ${[...new Set(left)].join(', ')}`);
  const out = path.join(root, 'css', `theme-${name}.css`);
  fs.writeFileSync(out, css);
  console.log('wrote', path.relative(root, out), css.length);
}
