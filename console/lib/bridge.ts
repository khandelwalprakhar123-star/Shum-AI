/**
 * The bridge contract, in TypeScript.
 *
 * These field names are not free. bot.py writes them into
 * bridge/pending_call.json, bridge/call_page.html reads them, and the
 * `contract` test suite parses both files and asserts they agree. This file is
 * the third reader, so it uses the same names — a rename here shows up as a
 * blank in the console rather than an error.
 */

export type CallStatus =
  | "awaiting_approval"
  | "approved"
  | "dialing"
  | "done"
  | "cancelled";

export interface PendingCall {
  id?: string;
  created_at?: string;
  chat_id?: number;
  restaurant_name?: string;
  restaurant_display?: string;
  real_number?: string | null;
  dial_number?: string | null;
  demo_override?: boolean;
  party_size?: number;
  when_text?: string;
  booking_name?: string;
  callback_number?: string;
  constraints?: string[];
  notes?: string;
  area?: string;
  vote_tally?: string;
  dial?: boolean;
  status?: CallStatus;
  approved_by?: string;
  approved_at?: string;
  outcome?: Record<string, unknown>;
  outcome_source?: string;
  finished_at?: string;
}

export interface BridgeConfig {
  agent_id: string;
  agent_id_present: boolean;
  booker_name: string;
  callback_number: string;
  demo_phone_active: boolean;
}

// Same-origin. The browser never talks to :8080 directly — app/api/bridge
// proxies it — so there is no CORS preflight and nothing for a strict
// sub-resource policy to block. Point BRIDGE_URL at the bridge server-side
// instead if it ever moves off the default port.
const BASE = "/api/bridge";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { cache: "no-store", ...init });
  if (!response.ok) {
    throw new Error(`bridge ${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}

export const bridge = {
  base: BASE,

  health: () => request<{ ok: boolean; service: string }>("/health"),
  config: () => request<BridgeConfig>("/config"),
  pending: () => request<PendingCall>("/pending"),

  dial: () => request<PendingCall>("/dial", { method: "POST", body: "{}" ,
    headers: { "Content-Type": "application/json" } }),

  cancel: () => request<PendingCall>("/cancel", { method: "POST", body: "{}",
    headers: { "Content-Type": "application/json" } }),

  amend: (patch: Partial<Pick<PendingCall,
    "party_size" | "when_text" | "booking_name" | "notes">>) =>
    request<PendingCall>("/amend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
};

export function hasBooking(pending: PendingCall | null): boolean {
  return !!pending && Object.keys(pending).length > 0 && !!pending.restaurant_name;
}

export function describeBooking(pending: PendingCall | null): string {
  if (!hasBooking(pending)) return "Nothing is queued. Run /decide then /close in the group chat.";
  const p = pending as PendingCall;
  const lines = [
    `Restaurant: ${p.restaurant_display || p.restaurant_name}${p.area ? ` (${p.area})` : ""}`,
    `Number to dial: ${p.dial_number || "none — this booking cannot be called"}`,
    `Party size: ${p.party_size ?? "unknown"}`,
    `When: ${p.when_text || "unknown"}`,
    `Booking name: ${p.booking_name || "unknown"}`,
    `Callback number: ${p.callback_number || "none"}`,
    `Constraints to mention: ${(p.constraints || []).join("; ") || "none recorded"}`,
    `Status: ${p.status || "unknown"}`,
    `Vote tally: ${p.vote_tally || "unknown"}`,
  ];
  if (p.demo_override) {
    lines.push(
      "SAFETY RAIL ACTIVE: DEMO_PHONE is set, so this call routes to a number the team " +
        `controls (${p.dial_number}), NOT to the restaurant` +
        (p.real_number ? ` (whose real number is ${p.real_number})` : "") + "."
    );
  } else {
    lines.push(
      "This is a live call to a real venue that consented in advance. The voice agent " +
        "discloses it is an AI in its first sentence."
    );
  }
  return lines.join("\n");
}
