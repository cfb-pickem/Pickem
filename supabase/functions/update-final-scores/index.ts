// supabase/functions/update-final-scores/index.ts
import { serve } from "https://deno.land/std/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const ESPN_API =
  "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard";

const toInt = (v: unknown) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
};

const mapState = (s: string | undefined) =>
  s === "post"
    ? "final"
    : s === "in" || s === "in_progress"
    ? "in_progress"
    : "scheduled";

// Prefer a TV broadcast short name; fall back to first available
function pickBroadcast(comp: any) {
  try {
    const arr = comp?.broadcasts ?? comp?.geoBroadcasts ?? [];
    if (!Array.isArray(arr) || !arr.length) return null;

    const tv = arr.find(
      (b: any) =>
        b?.type?.shortName === "TV" &&
        (b?.shortName ||
          b?.media?.shortName ||
          (b?.names && b?.names[0])),
    );
    const best = tv ?? arr[0];

    return (
      best?.shortName ||
      best?.media?.shortName ||
      (Array.isArray(best?.names) ? best.names[0] : null) ||
      null
    );
  } catch {
    return null;
  }
}

// Normalize undefined to null for stable comparisons
const norm = (v: any) => (v === undefined ? null : v);

// Compute the list of reasons a row needs updating (business fields only)
function diffReasons(prev: any, next: any) {
  const reasons: string[] = [];

  if (norm(prev?.Status) !== norm(next.Status)) reasons.push("status");
  if (norm(prev?.HomePts) !== norm(next.HomePts)) reasons.push("home");
  if (norm(prev?.AwayPts) !== norm(next.AwayPts)) reasons.push("away");
  if (norm(prev?.Period) !== norm(next.Period)) reasons.push("period");
  if (norm(prev?.Clock) !== norm(next.Clock)) reasons.push("clock");
  if (norm(prev?.Network) !== norm(next.Network)) reasons.push("network");
  if (norm(prev?.espn_url) !== norm(next.espn_url)) reasons.push("url");
  // only count FinalizedAt when it is being newly set
  if (next.FinalizedAt && !prev?.FinalizedAt) reasons.push("finalizedAt");

  return reasons;
}

serve(async (req: Request) => {
  const url = new URL(req.url);
  const debugMode = url.searchParams.get("debug") === "1";

  console.log("Fetching ESPN scoreboard:", ESPN_API);
  const res = await fetch(ESPN_API, {
    headers: {
      "cache-control": "no-cache",
    },
  });

  if (!res.ok) {
    console.error("ESPN fetch failed with status", res.status);
    return new Response("ESPN fetch failed", { status: 502 });
  }

  const data = await res.json();
  const events = data?.events ?? [];
  const sourceTs = new Date().toISOString();

  // Top-level ESPN diagnostics
  console.log("ESPN raw keys:", Object.keys(data || {}));
  console.log("ESPN events count:", events.length);

  // Sample event for logs
  if (events.length > 0) {
    const sample = events[0];
    const comp0 = sample?.competitions?.[0];
    console.log(
      "Sample event:",
      JSON.stringify(
        {
          id: sample?.id,
          competitions: sample?.competitions?.length ?? 0,
          comp0: {
            id: comp0?.id,
            status: comp0?.status,
            competitors: comp0?.competitors?.map((c: any) => ({
              homeAway: c.homeAway,
              score: c.score,
              team: c.team?.displayName,
            })),
          },
        },
        null,
        2,
      ),
    );
  }

  // 🔍 DEBUG MODE: just return what ESPN is sending (compact view)
  if (debugMode) {
    const debugPayload = {
      events: events.map((ev: any) => {
        const comp = ev?.competitions?.[0];
        const home = comp?.competitors?.find(
          (c: any) => c.homeAway === "home",
        );
        const away = comp?.competitors?.find(
          (c: any) => c.homeAway === "away",
        );

        return {
          gameId: comp?.id,
          state: comp?.status?.type?.state,
          description: comp?.status?.type?.description,
          detail: comp?.status?.type?.detail,
          displayClock: comp?.status?.displayClock,
          period: comp?.status?.period,
          home: {
            team: home?.team?.displayName,
            abbrev: home?.team?.abbreviation,
            score: home?.score,
          },
          away: {
            team: away?.team?.displayName,
            abbrev: away?.team?.abbreviation,
            score: away?.score,
          },
        };
      }),
    };

    return new Response(JSON.stringify(debugPayload, null, 2), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Normal mode: update DB (no inserts)
  const counters: Record<string, number> = {
    status: 0,
    home: 0,
    away: 0,
    period: 0,
    clock: 0,
    network: 0,
    url: 0,
    finalizedAt: 0,
    total: 0,
  };

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  // Read all columns we compare or set
  const { data: existing, error: exErr } = await supabase
    .from("all_games")
    .select(
      "GameId, Status, HomePts, AwayPts, Period, Clock, Network, espn_url, FinalizedAt",
    );

  if (exErr) {
    console.error("DB read error:", exErr);
    return new Response("DB read error", { status: 500 });
  }

  console.log(
    "Loaded existing games from all_games:",
    existing?.length ?? 0,
  );

  const current = new Map<number, any>(
    (existing ?? []).map((r: any) => [Number(r.GameId), r]),
  );

  const updates: any[] = [];

  for (const ev of events) {
    const comp = ev?.competitions?.[0];
    if (!comp) {
      console.log("Skipping event with no competitions:", ev?.id);
      continue;
    }

    const gameId = toInt(comp?.id);
    if (!gameId) {
      console.log(
        "Skipping competition with non-numeric or missing id:",
        comp?.id,
      );
      continue;
    }

    const home = comp?.competitors?.find(
      (c: any) => c.homeAway === "home",
    );
    const away = comp?.competitors?.find(
      (c: any) => c.homeAway === "away",
    );
    if (!home || !away) {
      console.log(
        "Skipping GameId",
        gameId,
        "missing home or away competitor",
      );
      continue;
    }

    const prev = current.get(gameId);

    if (!prev) {
      console.log(
        "Skipping GameId",
        gameId,
        "- no existing row in all_games (no inserts allowed)",
      );
      continue;
    }

    const nextStatus = mapState(comp?.status?.type?.state);
    const nextHome = toInt(home?.score);
    const nextAway = toInt(away?.score);
    const period = toInt(comp?.status?.period);
    const clock =
      comp?.status?.displayClock ??
      comp?.status?.type?.detail ??
      null;
    const network = pickBroadcast(comp);

    // ESPN link (raw from API)
    const rawEspnUrl =
      ev?.links?.find((l: any) => l?.rel?.includes("summary"))?.href ??
      ev?.link ??
      null;

    // Preserve previous non-blank URL if the new one is blank/null
    let finalEspnUrl: string | null = rawEspnUrl ?? null;
    if (
      prev.espn_url && // previously had a URL
      (!rawEspnUrl || rawEspnUrl.trim() === "") // new value is blank/empty
    ) {
      finalEspnUrl = prev.espn_url; // keep old URL
    }

    // Never regress a final to anything else
    if (prev.Status === "final" && nextStatus !== "final") {
      console.log(
        "Skipping GameId",
        gameId,
        "because it is already final and nextStatus would regress:",
        nextStatus,
      );
      continue;
    }

    // Build next row (no SourceUpdatedAt yet)
    const nextRow: any = {
      GameId: gameId,
      Status: nextStatus,
      HomePts: nextHome ?? null,
      AwayPts: nextAway ?? null,
      Period: period ?? null,
      Clock: clock != null ? String(clock) : null,
      Network: network ?? null,
      espn_url: finalEspnUrl,
      // only set FinalizedAt on the first transition to final
      ...(nextStatus === "final" && !prev?.FinalizedAt
        ? { FinalizedAt: sourceTs }
        : {}),
    };

    const reasons = diffReasons(prev, nextRow);

    console.log(
      "GameId",
      gameId,
      "prev vs next",
      JSON.stringify(
        {
          prev: {
            Status: prev.Status,
            HomePts: prev.HomePts,
            AwayPts: prev.AwayPts,
            Period: prev.Period,
            Clock: prev.Clock,
            Network: prev.Network,
            espn_url: prev.espn_url,
            FinalizedAt: prev.FinalizedAt,
          },
          next: {
            Status: nextRow.Status,
            HomePts: nextRow.HomePts,
            AwayPts: nextRow.AwayPts,
            Period: nextRow.Period,
            Clock: nextRow.Clock,
            Network: nextRow.Network,
            espn_url: nextRow.espn_url,
            FinalizedAt: nextRow.FinalizedAt ?? null,
          },
          reasons,
        },
        null,
        2,
      ),
    );

    // Only write if there's a meaningful change
    if (reasons.length) {
      nextRow.SourceUpdatedAt = sourceTs;
      updates.push(nextRow);
      counters.total++;
      for (const r of reasons) {
        counters[r] = (counters[r] ?? 0) + 1;
      }
    } else {
      console.log("GameId", gameId, "no changes detected");
    }
  }

  console.log("Prepared updates count:", updates.length);
  console.log("Counters:", JSON.stringify(counters));

  if (updates.length) {
    const { error: upErr } = await supabase
      .from("all_games")
      .upsert(updates, {
        onConflict: "GameId",
      });

    if (upErr) {
      console.error("Upsert error:", upErr);
      return new Response("Upsert error", { status: 500 });
    }
  }

  return new Response(
    `OK updates=${updates.length} reasons=${JSON.stringify(
      counters,
    )}`,
    { status: 200 },
  );
});