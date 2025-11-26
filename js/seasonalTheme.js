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

// If multiple windows overlap, earlier entries in HOLIDAY_THEMES win
function detectSeasonalTheme(seasonYear, games) {
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

export function updateSeasonalTheme(seasonYear, games) {
  const themeId = detectSeasonalTheme(seasonYear, games);
  document.body.dataset.theme = themeId || "default";
}