// supabase/functions/debug-espn-game/index.ts
// deno run --allow-net

import { serve } from "https://deno.land/std/http/server.ts";

const ESPN_API =
  "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard";

type SimplifiedGame = {
  espnId: string;
  date: string | null;
  statusState: string | null;
  statusDetail: string | null;
  period: number | null;
  clock: string | null;
  homeTeam: string | null;
  homeScore: string | null;
  awayTeam: string | null;
  awayScore: string | null;
  link: string | null;
};

function simplifyEvent(ev: any): SimplifiedGame | null {
  const comp = ev?.competitions?.[0];
  if (!comp) return null;

  const statusType = comp?.status?.type;
  const competitors = comp?.competitors ?? [];

  const home = competitors.find((c: any) => c.homeAway === "home") ?? {};
  const away = competitors.find((c: any) => c.homeAway === "away") ?? {};

  const link =
    ev?.links?.find((l: any) => l?.rel?.includes("summary"))?.href ??
    ev?.link ??
    null;

  return {
    espnId: String(comp.id),
    date: ev?.date ?? null,
    statusState: statusType?.state ?? null,
    statusDetail: statusType?.shortDetail ?? statusType?.detail ?? null,
    period: comp?.status?.period ?? null,
    clock: comp?.status?.displayClock ?? null,
    homeTeam: home?.team?.shortDisplayName ??
      home?.team?.displayName ??
      null,
    homeScore: home?.score ?? null,
    awayTeam: away?.team?.shortDisplayName ??
      away?.team?.displayName ??
      null,
    awayScore: away?.score ?? null,
    link,
  };
}

function buildHtmlTable(games: SimplifiedGame[]): string {
  const rows = games
    .map((g) => {
      const status =
        g.statusState === "pre"
          ? "Scheduled"
          : g.statusState === "in"
          ? "In Progress"
          : g.statusState === "post"
          ? "Final"
          : g.statusState ?? "";

      const date = g.date ?? "";
      const periodClock =
        g.period != null || g.clock
          ? `Q${g.period ?? ""} ${g.clock ?? ""}`.trim()
          : "";

      const linkCell = g.link
        ? `<a href="${g.link}" target="_blank">link</a>`
        : "";

      return `
        <tr>
          <td>${g.espnId}</td>
          <td>${date}</td>
          <td>${status}</td>
          <td>${g.statusDetail ?? ""}</td>
          <td>${periodClock}</td>
          <td>${g.awayTeam ?? ""}</td>
          <td>${g.awayScore ?? ""}</td>
          <td>${g.homeTeam ?? ""}</td>
          <td>${g.homeScore ?? ""}</td>
          <td>${linkCell}</td>
        </tr>
      `;
    })
    .join("");

  return `
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>ESPN CFB Scoreboard Debug</title>
        <style>
          body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            padding: 16px;
          }
          table {
            border-collapse: collapse;
            width: 100%;
            font-size: 14px;
          }
          th, td {
            border: 1px solid #ccc;
            padding: 4px 8px;
            text-align: left;
            white-space: nowrap;
          }
          th {
            background-color: #f5f5f5;
          }
          tr:nth-child(even) {
            background-color: #fafafa;
          }
          .small {
            font-size: 12px;
            color: #666;
          }
        </style>
      </head>
      <body>
        <h1>ESPN College Football Scoreboard (Debug)</h1>
        <p class="small">
          This is everything ESPN is currently returning from
          <code>/scoreboard</code>. Use the ESPN IDs in the first column
          to match against your <code>GameId</code>.
        </p>
        <table>
          <thead>
            <tr>
              <th>ESPN ID</th>
              <th>Date</th>
              <th>Status</th>
              <th>Status Detail</th>
              <th>Period / Clock</th>
              <th>Away Team</th>
              <th>Away Score</th>
              <th>Home Team</th>
              <th>Home Score</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody>
            ${rows || `<tr><td colspan="10">No games found.</td></tr>`}
          </tbody>
        </table>
      </body>
    </html>
  `;
}

serve(async () => {
  try {
    const res = await fetch(ESPN_API, {
      headers: { "cache-control": "no-cache" },
    });

    if (!res.ok) {
      const msg = `ESPN API error ${res.status}`;
      console.error(msg);
      return new Response(msg, {
        status: 502,
        headers: { "content-type": "text/plain" },
      });
    }

    const data = await res.json();
    const events = data?.events ?? [];

    const simplified = events
      .map(simplifyEvent)
      .filter((g): g is SimplifiedGame => g !== null);

    const html = buildHtmlTable(simplified);

    return new Response(html, {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  } catch (e) {
    console.error(e);
    return new Response(
      `Error: ${(e as Error).message}`,
      {
        status: 500,
        headers: { "content-type": "text/plain" },
      },
    );
  }
});