"use client";

/**
 * The operator console.
 *
 * The reason this is built with CopilotKit rather than as a plain dashboard is
 * `useHumanInTheLoop`. Shum-AI's whole risk surface is one moment: an AI is
 * about to dial a real phone number and speak to a real person. That moment
 * wants exactly the primitive CopilotKit models — the agent can propose the
 * call, and the call cannot happen until a human looks at a rendered card and
 * presses a button. The approval is not a confirm() bolted onto a chat; it IS
 * the tool call, suspended mid-execution until a person resolves it.
 *
 * Everything else here follows from that: the agent gets read access to the
 * booking through context, write access through two narrow frontend tools, and
 * no ability whatsoever to dial. `place_call` has no handler — only a `render`
 * — which is what makes it structurally impossible for the model to complete
 * it alone.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CopilotChat,
  ToolCallStatus,
  useAgentContext,
  useFrontendTool,
  useHumanInTheLoop,
} from "@copilotkit/react-core/v2";
import { z } from "zod";
import {
  bridge,
  describeBooking,
  hasBooking,
  type BridgeConfig,
  type PendingCall,
} from "@/lib/bridge";

type Health = "unknown" | "up" | "down";

export function Console() {
  const [pending, setPending] = useState<PendingCall | null>(null);
  const [config, setConfig] = useState<BridgeConfig | null>(null);
  const [health, setHealth] = useState<Health>("unknown");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const latest = useRef<PendingCall | null>(null);

  latest.current = pending;

  const refresh = useCallback(async () => {
    try {
      const next = await bridge.pending();
      setPending(next);
      setHealth("up");
    } catch {
      setHealth("down");
    }
  }, []);

  useEffect(() => {
    bridge.config().then(setConfig).catch(() => setConfig(null));
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [refresh]);

  // ---------------------------------------------------------------------
  // What the agent knows
  // ---------------------------------------------------------------------
  // Pushed as context rather than made a tool the model must remember to call.
  // The operator asks "is this right?" and the agent should already have the
  // booking in front of it, the same way the operator does.
  useAgentContext({
    description:
      "The restaurant booking currently queued on the call desk, including which " +
      "number would actually be dialled and whether the DEMO_PHONE safety rail is active.",
    value: describeBooking(pending),
  });

  useAgentContext({
    description: "Whether the localhost bridge on port 8080 is reachable right now.",
    value: health,
  });

  // ---------------------------------------------------------------------
  // What the agent can do: read, amend, cancel. Never dial.
  // ---------------------------------------------------------------------
  useFrontendTool({
    name: "read_booking",
    description:
      "Read the booking currently queued on the call desk: restaurant, number to dial, " +
      "party size, time, booking name, the constraints the agent will mention on the " +
      "phone, and whether the DEMO_PHONE safety rail is active.",
    parameters: z.object({}),
    handler: async () => describeBooking(latest.current),
  });

  useFrontendTool({
    name: "amend_booking",
    description:
      "Change details of the queued booking before the call is placed. Only pass the " +
      "fields you are changing. Cannot change the restaurant or the phone number — " +
      "those come from the poll result and from OpenStreetMap respectively.",
    parameters: z.object({
      party_size: z.number().int().min(1).max(40).optional()
        .describe("New party size"),
      when_text: z.string().max(120).optional()
        .describe("New time, in the words the agent should say aloud, e.g. 'Friday at 8pm'"),
      booking_name: z.string().max(60).optional()
        .describe("The name the table should be booked under"),
      notes: z.string().max(300).optional()
        .describe("Extra context for the voice agent"),
    }),
    handler: async (args) => {
      const patch = Object.fromEntries(
        Object.entries(args).filter(([, v]) => v !== undefined && v !== null)
      );
      if (Object.keys(patch).length === 0) {
        return "Nothing to change — no fields were provided.";
      }
      if (!hasBooking(latest.current)) {
        return "There is no booking queued, so there is nothing to amend.";
      }
      try {
        const updated = await bridge.amend(patch);
        setPending(updated);
        setNote(`Amended: ${Object.keys(patch).join(", ")}`);
        return `Updated. The call desk now shows:\n${describeBooking(updated)}`;
      } catch (error) {
        return `Could not amend the booking: ${(error as Error).message}`;
      }
    },
  });

  useFrontendTool({
    name: "cancel_booking",
    description:
      "Cancel the queued booking so no call is placed. Use when the operator says to " +
      "stand down, or when something about the booking is wrong enough not to dial.",
    parameters: z.object({
      reason: z.string().max(200).describe("Why it is being cancelled"),
    }),
    handler: async ({ reason }) => {
      try {
        await bridge.cancel();
        await refresh();
        setNote(`Cancelled: ${reason}`);
        return `Cancelled. Nothing was dialled. Reason recorded: ${reason}`;
      } catch (error) {
        return `Could not cancel: ${(error as Error).message}`;
      }
    },
  });

  // ---------------------------------------------------------------------
  // The human-in-the-loop gate
  // ---------------------------------------------------------------------
  // No `handler`. The type of a human-in-the-loop tool omits it, so there is
  // no code path by which the model resolves this itself — it can only suspend
  // and wait for `respond`. That is the safety property, enforced by the type
  // rather than by a prompt asking the model to be careful.
  useHumanInTheLoop({
    name: "place_call",
    description:
      "Ask a human to approve placing the phone call to the queued restaurant. This " +
      "does NOT dial. It renders an approval card; a person presses the button, and " +
      "then a person physically dials the phone and puts it on speakerphone. Always " +
      "use this rather than claiming a call has been made.",
    parameters: z.object({
      reason: z
        .string()
        .max(240)
        .describe("One line for the operator on why this call should go out now"),
    }),
    render: ({ args, status, respond, result }) => {
      const booking = latest.current;
      const number = booking?.dial_number;

      if (status === ToolCallStatus.Complete) {
        return (
          <div className="hitl resolved">
            <h3>Approval resolved</h3>
            <p>{typeof result === "string" ? result : "Handled."}</p>
          </div>
        );
      }

      if (status === ToolCallStatus.InProgress) {
        return (
          <div className="hitl">
            <h3>Preparing approval…</h3>
          </div>
        );
      }

      return (
        <div className="hitl">
          <h3>Approve this call?</h3>
          <p>{args.reason || "The agent is asking to place the call."}</p>
          <dl className="outcome" style={{ marginBottom: 14 }}>
            <dt>Restaurant</dt>
            <dd>{booking?.restaurant_display || booking?.restaurant_name || "—"}</dd>
            <dt>Will dial</dt>
            <dd className="num">{number || "no number — cannot call"}</dd>
            <dt>Party / when</dt>
            <dd>
              {booking?.party_size ?? "?"} · {booking?.when_text || "?"}
            </dd>
          </dl>
          {booking?.demo_override ? (
            <p style={{ color: "var(--warn)" }}>
              <b>DEMO_PHONE is active.</b> This routes to a number the team controls, not
              to the restaurant.
            </p>
          ) : (
            <p style={{ color: "var(--info)" }}>
              <b>Live call</b> to a venue that consented in advance. The voice agent
              discloses it is an AI in its first sentence.
            </p>
          )}
          <div className="row">
            <button
              className="go"
              disabled={!number}
              onClick={async () => {
                try {
                  await bridge.dial();
                  await refresh();
                  setNote("Approved from the console — the call page is dialling.");
                  await respond?.(
                    `Approved by the operator. The call desk is dialling ${number}. ` +
                      `A human still has to dial the phone physically and put it on speakerphone.`
                  );
                } catch (error) {
                  await respond?.(
                    `The operator approved, but the bridge refused: ${(error as Error).message}`
                  );
                }
              }}
            >
              Approve the call
            </button>
            <button
              className="stop"
              onClick={() =>
                respond?.("The operator declined. Nothing was dialled.")
              }
            >
              Decline
            </button>
          </div>
        </div>
      );
    },
  });

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------
  const booking = hasBooking(pending) ? (pending as PendingCall) : null;
  const status = booking?.status ?? "idle";
  const outcome = (booking?.outcome ?? {}) as Record<string, unknown>;

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
      setNote(message);
    } catch (error) {
      setNote(`Failed: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <h1>Shum-AI</h1>
          <span>operator console</span>
        </div>

        <span className={`pill ${health === "up" ? "on" : health === "down" ? "bad" : ""}`}>
          <i className="dot" />
          bridge {health === "up" ? "live" : health === "down" ? "unreachable" : "…"}
        </span>

        <span className={`pill ${config?.agent_id_present ? "" : "bad"}`}>
          voice agent {config?.agent_id_present ? "configured" : "missing"}
        </span>

        <span className={`pill ${config?.demo_phone_active ? "warn" : "info"}`}>
          {config?.demo_phone_active ? "DEMO_PHONE rail active" : "live calling"}
        </span>

        <span className={`pill ${status === "dialing" ? "on" : status === "done" ? "info" : ""}`}>
          {String(status)}
        </span>
      </header>

      <div className="body">
        <main className="left">
          {!booking && (
            <section className="card hero">
              <h2>Nothing queued</h2>
              <p className="empty">
                Run <code>/decide</code> then <code>/close</code> in the Telegram group, and
                approve the booking card. It appears here the moment a human approves it.
              </p>
              {health === "down" && (
                <div className="rail">
                  <b>Bridge unreachable on :8080.</b> Start it with{" "}
                  <code>python3 bridge/server.py</code>. If it <i>is</i> running, this is the
                  cross-origin case — the bridge sends CORS headers, so a failure here usually
                  means a different process is on that port.
                </div>
              )}
            </section>
          )}

          {booking && (
            <>
              <section className="card hero">
                <h2>Queued call</h2>
                <p className="venue">
                  {booking.restaurant_display || booking.restaurant_name}
                  <small>
                    {booking.area ? `${booking.area} · ` : ""}
                    {booking.vote_tally || "no tally recorded"}
                  </small>
                </p>

                <dl className="grid" style={{ marginTop: 18 }}>
                  <dt>Will dial</dt>
                  <dd className="num">{booking.dial_number || "— no number, cannot call"}</dd>
                  <dt>Party size</dt>
                  <dd className="num">{booking.party_size ?? "—"}</dd>
                  <dt>When</dt>
                  <dd>{booking.when_text || "—"}</dd>
                  <dt>Under the name</dt>
                  <dd>{booking.booking_name || "—"}</dd>
                  <dt>Callback</dt>
                  <dd className="num">{booking.callback_number || "—"}</dd>
                  <dt>Approved by</dt>
                  <dd>{booking.approved_by || "not yet approved"}</dd>
                </dl>

                {(booking.constraints || []).length > 0 && (
                  <>
                    <h2 style={{ marginTop: 20 }}>Mentioning on the call</h2>
                    <div className="chips">
                      {(booking.constraints || []).map((c) => (
                        <span className="chip" key={c}>
                          {c}
                        </span>
                      ))}
                    </div>
                  </>
                )}

                {booking.demo_override ? (
                  <div className="rail">
                    <b>Safety rail active.</b> DEMO_PHONE is set, so this call goes to{" "}
                    <span className="num">{booking.dial_number}</span> — a number the team
                    controls — not to the restaurant
                    {booking.real_number ? (
                      <>
                        {" "}
                        (<span className="num">{booking.real_number}</span>)
                      </>
                    ) : null}
                    . The group chat was told this too.
                  </div>
                ) : (
                  <div className="rail live">
                    <b>Live call.</b> A real venue that consented in advance. The agent
                    discloses it is an AI in its first sentence. If a table gets booked, turn
                    up or cancel it.
                  </div>
                )}

                <div className="row" style={{ marginTop: 18 }}>
                  <button
                    className="go"
                    disabled={busy || !booking.dial_number || status === "dialing"}
                    onClick={() =>
                      act(bridge.dial, "Dialling — the call page is taking it from here.")
                    }
                  >
                    {status === "dialing" ? "Dialling…" : "Dial now"}
                  </button>
                  <button
                    className="stop"
                    disabled={busy}
                    onClick={() => act(bridge.cancel, "Cancelled. Nothing was dialled.")}
                  >
                    Cancel
                  </button>
                  <span className="empty">
                    A human presses this, and a human dials the phone.
                  </span>
                </div>

                {note && (
                  <p className="empty" style={{ marginTop: 14 }}>
                    {note}
                  </p>
                )}
              </section>

              {Object.keys(outcome).length > 0 && (
                <section className="card">
                  <h2>
                    Call outcome
                    {booking.outcome_source ? ` · via ${booking.outcome_source}` : ""}
                  </h2>
                  <dl className="outcome">
                    {Object.entries(outcome).map(([key, value]) => (
                      <div key={key} style={{ display: "contents" }}>
                        <dt>{key.replace(/_/g, " ")}</dt>
                        <dd>{value === null || value === "" ? "—" : String(value)}</dd>
                      </div>
                    ))}
                  </dl>
                  {booking.outcome_source === "transcript (derived)" && (
                    <div className="rail">
                      These fields were read back off the transcript rather than confirmed by
                      the agent&apos;s own call analysis. Worth a glance before relying on them.
                    </div>
                  )}
                </section>
              )}
            </>
          )}
        </main>

        <aside className="right">
          <header>
            <h2>Operator copilot</h2>
            <p>
              Reads the queued booking. Can amend or cancel it. Cannot dial — asking it to
              call opens an approval card here.
            </p>
          </header>
          <div className="chatwrap">
            <CopilotChat />
          </div>
        </aside>
      </div>
    </div>
  );
}
