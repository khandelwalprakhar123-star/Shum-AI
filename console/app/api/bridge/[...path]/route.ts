/**
 * Same-origin proxy to the localhost bridge.
 *
 * The console runs on :3000 and the bridge on :8080, which makes every direct
 * call from the browser cross-origin. bridge/server.py does send CORS headers,
 * so that path works in a normal browser — but it fails in any context with a
 * stricter sub-resource policy (an embedded browser pane blocks it outright as
 * ERR_BLOCKED_BY_CLIENT, and enterprise extensions do the same), and when it
 * fails it surfaces as a bare "Failed to fetch" with no explanation.
 *
 * Proxying through Next removes the problem rather than configuring around it:
 * the browser only ever talks to its own origin, so there is no preflight, no
 * CORS, and nothing for a client policy to block. The bridge keeps its CORS
 * headers for anything else that wants to call it directly.
 */
import { NextRequest } from "next/server";

const BRIDGE = (process.env.BRIDGE_URL || "http://127.0.0.1:8080").replace(/\/$/, "");

// A allowlist, not a wildcard. An open proxy to anything on localhost would be
// a genuinely bad thing to leave running on a laptop at a conference.
const ALLOWED = new Set(["health", "config", "pending", "dial", "cancel", "amend", "outcome"]);

async function forward(request: NextRequest, path: string[]) {
  const route = (path || []).join("/");
  if (!ALLOWED.has(route)) {
    return Response.json({ error: `route not proxied: ${route}` }, { status: 404 });
  }

  const body = request.method === "POST" ? await request.text() : undefined;
  try {
    const upstream = await fetch(`${BRIDGE}/${route}`, {
      method: request.method,
      headers: { "Content-Type": "application/json" },
      body: body || undefined,
      cache: "no-store",
    });
    const text = await upstream.text();
    return new Response(text, {
      status: upstream.status,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch (error) {
    // Name the actual cause. "The bridge is not running" is a different problem
    // from "the bridge returned an error", and the console shows them differently.
    return Response.json(
      {
        error: "bridge unreachable",
        detail: (error as Error).message,
        hint: `Start it with: python3 bridge/server.py   (expected at ${BRIDGE})`,
      },
      { status: 502 }
    );
  }
}

export async function GET(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return forward(request, (await ctx.params).path);
}

export async function POST(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return forward(request, (await ctx.params).path);
}
