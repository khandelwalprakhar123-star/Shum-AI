/**
 * The CopilotKit runtime endpoint.
 *
 * createCopilotEndpoint returns a Hono app, and Hono apps expose a
 * `fetch(request)` — which is exactly the shape a Next.js App Router handler
 * needs. So the whole runtime is four lines and no adapter.
 *
 * BuiltInAgent is given a LanguageModel INSTANCE rather than a "google/..."
 * model string on purpose. The string form resolves through a provider gateway
 * that wants its own credential; constructing the provider here means the only
 * key involved is the Gemini key this project already has, and the failure mode
 * if it is missing is a clear error at startup instead of a 401 mid-conversation.
 */
import { createGoogleGenerativeAI } from "@ai-sdk/google";
import { BuiltInAgent, CopilotRuntime, createCopilotEndpoint } from "@copilotkit/runtime/v2";

const apiKey =
  process.env.GOOGLE_GENERATIVE_AI_API_KEY ||
  process.env.GOOGLE_API_KEY ||
  process.env.GEMINI_API_KEY;

if (!apiKey) {
  // Loud, at module load, naming the fix. Better than a 401 the operator sees
  // as "the chat just doesn't answer".
  console.error(
    "[console] No Gemini key. Set GOOGLE_GENERATIVE_AI_API_KEY in console/.env.local " +
      "to the same value as GEMINI_API_KEY in the repo-root .env."
  );
}

const google = createGoogleGenerativeAI({ apiKey });
const modelName = process.env.CONSOLE_MODEL || "gemini-3.5-flash";

const SYSTEM = `You are the operator assistant inside Shum-AI's call desk.

Shum-AI reads a friends' group chat arguing about where to eat, extracts the constraints the group already agreed on, runs a poll, and then places a REAL phone call to the winning restaurant with a voice agent.

You are the operator's copilot at the moment before that call goes out. Your job:
- Explain what is queued and why, reading it from the booking state you are given.
- Flag anything that looks wrong BEFORE the call: a party size that contradicts the chat, a time nobody agreed, a missing dietary constraint, a number that is not on the consent allowlist.
- Amend the booking when the operator asks, using your tools.
- When the operator wants the call placed, use the place_call tool. That tool always asks a human to confirm. Never claim you have called anyone.

Hard rules:
- You cannot dial anything yourself. A human approves and a human physically dials the phone.
- Never invent or guess a phone number. If a booking has no number, say so.
- If DEMO_PHONE is active, say plainly that the call routes to a number the team controls rather than the restaurant.
- Be brief. The operator is standing in front of an audience.`;

const runtime = new CopilotRuntime({
  agents: {
    operator: new BuiltInAgent({
      model: google(modelName),
      // `prompt`, not `instructions`. Verified against
      // BuiltInAgentClassicConfig in the published 1.71.1 types -- the field
      // list is model/apiKey/maxSteps/.../prompt, and `instructions` is not
      // among them. TypeScript caught this; a JS project would have shipped an
      // agent with no system prompt at all and no error anywhere.
      prompt: SYSTEM,
      temperature: 0.3,
    }),
  },
});

export const { GET, POST, OPTIONS } = (() => {
  const app = createCopilotEndpoint({ runtime, basePath: "/api/copilotkit" });
  const handler = (request: Request) => app.fetch(request);
  return { GET: handler, POST: handler, OPTIONS: handler };
})();
