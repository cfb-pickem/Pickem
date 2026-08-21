# CFB Pick'em

College football pick'em league site — leaderboard, weekly picks, tiebreakers, and season stats.

**Live:** https://cfb-pickem.github.io/Pickem/

A static site (plain HTML + ES modules + Tailwind) served directly from GitHub Pages off the
`main` branch. Data lives in Supabase and is read from the browser with the public anon key.

---

## Local development

Because the pages use ES modules (`<script type="module">`), you can't open them with `file://` —
you need a local server:

```bash
npx serve .
# then open http://localhost:3000
```

### Styles

Tailwind is **prebuilt**, not loaded from a CDN. `css/tailwind.css` is committed and served as-is.

After adding or changing any Tailwind class — in an `.html` file *or* inside a JS template
literal in `js/` — regenerate it:

```bash
npm install     # first time only
npm run css     # rebuild css/tailwind.css
npm run css:watch   # or rebuild continuously while working
```

`tailwind.config.js` scans `./*.html` and `./js/**/*.js`, so classes built in JS strings
(for example in `js/nav.js`) are picked up.

> CI (`.github/workflows/css-check.yml`) fails if `css/tailwind.css` doesn't match a fresh
> build, so a forgotten rebuild can't silently ship broken styling.

### Images

Images are committed as WebP, sized for how they're actually displayed. If you add a new
mascot, downscale it first rather than committing a full-resolution export — the mascots
render at ≤180px wide, so ~400px is plenty:

```bash
npx sharp-cli -i new-mascot.png -o mascots/new-mascot.webp resize 400 -- webp --quality 82
```

Then add the filename to `MASCOT_IMAGES` in `js/mascots.js`.

---

## Layout

| Path | What it is |
| --- | --- |
| `index.html` | Leaderboard — everyone's picks, points, live scores |
| `picks.html` | Make picks (auth required, locks at first kickoff) |
| `tiebreakers.html` | Tiebreaker picks and results |
| `stats.html` | Season stats — accuracy, perfect weeks, favorites, underdogs |
| `cfb-genius.html` | League rules, scoring, reigning champion |
| `commissioner.html` | Commissioner tools (games, lines, scores, seeds) |
| `signin.html` / `claim-team.html` / `reset.html` | Auth flow |
| `404.html` | Served by GitHub Pages for unknown URLs |
| `js/` | Shared modules — Supabase client, nav, utils, mascots, seasonal themes |
| `css/base.css` | Design tokens and components |
| `css/theme-*.css` | Halloween / Thanksgiving / Christmas overrides, applied by `js/seasonalTheme.js` |
| `css/tailwind.css` | **Generated** — do not edit by hand |

---

## Deployment

Push to `main`. GitHub Pages serves the repository root — there is no deploy step.

Make sure `css/tailwind.css` is rebuilt and committed alongside any markup change.

---

## Supabase

The anon key in `js/supabaseClient.js` is public by design; it's safe to commit **only because
Row Level Security is enabled on every table**. RLS is what actually stops someone from
rewriting picks or scores — the UI's lock logic is convenience, not enforcement.

If you add a table, enable RLS on it before shipping:

```sql
alter table public.<name> enable row level security;
```

To audit what's currently exposed:

```sql
-- any table with rowsecurity = false is publicly writable
select schemaname, tablename, rowsecurity
from pg_tables
where schemaname = 'public'
order by rowsecurity, tablename;

-- review the actual policies
select tablename, policyname, cmd, roles, qual, with_check
from pg_policies
where schemaname = 'public'
order by tablename, cmd;
```
