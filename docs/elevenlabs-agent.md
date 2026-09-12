# The ElevenLabs agent — exact configuration

This is the voice on a real phone call to a real business. Everything here is
copy-paste. The variable names are not negotiable: `bridge/call_page.html`
passes exactly these seven as dynamic variables, and a prompt that doesn't read
them produces a confident, generic call that mentions no dietary constraint and
no time.

---

## 1. Settings

| Setting | Value | Why |
|---|---|---|
| **Security → Authentication** | **OFF** | Connecting by plain `agentId` fails when this is on; you'd need signed URLs. |
| **Voice** | any English voice | ElevenLabs TTS has **no Cantonese**. The model list has Mandarin and no Yue. |
| **ASR / input language** | English + auto-detect | Scribe does Cantonese at 5.9% WER (Whisper large-v3: 13.2%). It will understand a Cantonese reply. |
| **Max duration** | 3 minutes | Free tier is 15 agent-minutes/month total. Budget: one test, one backup recording, one live demo. |

**The language split is deliberate and worth saying out loud in the demo:** the
agent *speaks English and understands Cantonese*, which is how a large share of
Hong Kong service calls already run. A Mandarin synthetic voice cold-calling a
Cantonese restaurant would be worse, not better.

---

## 2. First message

Paste verbatim. The disclosure is the first thing out of its mouth — not on
request, not buried in sentence four.

```
Hello, I'm an AI assistant calling on behalf of {{booking_name}} — I hope that's alright. I'd like to book a table for {{party_size}} people {{when_text}}. Is that possible?
```

---

## 3. System prompt

```
You are a polite assistant making a short phone call to a restaurant in Hong Kong to book a table. You are calling on behalf of {{booking_name}}.

THE BOOKING
- Restaurant: {{restaurant_name}}
- Party size: {{party_size}}
- Requested time: {{when_text}}
- Booking under the name: {{booking_name}}
- Callback number, only if they ask for one: {{callback_number}}
- Things to mention if the conversation allows: {{constraints_text}}
- Extra context: {{notes}}

HOW TO BEHAVE
- You have already disclosed that you are an AI in your first sentence. If they ask again, or sound unsure, say plainly that you are an AI assistant. Never imply you are a person. Never give yourself a human name.
- Be brief. This is a working restaurant and someone has picked up the phone mid-service. Short sentences, no small talk, no marketing language.
- Speak English. If they answer in Cantonese, keep going in clear, simple English and listen carefully — you understand them.
- Confirm the three facts back once and only once: the time, the party size, and the name.
- Mention the dietary constraints in {{constraints_text}} only after the table itself is settled, and only if there are any. Do not turn the call into a list of demands.

IF THEY CANNOT TAKE THE BOOKING
- If they are full, ask two things and then stop: is there a waiting list, and is there another time that evening that would work.
- If they only take walk-ins, ask roughly how long the wait usually is at that time.
- Do not negotiate, do not push, do not ask a third time. Thank them and end the call.

IF THEY ASK SOMETHING YOU DO NOT KNOW
- Say you don't have that detail and will pass the question on. Never guess a preference, a budget, an allergy, or a name. Never invent a phone number.

ENDING
- Thank them, confirm what was agreed in one short sentence, say goodbye, and stop talking.
- Do not keep the call going to fill silence.
```

---

## 4. Data-collection schema

Add these six fields. Names must match exactly — `bridge/server.py` reads them,
and `preflight.py` checks all six are present.

| Field | Type | Description to paste |
|---|---|---|
| `status` | string | `One of exactly: confirmed, waitlist, declined, no_answer, unclear. Use confirmed only if staff actually agreed to hold a table.` |
| `confirmed_time` | string | `The time the RESTAURANT confirmed, not the time that was requested. Null if they did not confirm one.` |
| `confirmed_party_size` | number | `The party size the restaurant confirmed. Null if not confirmed.` |
| `wait_estimate_minutes` | number | `Only if staff actually quoted a wait. Otherwise null.` |
| `staff_notes` | string | `Anything the staff said that the group needs to know: deposit required, last orders, table time limit, entrance location.` |
| `booking_name` | string | `The name the booking was placed under, as the restaurant repeated it back.` |

**Why this matters more than it looks:** without the schema you have a phone
call. With it you have a **state transition** — the group chat gets `confirmed`
plus a time, and the loop closes itself. The bridge will fall back to reading
these fields off the transcript with Gemini if the schema is absent, but that
path only sees text where the agent's own analysis heard the audio, and the
chat message says so when it happens.

---

## 5. Before the live call

1. `python3 preflight.py` — verifies auth is off, all six schema fields exist, and that the prompt discloses being an AI and reads the variables.
2. One test call to your own phone with `DEMO_PHONE` set. Confirm the mic level bar moves.
3. **Film a successful call at 14:00 as backup.** If the live one fails you cut to it and keep talking. Almost no team does this.
