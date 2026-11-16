// Runs the SITE'S OWN JS on fixtures from league_rules.conformance_fixtures()
// and reports where it disagrees with the Python rules in league_rules.py.
//
//   node rules_conformance.mjs <dir holding a copy of the site's js/>  < fixtures.json
//
// Prints {"mismatches": [...]}. league_rules.run_conformance() copies the js
// files (from the working tree or a git ref) into a temp dir and calls this.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const dir = process.argv[2];
const mod = rel => import(pathToFileURL(path.join(dir, rel)).href);
const fx = JSON.parse(readFileSync(0, 'utf8'));
const bad = [];

const kickoff = await mod('js/kickoff.js');
const history = await mod('js/seasonHistory.js');
const cover = await mod('js/coverOdds.js');

// Cover margin: coverOdds.js runs set_winner_on_final's sum on a live row.
const { coverMargin } = cover.makeCoverOdds({ isLive: () => true });
for (const c of fx.covers) {
  const m = coverMargin({ line: c.line, HomePts: c.home, AwayPts: c.away });
  if (m !== c.py_margin) bad.push(`cover margin ${c.home}-${c.away} line ${c.line}: js ${m}, python ${c.py_margin}`);
  const jsWinner = m > 0 ? 'Home' : 'Away';
  if (jsWinner !== c.py_winner) bad.push(`cover winner ${c.home}-${c.away} line ${c.line}: js ${jsWinner}, python ${c.py_winner}`);
}

// Scoring: seasonHistory.isWin is "the leaderboard's rule, verbatim".
for (const p of fx.picks) {
  const js = history.isWin({ pick: p.pick, jackpot: p.jackpot }, p.winner);
  if (js !== p.py) bad.push(`isWin(pick=${p.pick}, winner=${p.winner}, jackpot=${p.jackpot}): js ${js}, python ${p.py}`);
}

// Week lock: kickoff.isWeekLocked over every game of the week.
for (const w of fx.weeks) {
  const now = kickoff.ctToEpochMs(w.now);
  const js = kickoff.isWeekLocked(w.games, now);
  if (js !== w.py) bad.push(`isWeekLocked(${JSON.stringify(w.games)}, ${w.now}): js ${js}, python ${w.py}`);
}

process.stdout.write(JSON.stringify({ mismatches: bad }));
