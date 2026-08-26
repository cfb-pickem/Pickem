// js/leagueHistory.js — seasons that predate this site.
//
// 2022, 2023 and 2024 were played on Splash Sports (formerly RunYourPool) and
// none of it lives in Supabase. What survives is the Weekly Results grid: wins
// per player per week, and a year-to-date total.
//
// WHAT THIS IS NOT. It is a count of wins, not a record of picks - there is no
// way to know which side of a spread anyone took. So it cannot train
// js/scoutModel.js, which needs the side, and it is deliberately not wired into
// it. It is history for people to read, and that is all it can honestly be.
//
// Transcribed from screenshots of the Splash report, so each row was checked by
// summing its weeks against the YTD column the report prints: all 25 rows
// matched. Players who have since left the league are left out on purpose -
// Skyler, Crotty Kid, Sam, Talbot Jacobs, DJ and John Lipka all appear in the
// source and none of them are here.
//
// Names are the Splash entry names, mapped to current teams by NAME_ALIASES
// below, since a couple were spelled differently there - and one player has
// gone by more than one entry name across seasons.
//
// KNOWN GAP: the 2023 screenshot this came from was cut off. 2022 and 2024 both
// ended with "Showing 1 to 10 of 10 entries" so they are complete; 2023 showed
// twelve rows with no such footer, so there are probably more below the fold.
// Anyone missing from 2023 below is missing for that reason, not because they
// did not play.

export const HISTORY_WEEKS = 15;

/** season -> entry name -> wins in weeks 1..15 */
export const LEAGUE_HISTORY = {
  2024: {
    'Breaking Horrendous': [4, 2, 3, 1, 3, 1, 2, 4, 3, 3, 2, 0, 1, 2, 4],
    'DaddyFreeman':        [3, 2, 3, 0, 1, 1, 2, 1, 3, 1, 1, 1, 1, 4, 0],
    'Wedge Mulshine':      [3, 3, 3, 2, 3, 1, 2, 1, 2, 2, 2, 1, 1, 1, 2],
    'Andrew Bub':          [2, 3, 2, 1, 2, 2, 0, 2, 2, 2, 3, 2, 0, 2, 3],
    'Chuck P':             [2, 3, 2, 2, 3, 1, 1, 0, 1, 2, 2, 3, 1, 1, 2],
    'Clam':                [2, 2, 2, 3, 2, 1, 1, 3, 4, 1, 2, 2, 2, 1, 2],
    'Kevin Culligan':      [2, 1, 1, 3, 2, 0, 2, 3, 2, 1, 2, 2, 2, 0, 0],
    'Quinning':            [2, 3, 2, 1, 4, 2, 1, 3, 1, 3, 4, 2, 0, 3, 2],
    'Liam Bryson':         [1, 3, 3, 1, 3, 0, 0, 1, 4, 1, 1, 0, 3, 2, 3],
    'Steve Drolet':        [1, 2, 1, 1, 1, 3, 3, 2, 2, 2, 2, 2, 1, 3, 2],
  },
  2023: {
    'Clam':                [3, 3, 1, 2, 2, 1, 2, 0, 1, 1, 4, 2, 3, 4, 0],
    'Brian':               [2, 2, 1, 3, 3, 2, 1, 2, 1, 2, 0, 2, 3, 2, 1],
    'Chuck P':             [2, 1, 2, 2, 1, 0, 3, 2, 2, 1, 3, 4, 4, 3, 0],
    'Liam Bryson':         [2, 3, 1, 0, 2, 1, 1, 1, 4, 3, 2, 3, 3, 0, 0],
    'Quinning':            [2, 3, 0, 3, 1, 0, 2, 1, 3, 2, 2, 4, 2, 1, 0],
    'Andrew Bub':          [1, 3, 1, 1, 3, 2, 3, 0, 2, 3, 2, 3, 1, 2, 0],
    'DaddyFreeman':        [1, 2, 4, 2, 1, 1, 3, 2, 3, 3, 4, 3, 4, 2, 0],
    'Wedge Mulshine':      [1, 3, 2, 1, 0, 3, 0, 0, 2, 2, 2, 3, 2, 2, 0],
  },
  2022: {
    'Brian':               [1, 2, 3, 1, 1, 1, 3, 3, 1, 3, 1, 3, 3, 3, 0],
    'Liam Bryson':         [1, 2, 1, 2, 3, 1, 2, 2, 3, 1, 3, 1, 3, 4, 0],
    'Chuck P':             [1, 2, 1, 2, 4, 3, 1, 3, 2, 2, 2, 1, 1, 2, 1],
    'Andrew Bub':          [2, 1, 2, 2, 2, 4, 0, 2, 2, 0, 2, 3, 0, 2, 0],
    'Kevin Culligan':      [2, 0, 3, 0, 3, 2, 4, 1, 2, 1, 0, 1, 3, 1, 0],
    'Clam':                [3, 1, 1, 1, 3, 4, 1, 0, 1, 1, 2, 1, 1, 2, 0],
    'DaddyFreeman':        [0, 1, 2, 2, 3, 1, 1, 2, 1, 1, 2, 1, 0, 1, 0],
  },
};

// Splash entry name -> team_name in this database, where they differ. Someone
// who renamed their entry between seasons still needs their history to add up,
// so match on every name they have played under.
const NAME_ALIASES = {
  'kevinculligan': 'Kevin Culligan',
  'pancho':        'Breaking Horrendous',
  'pomcho':        'Breaking Horrendous',
};

const norm = s => String(s || '').trim().toLowerCase().replace(/\s+/g, '');

/** Wins for one team in one historical season, or null if they did not play. */
export function historyFor(teamName, season) {
  const table = LEAGUE_HISTORY[season];
  if (!table) return null;
  const want = norm(teamName);
  for (const entry of Object.keys(table)) {
    const e = norm(entry);
    if (e === want || norm(NAME_ALIASES[e] || '') === want) {
      const weeks = table[entry];
      return { weeks, total: weeks.reduce((a, b) => a + b, 0) };
    }
  }
  return null;
}

/** Seasons held here, newest first. */
export const HISTORY_SEASONS = Object.keys(LEAGUE_HISTORY)
  .map(Number).sort((a, b) => b - a);
