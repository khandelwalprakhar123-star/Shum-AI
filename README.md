# Shum-AI

**An agent that lives in your friends' group chat, reads the argument, and then actually phones the restaurant.**

*Shumai* (燒賣) is the dumpling nobody argues about at a Hong Kong dim sum table. Everything else, they argue about — which is the problem this solves.

Built at the AI Tinkerers *Agents, Everywhere* global hackathon — Hong Kong site, Cyberport, 12 September 2026.

---

## The problem is not recommendation

Restaurant recommendation is a solved problem. Nobody in Hong Kong is short of suggestions.

What fails, every week, is **convergence**. Six people, forty messages, nobody commits, and at 19:40 somebody says *"just pick anything."*

So this agent's job is coordination, not ranking. Its advantage isn't a better model — it's that **it has read the last two hundred messages.** It knows Priya doesn't eat pork, that Marcus is coming in from Sha Tin so Central is a fight, that the group vetoed hotpot twice this month, and that the budget conversation already happened in July.

**None of that would ever be typed into a form.** It exists only in the chat. That is precisely why the agent has to live in the chat.

It also adds the primitive a group chat is missing: **a closing mechanism.** A poll with a deadline and a default, so a decision gets made rather than deferred.

## Why it makes a phone call

In Hong Kong, phoning to book is the cultural norm. It's also the only option, because **there is no booking API you can use:**

| Platform | Self-serve write API? | Reality |
|---|---|---|
| OpenTable | No | Partner application, 3–4 week review |
| Resy | No | Partner tier only |
| SevenRooms | No | Partner ecosystem, not open signup |
| Tock | No | Partner-level only |
| OpenRice (HK's dominant platform) | No | Closed advertising product |
| Chope | No | Affiliate links only |

The industry pattern is link handoff. So "books the table" cannot be real via API. That leaves two options: fake the final step, or actually phone them.

**We phone them.** An ElevenLabs voice agent runs in a browser tab over WebRTC, a human dials the restaurant on their own phone and puts it on speakerphone, and the two talk through the laptop's microphone and speakers. Acoustic coupling.

Nothing in the pipeline is mocked. The last step is the real one.

### Why acoustic coupling and not Twilio

| | Twilio path | This |
|---|---|---|
| Caller ID | Overseas number; HK restaurants may not answer | **A real +852** |
| HK number | Regulatory bundle: HKID + 3-month address proof, days to approve | Not needed |
| OFCA rule | Since Mar 2023 an HK caller ID to an HK number must originate from a Twilio number | N/A |
| Cost | Number + per-minute | **$0** |
| Public tunnel | Required | **None — everything is localhost** |

ElevenLabs moved Agents to WebRTC specifically for *"best-in-class echo cancellation and background noise removal"*, which is exactly the problem a phone speaker next to a laptop mic creates. The hard part was already built.

The second consequence matters as much: **no ngrok, no cloudflared, no public webhook.** A tunnel that dies at 16:04 is the single most common way a hackathon voice demo fails, and the possibility has been removed rather than hoped away.

## Architecture

```
   Telegram group                       (where the argument actually happens)
        │  /decide
        ▼
   bot.py  ──────────────► pipeline.py ──► Gemini  (constraints, then 3 picks)
        │                       │            └─► OpenRouter ─► keyword fallback
        │                  places.py ────► Overpass / OpenStreetMap  (phone numbers)
        │                  exa_search.py ─► Exa                      (discovery)
        │
        │  native poll → votes → /close → winner → human approval
        ▼
   bridge/pending_call.json          (the approved booking, on disk)
        │
        └──────────────► bridge/server.py  :8080   /pending  /dial  /outcome
                                │
                                ├──► bridge/call_page.html   ElevenLabs over WebRTC
                                │        │
                                │        ▼
                                │   laptop speakers ──► YOUR PHONE on speaker ──► restaurant
                                │   laptop mic     ◄── phone's speaker         ◄──
                                │
                                └──► posts the transcript back into the group chat
```

## What is real and what is not

Stated plainly, because a mocked final step is the most common way a demo gets marked down.

**Real:**
- Telegram group messages, read live. Native Telegram polls with real vote counts.
- Constraint extraction by Gemini over actual chat history, with the quote each constraint came from.
- Restaurant data from OpenStreetMap via Overpass — real names, real phone numbers.
- The phone call. A real number rings, a real human answers, the agent holds the conversation, and the transcript goes back into the chat.

**Honest limitations:**
- **OpenStreetMap phone coverage is about 20%.** Measured: 1,199 named places across HK Island north shore and Kowloon, 156 with a usable phone number. Phone-bearing rows are therefore sorted first everywhere — a recommendation you cannot dial is worthless to this agent.
- **A human dials.** The agent does not place the call itself; it speaks once a person has connected it. That's a safety decision, not a missing feature.
- **The agent speaks English and understands Cantonese.** ElevenLabs Scribe does Cantonese speech-to-text at 5.9% WER (vs Whisper large-v3 at 13.2%), but ElevenLabs TTS has no Cantonese voice at all — the model list has Mandarin and no Yue. So: English out, Cantonese in. That is how a large share of Hong Kong service calls already run.
- **Exa is an enhancement, never a dependency.** Every failure path returns `[]`. It finds names; OpenStreetMap makes them callable.
- The CopilotKit operator console is not built. It was scoped as optional and the core came first.

**Not used, deliberately:** OpenRice. Its `robots.txt` names `GPTBot`, `PerplexityBot`, `meta-externalagent` and `Bytespider` and disallows the JSON service endpoints, and its terms forbid using *"any robot, any automatic device or manual process to monitor or copy the Channels."* A hackathon submission is a public repo and a live stage demo. Don't.

## Safety rails

Not optional polish. These are the difference between a good demo and an irresponsible one.

1. **The agent discloses it is an AI in its first sentence.** Not on request, not buried. It's in the agent's fixed prompt, and this repo only ever passes *dynamic variables* — the booking facts — so the disclosure cannot be edited out from the calling code.
2. **A human presses the button and a human dials.** The agent never initiates a call.
3. **`CONSENTED_NUMBERS` is an allowlist enforced in code.** A booking for a number nobody agreed to is refused at the approval gate, with an explanation posted to the chat. See `resolve_dial_target()` in `bot.py`.
4. **`DEMO_PHONE` routes every call to a number you control**, whichever restaurant won the poll. The approval card and the call page both say so out loud.
5. **No invented phone numbers, anywhere.** The model is explicitly forbidden from emitting one, and `_rehydrate()` in `pipeline.py` structurally drops any phone field the model returns — the digits that get dialled come only from OpenStreetMap. The offline seed list in `places.py` carries names with `phone: None` rather than numbers typed from memory.
6. **If it books a real table, turn up or cancel it.**

## Setup

```bash
git clone https://github.com/khandelwalprakhar123-star/Shum-AI && cd Shum-AI
cp .env.example .env      # then fill it in
```

Stdlib only. Nothing to install.

**BotFather, and don't skip the second step:**
1. `/newbot` → copy the token into `TELEGRAM_TOKEN`
2. **`/setprivacy` → Disable.** Without this the bot cannot see group messages at all and nothing works.

**ElevenLabs:** create an agent, set **authentication OFF** (otherwise connecting by plain `agentId` fails and you need signed URLs), and configure a data-collection schema with `status`, `confirmed_time`, `confirmed_party_size`, `wait_estimate_minutes`, `staff_notes`, `booking_name`. Without that schema you have a phone call; with it you have a state transition and the loop can close itself.

**Build the offline safety net while you have working wifi:**
```bash
python3 places.py --refresh-cache
```

**If you are on macOS with Python from python.org, run this once:**
```bash
open "/Applications/Python 3.14/Install Certificates.command"
```
That installer does not wire Python into the system keychain — it expects a `cert.pem` it never creates. The symptom is vicious: `curl https://...` works perfectly while *every* `urllib` call in the same shell dies with `CERTIFICATE_VERIFY_FAILED`. Since every network call here goes through `urllib`, nothing works at all, and the error points at certificates rather than the one-line fix. Both `bot.py` and `bridge/server.py` now check the trust store at startup and print the exact command if it's empty.

**Check everything before it matters:**
```bash
python3 preflight.py
```
Every dependency, checked live, each failure printing the exact fix. It exists because this project's failure modes all masquerade as something else: an empty TLS store looks like a network outage, a retired model looks like a bad prompt, and a bot with privacy mode still on looks like a bot that is ignoring you.

**See the brain work without a Telegram bot or a phone call:**
```bash
python3 dryrun.py            # or: python3 dryrun.py my_chat.txt
```
This is exactly what `/decide` does — read a conversation, name the constraints with the quotes they came from, search, pick three — minus the chat and the call. Roughly 8 seconds end to end.

**Run, in two terminals:**
```bash
python3 bridge/server.py      # then open http://localhost:8080/
python3 bot.py
```

`http://localhost:8080/`, not the file. Browsers refuse `getUserMedia` on `file://`, and that is the number one cause of "it just hangs".

## Using it

Add the bot to a group chat, argue normally, then:

- `/decide` — reads the history, posts the constraints it found *with the quotes they came from*, proposes three places, opens a poll
- `/close` — closes the poll, names the winner, asks a human to approve the call
- `/status` — what it has read and which safety rail is active

On the call page: **Arm microphone** → confirm the level bar moves → dial → speakerphone → **Start agent**.

The level bar is the only diagnostic that matters. If it doesn't move when the restaurant's voice comes out of the phone speaker, acoustic coupling has failed and no prompt tuning will fix it.

## Tests

```bash
python3 tests/run.py
```

The network is mocked entirely — no Telegram, no Gemini, no Overpass, no Exa — and the *real* code is driven against it. The runner exits non-zero on failure, crash **or skip**: a suite silently not running while the report says "0 failed" is worse than a red.

## Stack

### Measured latency of one `/decide`

| Stage | Time |
|---|---|
| Constraint extraction (Gemini) | 2.6s |
| Candidate search (fresh cache) | 0.01s |
| Exa discovery | 1.8s |
| Choosing three (Gemini) | 3.8s |
| **Total** | **~8s** |

It was 79 seconds before two fixes: Gemini was spending its whole token budget on thinking and returning truncated JSON, and a live Overpass round trip was costing up to 18 seconds on every call. Both are described in the commit history. A `/decide` that six impatient people watch for 79 seconds is a different product from one that answers in eight.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Chat | Telegram Bot API | Free, instant token, native polls with real vote counts |
| Brain | Gemini (AI Studio) | Free tier, no card. Chain, never a pin — availability moved twice in 24h |
| Fallback | OpenRouter | Different vendor, different outage |
| Discovery | Exa | Semantic search in the group's own words |
| Phone numbers | Overpass / OpenStreetMap | No key, real HK data, a licence that permits this |
| Voice | ElevenLabs Agents (WebRTC) | Browser-based, no phone number, built-in echo cancellation |

## Licence

MIT.
