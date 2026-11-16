// js/seasonalTheme.js

// --- date helpers ---

function getThanksgivingDate(seasonYear) {
  // 4th Thursday of November
  const nov1 = new Date(seasonYear, 10, 1); // month 10 = November
  const day = nov1.getDay();                // 0=Sun..6=Sat
  const offsetToThu = (4 - day + 7) % 7;
  const fourthThuDate = 1 + offsetToThu + 21;
  return new Date(seasonYear, 10, fourthThuDate);
}

function getHalloweenDate(seasonYear) {
  return new Date(seasonYear, 9, 31); // Oct 31
}

function getChristmasDate(seasonYear) {
  return new Date(seasonYear, 11, 25); // Dec 25
}

// Inclusive window helper: offset in days relative to the holiday date
function anyGameInWindow(games, centerDate, startOffsetDays, endOffsetDays) {
  if (!games || !games.length) return false;

  const start = new Date(centerDate);
  start.setDate(centerDate.getDate() + startOffsetDays);

  const end = new Date(centerDate);
  end.setDate(centerDate.getDate() + endOffsetDays);

  return games.some(g => {
    const raw = g && g["Start (CT)"];
    if (!raw) return false;
    const ts = new Date(raw);
    if (Number.isNaN(ts.getTime())) return false;
    return ts >= start && ts <= end;
  });
}

const HOLIDAY_THEMES = [
  {
    id: "halloween",
    getDate: getHalloweenDate,
    startOffsetDays: -4,
    endOffsetDays: 3,
  },
  {
    id: "thanksgiving",
    getDate: getThanksgivingDate,
    startOffsetDays: -4,
    endOffsetDays: 3,
  },
  {
    id: "christmas",
    getDate: getChristmasDate,
    startOffsetDays: -4,
    endOffsetDays: 3,
  },
];

// Themed weeks that aren't holidays: the league calls them, not the calendar.
// Matched on the week being shown, and they win over any holiday window.
const SPECIAL_WEEKS = [
  // Sickos week: Wake Forest @ SMU, Oregon State @ Boise, North Texas @ UTSA,
  // and North Dakota State at Hawai'i at 10pm Central. Yes... ha ha ha... YES.
  { season: 2026, week: 11, id: "sickos" },
];

function specialWeekTheme(seasonYear, games) {
  if (!games || !games.length) return null;
  const weeks = new Set(games.map(g => Number(g && g.week)));
  const hit = SPECIAL_WEEKS.find(s => s.season === Number(seasonYear) && weeks.has(s.week));
  if (!hit) return null;
  // The board keeps showing a week until the next week's games post, so a themed
  // week ends at 6am CT the day after its last kickoff (Charlie, 2026-11-16).
  const starts = games
    .filter(g => Number(g && g.week) === hit.week && g["Start (CT)"])
    .map(g => new Date(g["Start (CT)"]).getTime())
    .filter(t => !Number.isNaN(t));
  if (starts.length) {
    const end = new Date(Math.max(...starts));
    end.setDate(end.getDate() + 1);
    end.setHours(6, 0, 0, 0);
    if (Date.now() > end.getTime()) return null;
  }
  return hit.id;
}

// If multiple windows overlap, earlier entries in HOLIDAY_THEMES win
function detectSeasonalTheme(seasonYear, games) {
  const special = specialWeekTheme(seasonYear, games);
  if (special) return special;
  for (const h of HOLIDAY_THEMES) {
    const date = h.getDate(seasonYear);
    const inWindow = anyGameInWindow(
      games,
      date,
      h.startOffsetDays,
      h.endOffsetDays
    );
    if (inWindow) return h.id;
  }
  return null;
}

// Only the leaderboard and tiebreakers have the game list needed to work out
// whether a holiday window is actually live. The result is remembered so every
// other page can dress itself the same way without refetching games - otherwise
// the season would stop at the edge of two pages.
const THEME_KEY = "cfb_theme_v1";

export function updateSeasonalTheme(seasonYear, games) {
  const themeId = detectSeasonalTheme(seasonYear, games) || "default";
  document.body.dataset.theme = themeId;
  // Also on <html>, matching what the pre-paint applier on other pages sets.
  if (themeId === "default") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", themeId);
  try {
    localStorage.setItem(THEME_KEY, themeId);
  } catch {
    /* private mode - the theme just won't carry to other pages */
  }
}