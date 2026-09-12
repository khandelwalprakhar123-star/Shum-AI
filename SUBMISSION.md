# Shum-AI — submission pack

AI Tinkerers **"Agents, Everywhere"** global hackathon · Hong Kong site, Cyberport · 12 September 2026

Repo: <https://github.com/khandelwalprakhar123-star/Shum-AI>

---

## 1. Written description

### Short version — for a character-limited field (~140 words)

Shum-AI lives in a Telegram group chat, reads the argument, and then actually phones the restaurant.

Recommendation is solved; *converging* is not. The agent has read the last two hundred messages, so it knows who doesn't eat pork, who commutes from Sha Tin, and what got vetoed last month — none of which would ever be typed into a form. It runs a native poll with a deadline so a decision gets made, searches the districts that minimise the **worst** journey rather than the average, and then closes the loop offline: no Hong Kong booking platform has a public write API, so an ElevenLabs agent over WebRTC talks to a real phone on speakerphone, with the transcript streaming back into the chat live. A confirmed table posts one calendar link everybody taps. 753 checks, network fully mocked.

### Full version


**Shum-AI is an agent that lives in a Telegram group chat, reads the argument, and then actually phones the restaurant.**

Restaurant recommendation is solved. What fails every week is *convergence*: six people, forty messages, nobody commits, and at 19:40 someone says "just pick anything." So the agent's job is coordination, not ranking.

The group chat is the environment, and it is the whole advantage. The agent has read the last two hundred messages, so it knows Priya doesn't eat pork, that Marcus commutes from Sha Tin, and that hotpot was vetoed twice this month. None of that would ever be typed into a form — it exists only in the chat. A standalone chatbot starts every conversation from zero; this one starts with the group's history and its memory of the people in it. Nothing is remembered without both a named speaker and the quote it came from, and `/who` and `/forget` make every belief inspectable and correctable.

It also supplies the two primitives a group chat lacks. First, a decision: a native Telegram poll with a deadline and a default, so something actually gets chosen. Second, a fair place to meet — "coming from Sha Tin" is a constraint on the journey, not a request to eat in Sha Tin, so when nobody names a district the agent searches the areas that minimise the **worst** journey rather than the average. An average is fair to the group and unfair to whoever lives at the end of the line.

Then it closes the loop offline. No Hong Kong booking platform exposes a self-serve write API, so rather than fake the last step, an ElevenLabs voice agent runs in a browser tab over WebRTC while a human dials and puts the phone on speaker — acoustic coupling, real +852 caller ID, no Twilio number, no public tunnel to die mid-demo. The transcript streams back into the chat turn by turn, so the delegation is supervised rather than an act of faith. When the table is confirmed the agent posts a Google Calendar link anyone can tap — Telegram doesn't hand out member emails, so there is nobody to send an invite *to*, and a link needs no addresses, no OAuth and no account. It is withheld unless a real date **and** a real clock time were pinned down: a dinner filed at a guessed hour is wrong in six pockets until everybody is late.

**Stack.** Stdlib-only Python bot on the Telegram Bot API; Gemini for constraint extraction and selection with an OpenRouter fallback and a keyless regex floor; Overpass/OpenStreetMap for real phone numbers; Exa for semantic discovery (an enhancement, never a dependency — every failure path returns `[]`); ElevenLabs Agents over WebRTC; a Next.js + CopilotKit v2 operator console whose `useHumanInTheLoop` approval *is* the suspended tool call — `place_call` has no handler, so the model structurally cannot dial alone. **753 checks, network fully mocked.** `/decide` answers in ~8 seconds.

---

## 2. Two-minute video script

Total 1:55. Screen recording of a real Telegram chat, phone visible beside the laptop. Narration in **bold**; what's on screen in plain text.

### 0:00 – 0:18 · The problem, in the artefact itself

Scroll a real group chat: forty messages, three cuisines, two vetoes, nobody deciding.

> **"Six people, forty messages, no dinner. Recommendation isn't the problem — nobody's short of restaurant suggestions. Converging is the problem. So we put the agent where the argument already is."**

### 0:18 – 0:40 · It reads the chat, not a form

Type `/decide`. The card appears: constraints listed, each with the person and the quote it came from.

> **"It's read the last two hundred messages. Priya doesn't eat pork — that's her words, quoted. Marcus commutes from Sha Tin, so it's not looking for restaurants in Sha Tin; it's looking in the districts that minimise the worst journey. Nothing here was typed into a form. It only exists in the chat."**

Point at the `/who` output: remembered preferences, per person, across chats.

### 0:40 – 0:55 · A decision gets made

The native Telegram poll, with a deadline and a default.

> **"Group chats have no way to actually decide. So it brings one: a poll with a deadline and a default. Silence resolves instead of stalling."**

### 0:55 – 1:25 · The part everyone skips

The approval card. Tap approve. Dial the restaurant on your phone, speakerphone on beside the laptop; the transcript appears in the chat turn by turn.

> **"Now the hard part. No Hong Kong booking platform has a public write API — so most demos stop here and call it 'integration pending'. We phone them. An ElevenLabs agent over WebRTC, a human dials, speakerphone next to the laptop mic. Real +852 caller ID, no Twilio number. It says it's an AI in the first sentence. And the transcript comes back into the chat live, so this is supervised delegation, not faith."**

### 1:25 – 1:42 · The loop actually closes

Confirmation card: time, party size, name. Tap the calendar link; the event opens pre-filled.

> **"Table for two, two o'clock, under Prakhar. And one link — everyone taps it once and it's in their own calendar. Telegram won't give out member emails, so there's nobody to email. If the exact time never got pinned down, there's no link at all: a guessed hour is wrong in six pockets until everybody's late."**

### 1:42 – 1:55 · Why it can't misbehave

Show `CONSENTED_NUMBERS`, then the console: `place_call` with a `render` and no handler.

> **"It can only dial an allowlisted number, a human approves every call, and in the console the approval *is* the suspended tool call — `place_call` has no handler. The model structurally cannot dial on its own. 753 checks, network fully mocked."**

**Shot list to capture (in this order, before anything else):**

1. The populated chat, scrolling. 2. `/decide` → card. 3. `/who`. 4. The poll. 5. Approve → phone ringing → speakerphone → live transcript. 6. Confirmation card → calendar link opening. 7. `.env` allowlist + `Console.tsx` `place_call`.

---

## 3. Social post

> Six people. Forty messages. No dinner.
>
> **Shum-AI** lives in your Telegram group, reads the argument — who doesn't eat pork, who's commuting from Sha Tin, what got vetoed last month — runs a poll with a deadline, then **actually phones the restaurant** and books the table. The transcript streams back into the chat live, and everyone gets one calendar link to tap.
>
> No Hong Kong booking platform has a public write API. So instead of faking the last step: an @elevenlabsio agent over WebRTC, acoustic-coupled to a real phone on speaker. Real +852 caller ID, no Twilio number, and it discloses it's an AI in the first sentence.
>
> Built at @aitinkerers **Agents, Everywhere** — Hong Kong site, @Cyberport.
> Discovery by @exaailabs · reasoning on Google @GeminiApp · operator console on @CopilotKit
>
> 753 checks, network fully mocked. Repo below. 👇
>
> #AgentsEverywhere #AITinkerers #HongKong

*Verify each handle before posting — spelling is on you, and tagging the wrong account is worse than not tagging.*

---

## 4. Backup footage

Archived under `call_log/` — every call is written to disk with its full transcript and structured
outcome, so if the live call fails on stage there is real footage of a successful one.

Two successful calls on 12 Sep:

- **14:06** — 130 seconds, 13 turns, table for two at 14:00 under Prakhar.
- **15:14** — the restaurant *refused the requested time and offered another*, and the agent took it: asked for 8pm, was told eight was reserved and 8:30 was free, confirmed 8:30, and separately got "we have egg options for the vegetarian" recorded against the group's vegetarian constraint. Outcome: `confirmed`, `20:30`, party of 6, under Prakhar. That is the call worth showing — a happy path proves the plumbing, a renegotiated time proves the agent is listening.
