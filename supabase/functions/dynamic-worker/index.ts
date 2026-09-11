// supabase/functions/sync-cfbd-games/index.ts
// deno run --allow-env --allow-net

import { serve } from "https://deno.land/std/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CFBD_BASE = "https://api.collegefootballdata.com";
const ESPN_API =
  "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard";
const TZ = "America/Chicago";

const CFBD_API_KEY = Deno.env.get("CFBD_API_KEY");
const SUPABASE_URL = Deno.env.get("SUPABASE_URL");
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get(
  "SUPABASE_SERVICE_ROLE_KEY",
);

if (!CFBD_API_KEY) throw new Error("Missing CFBD_API_KEY");
if (!SUPABASE_URL) throw new Error("Missing SUPABASE_URL");
if (!SUPABASE_SERVICE_ROLE_KEY) {
  throw new Error("Missing SUPABASE_SERVICE_ROLE_KEY");
}

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// ───────────── helpers ─────────────
function formatCT(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);

  const [mdy, time] = fmt.split(", ");
  const [m, dd, y] = mdy.split("/");
  return `${y}-${m}-${dd}T${time}`;
}

function normalizeCt(ct: string): string {
  return ct.replace(" ", "T").slice(0, 19);
}

function defaultSeasonYear(now = new Date()) {
  const y = now.getFullYear();
  const m = now.getMonth() + 1; // 1-12
  return m < 8 ? y - 1 : y;
}

function nowCtIso(): string {
  return normalizeCt(formatCT(new Date().toISOString())!);
}

function isStartInPastCt(startCt: string | null): boolean {
  if (!startCt) return false;
  return normalizeCt(startCt) < nowCtIso();
}

async function detectCurrentWeekCFBD(year: number): Promise<number> {
  const r = await fetch(`${CFBD_BASE}/calendar?year=${year}`, {
    headers: { Authorization: `Bearer ${CFBD_API_KEY}` },
  });
  if (!r.ok) throw new Error(`CFBD calendar ${r.status}`);
  const cal = await r.json();

  const now = new Date();
  const cur = cal.find((w: any) =>
    w.seasonType === "regular" &&
    new Date(w.startDate) <= now &&
    now <= new Date(w.endDate)
  );

  if (!cur) {
    const post = cal.find((w: any) =>
      w.seasonType === "postseason" &&
      new Date(w.startDate) <= now &&
      now <= new Date(w.endDate)
    );
    if (post?.week) return Number(post.week);

    const started = cal
      .filter((w: any) => new Date(w.startDate) <= now)
      .sort((a: any, b: any) =>
        new Date(b.startDate).getTime() -
        new Date(a.startDate).getTime()
      );

    if (started[0]?.week) return Number(started[0].week);
  }

  return cur?.week ?? 1;
}

async function fetchCFBDBasics(
  year: number,
  week: number,
  seasonType = "both",
) {
  const url = new URL(`${CFBD_BASE}/games`);
  url.searchParams.set("year", String(year));
  url.searchParams.set("seasonType", seasonType);
  url.searchParams.set("division", "fbs");
  if (week) url.searchParams.set("week", String(week));

  const r = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${CFBD_API_KEY}` },
  });
  if (!r.ok) throw new Error(`CFBD /games ${r.status}`);
  const data = await r.json();

  return data.map((g: any) => ({
    GameId: Number(g.id),
    "Start (CT)": formatCT(g.startDate || g.start_date || null),
    Away: (g.awayTeam || g.away_team || "").trim() || null,
    Home: (g.homeTeam || g.home_team || "").trim() || null,
  }));
}

// ── ESPN fetch ──
async function fetchESPNScoreboard(): Promise<any | null> {
  const r = await fetch(ESPN_API, {
    headers: { "cache-control": "no-cache" },
  });
  if (!r.ok) return null;
  return await r.json();
}

function detectWeekFromESPNPayload(data: any): number | null {
  const wk = data?.week?.number;
  if (wk != null && !Number.isNaN(Number(wk))) return Number(wk);

  const wk2 = data?.leagues?.[0]?.week?.number;
  if (wk2 != null && !Number.isNaN(Number(wk2))) return Number(wk2);

  return null;
}

// ── ESPN overlay (NO POINTS) ──
function mapState(state: string | undefined): string {
  return state === "post"
    ? "final"
    : state === "in" || state === "in_progress"
    ? "in_progress"
    : "scheduled";
}

function pickBroadcast(comp: any): string | null {
  const arr = comp?.broadcasts ?? comp?.geoBroadcasts ?? [];
  if (!Array.isArray(arr) || !arr.length) return null;

  const tv = arr.find((b: any) =>
    b?.type?.shortName === "TV"
  );

  const best = tv ?? arr[0];
  return best?.shortName ||
    best?.media?.shortName ||
    (Array.isArray(best?.names) ? best.names[0] : null) ||
    null;
}

function pickNum(...vals: any[]): number | null {
  for (const v of vals) {
    if (v != null && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
}

async function overlayESPN(rows: any[], espnData: any | null) {
  if (!espnData) return rows;

  const events = espnData?.events ?? [];
  const byId = new Map(rows.map((r) => [String(r.GameId), r]));
  const nowIso = new Date().toISOString();

  for (const ev of events) {
    const comp = ev?.competitions?.[0];
    if (!comp) continue;

    const row = byId.get(String(comp.id));
    if (!row) continue;

    row.Status = mapState(comp?.status?.type?.state) ?? row.Status ?? null;
    row.Period = pickNum(comp?.status?.period) ?? row.Period ?? null;
    row.Clock = comp?.status?.displayClock ?? row.Clock ?? null;
    row.Network = pickBroadcast(comp) ?? row.Network ?? null;
    row.espn_url =
      ev?.links?.find((l: any) => l?.rel?.includes("summary"))?.href ??
        ev?.link ??
        row.espn_url ??
        null;

    if (row.Status === "final" && !row.FinalizedAt) {
      row.FinalizedAt = nowIso;
    }
  }

  return Array.from(byId.values());
}

const coalesce = (v: any) => {
  if (v === undefined || v === "") return null;
  if (typeof v === "string") return v.trim() || null;
  return v;
};

function differOn(fields: string[], a: any, b: any) {
  for (const k of fields) {
    if (coalesce(a[k]) !== coalesce(b[k])) return true;
  }
  return false;
}

async function loadExisting(ids: number[]) {
  if (!ids.length) return new Map<number, any>();

  const { data, error } = await supabase
    .from("all_games")
    .select(
      `GameId,"Start (CT)",Away,Home,Status,Period,Clock,Network,espn_url,FinalizedAt`,
    )
    .in("GameId", ids);

  if (error) throw error;

  return new Map((data ?? []).map((r: any) => [Number(r.GameId), r]));
}

async function filterChanged(
  rows: any[],
  fields: string[],
  existing?: Map<number, any>,
) {
  const ids = rows.map((r) => r.GameId);
  const prevMap = existing ?? await loadExisting(ids);

  const nowIso = new Date().toISOString();
  const toUpsert: any[] = [];

  for (const row of rows) {
    const prev = prevMap.get(row.GameId);

    if (!prev) {
      // 🚫 Skip new inserts if start time already passed (CT)
      if (isStartInPastCt(row["Start (CT)"] ?? null)) continue;

      toUpsert.push({ ...row, SourceUpdatedAt: nowIso });
      continue;
    }

    if (differOn(fields, row, prev)) {
      toUpsert.push({ ...row, SourceUpdatedAt: nowIso });
    }
  }

  return toUpsert;
}

// ───────────── HTTP ─────────────
serve(async (req) => {
  try {
    const u = new URL(req.url);

    // ✅ Forced YEAR + WEEK together at the top
    const yearParam = u.searchParams.get("year");
    const forcedYear = yearParam ? Number(yearParam) : null;
    const year = forcedYear ?? defaultSeasonYear();
    const yearSource = forcedYear != null ? "forced" : "default";

    const weekParam = u.searchParams.get("week");
    const forcedWeek = weekParam ? Number(weekParam) : null;

    const seasonType = u.searchParams.get("seasonType") ?? "both";
    const overlayEspn = u.searchParams.get("overlayEspn") === "true";

    const espnData = forcedWeek == null
      ? await fetchESPNScoreboard()
      : null;
    const espnWeek = forcedWeek == null
      ? detectWeekFromESPNPayload(espnData)
      : null;

    const weekSource =
      forcedWeek != null ? "forced" : espnWeek != null ? "espn" : "cfbd";

    const weekToUse =
      forcedWeek ?? espnWeek ?? await detectCurrentWeekCFBD(year);

    const cfbdBasics = await fetchCFBDBasics(
      year,
      weekToUse,
      seasonType,
    );

    const existing = await loadExisting(
      cfbdBasics.map((r: any) => r.GameId),
    );

    const rows = overlayEspn
      ? await overlayESPN(
        cfbdBasics,
        espnData ?? await fetchESPNScoreboard(),
      )
      : cfbdBasics;

    const cfbdFields = ["Start (CT)", "Away", "Home"];
    const espnFields = [
      "Status",
      "Period",
      "Clock",
      "Network",
      "espn_url",
      "FinalizedAt",
    ];

    const ownedFields = overlayEspn ? espnFields : cfbdFields;

    const changed = await filterChanged(rows, ownedFields, existing);

    if (changed.length) {
      const { error } = await supabase.from("all_games").upsert(changed, {
        onConflict: "GameId",
      });
      if (error) throw error;
    }

    return new Response(
      JSON.stringify(
        {
          ok: true,
          year,
          yearSource, // ✅ new
          seasonType,
          weekUsed: weekToUse,
          weekSource,
          espnWeek,
          overlayEspn,
          upserts: changed.length,
          scanned: rows.length,
        },
        null,
        2,
      ),
      { headers: { "content-type": "application/json" } },
    );
  } catch (e) {
    console.error(e);
    return new Response(
      JSON.stringify({ ok: false, error: e?.message ?? String(e) }),
      { status: 500, headers: { "content-type": "application/json" } },
    );
  }
});
