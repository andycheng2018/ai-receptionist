# AI Receptionist Prototype

An AI-powered receptionist prototype for a painting business. The system can chat with customers, extract painting project details, ask follow-up questions, save leads to SQLite, manage lead statuses, and support Twilio phone-call testing.

## Features

- FastAPI backend
- Browser chat interface
- Browser voice input/output
<<<<<<< HEAD
- Hybrid regex + LLM information extraction
=======
- Hybrid rule-based + AI patch extraction: fast rules for simple turns, AI fallback for messy/corrective turns
>>>>>>> 3895666 (Deploy AI receptionist)
- Latency tracking
- Multi-customer sessions
- SQLite lead storage
- CRM-style lead dashboard
- Lead status workflow: New, Contacted, Scheduled, Closed, Lost
- Twilio phone-call integration
- ngrok webhook testing

<<<<<<< HEAD
=======

## Hybrid Talker / Reasoner Design

This version follows a lightweight version of the Talker-Reasoner idea from
“Agents Thinking Fast and Slow: A Talker-Reasoner Architecture.”

```text
Customer message
  -> Fast Talker layer
     - company FAQ/cache
     - simple city/name/phone/email extraction
     - obvious painting service extraction
     - safe pricing/availability templates
  -> Reasoner layer only when needed
     - corrections: "actually", "ignore that", "not outside"
     - mixed projects: cabinets plus touch-ups, drywall plus painting
     - customer city vs project city
     - complex scheduling or pricing questions with lead details
     - non-trivial messages where rules extracted nothing useful
  -> Deterministic next-question engine
     - keeps replies short
     - asks one question at a time
     - avoids repeating known details
```

The Reasoner does **not** write the final customer reply. It returns a JSON patch:

```json
{
  "set": {"service": "interior painting", "project_scope": "hallway and kitchen"},
  "clear": ["timeline", "urgency", "stories"],
  "is_correction": true,
  "confidence": 0.95
}
```

The deterministic receptionist code applies the patch, scores the lead, and chooses the next short spoken response.

## Reasoner Debug Panel

The browser now shows a **Reasoner** line under latency. This tells you how each turn was handled:

```text
Reasoner: Rule layer
```

Means the message stayed in fast deterministic code. Examples: `Andy`, `650-555-1234`, `San Mateo`, `yes`, `two`.

```text
Reasoner: Heuristic fallback patch
```

Means the app detected a complex turn and used the local patch fallback because no real AI key/server was available.

```text
Reasoner: Real AI / LLM patch
```

Means the app called OpenAI or your OpenAI-compatible Qwen server. To see this, set `OPENAI_API_KEY` or `OPENAI_BASE_URL`/`OPENAI_MODEL` and restart the backend.

The `/chat` response also includes this metadata under `metrics`:

```json
{
  "reasoner_source": "rule | heuristic | llm | error",
  "reasoner_used": true,
  "reasoner_trigger": "correction_signal",
  "reasoner_confidence": 0.86,
  "reasoner_latency_ms": 123,
  "reasoner_reason": "Why the Reasoner ran"
}
```


## Live Call Mode vs Smart Mode

The app now defaults to **Live Call Mode** because phone users should not wait 4-5 seconds for a model call.

```text
Live Call Mode checked / AI_RECEPTIONIST_LIVE_MODE=true
  - Rules + heuristic patch run immediately
  - Real LLM is deferred
  - Best for voice calls and demos
  - Target latency: usually under 100 ms for heuristic turns

Live Call Mode unchecked / AI_RECEPTIONIST_LIVE_MODE=false
  - Complex turns may call the real LLM before replying
  - More accurate on the first response, but slower
  - Best for web chat testing or debugging

Run AI Cleanup button
  - Calls the real LLM after the fast reply
  - Cleans up the structured lead without blocking the caller
```

The Reasoner panel shows this explicitly:

```text
Reasoner: Fast heuristic patch — real AI deferred | mode: live/fast | LLM deferred for cleanup
```

Uncheck **Live call mode** to verify your real model is connected. You should then see:

```text
Reasoner: Real AI / LLM patch | mode: smart/blocking
```

>>>>>>> 3895666 (Deploy AI receptionist)
## Project Structure

```text
ai-receptionist/
  app/
    main.py
    receptionist.py
    database.py
    estimate_rules.py
    llm_extractor.py
    models.py
    voice.py
  index.html
  requirements.txt
  README.md
  .gitignore
  .env
  leads.db
```

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Your `requirements.txt` should include:

```txt
fastapi
uvicorn
pydantic
python-dotenv
openai
twilio
python-multipart
word2number
```

## Environment Variables

<<<<<<< HEAD
Create a `.env` file in the project root:

```text
OPENAI_API_KEY=your_openai_api_key_here
=======
The app works without an API key. In that mode, it uses fast rules plus a small local heuristic patcher for common corrections.

For the full AI fallback, create a `.env` file in the project root:

```text
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-4.1-mini
```

You can also use Qwen or another OpenAI-compatible server:

```text
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=local-dev-key
OPENAI_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507
>>>>>>> 3895666 (Deploy AI receptionist)
```

Do not upload `.env` to GitHub.

## Run the Backend

From the project root:

```bash
source venv/bin/activate
uvicorn app.main:app --reload
```

The backend should run at:

```text
http://127.0.0.1:8000
```

Test it in the browser:

```text
http://127.0.0.1:8000
```

Expected response:

```json
{
  "status": "ok",
  "service": "AI Receptionist Prototype"
}
```

## Run the Frontend

Open `index.html` in your browser:

```bash
open index.html
```

The frontend supports:

- Sending customer messages
- Browser voice input
- Browser voice output
- Latency display
- Extracted lead display
- Saved leads dashboard
- Lead status updates

## Saved Leads

Saved leads are stored in SQLite:

```text
leads.db
```

To view leads from the browser:

```text
http://127.0.0.1:8000/leads
```

To inspect the database from terminal:

```bash
sqlite3 leads.db
```

Then run:

```sql
.headers on
.mode column
SELECT id, name, phone, city, service, room_size_sqft, timeline, status FROM leads;
```

Exit SQLite:

```sql
.quit
```

## Twilio Phone Call Testing

Run the backend first:

```bash
uvicorn app.main:app --reload
```

In a second terminal, expose the backend with ngrok:

```bash
ngrok http 8000
```

Copy the HTTPS forwarding URL, for example:

```text
https://xxxx.ngrok-free.app
```

Set your Twilio phone number webhook to:

```text
https://xxxx.ngrok-free.app/voice
```

Use:

```text
HTTP POST
```

Test the voice endpoint:

```bash
curl -X POST https://xxxx.ngrok-free.app/voice
```

Expected output should start with:

```xml
<Response>
```

## Example Web Chat Test

Paste this into the web chat:

```text
I need a 100 sq ft room painted in San Jose. Walls only. Next week. My name is Andy and my phone is 408-555-1234. How much does it cost?
```

Expected behavior:

- Extracts city: San Jose
- Extracts service: interior painting
- Extracts size: 100 sq ft
- Extracts walls only
- Extracts timeline: next week
- Extracts name and phone
- Gives a rough estimate
- Saves the lead to SQLite

## Example Phone Call Test

Say this during the Twilio call:

```text
Hi, I need a one hundred square foot room painted in San Jose. Walls only. Next week. My name is Andy, and my phone number is four zero eight five five five one two three four.
```

Expected behavior:

- Twilio transcribes the call
- FastAPI receives the transcript
- The receptionist extracts the lead
- The bot asks follow-up questions if needed
- Completed lead saves to SQLite
- The dashboard updates with the new lead

## LLM Test Case

Use this to test the LLM path:

```text
I want to freshen up a normal sized guest bedroom in Palo Alto before my in laws visit. It is just the walls, and there are a few small nail holes. My name is Amanda, and my phone number is six five zero five five five eight eight four four.
```

This should require the LLM because it includes:

- Implied painting: “freshen up”
- Vague size: “normal sized guest bedroom”
- Flexible timeline: “before my in laws visit”
- Repair note: “small nail holes”

## Current Limitations

- Twilio call flow is turn-based, not real-time streaming.
- Speech-to-text can mishear words, such as “walls only” as “wars only.”
- Phone number transcription can still need tuning.
- LLM calls are slower than regex parsing.
- Pricing rules are basic and mostly focused on interior room painting.
- Exterior painting and cabinet painting need better service-specific estimate logic.

## Future Improvements

- Add service-specific estimates for exterior and cabinet painting
- Add CSV export for leads
- Add duplicate lead prevention
- Add better phone number normalization
- Improve Twilio call flow
- Add Deepgram speech-to-text and ElevenLabs text-to-speech
- Build a deployed version instead of local ngrok testing
- Add authentication for the lead dashboard

## GitHub Safety

Before uploading to GitHub, make sure `.gitignore` includes:

```gitignore
venv/
__pycache__/
*.pyc
.env
leads.db
leads.json
.DS_Store
*.log
```

Do not upload:

```text
.env
leads.db
venv/
```

## Normal Development Workflow

Run backend:

```bash
cd ~/ai-receptionist
source venv/bin/activate
uvicorn app.main:app --reload
```

Open frontend:

```bash
open index.html
```

For phone testing:

```bash
ngrok http 8000
<<<<<<< HEAD
```
=======
```
## Upgrade notes

This version is tuned to behave more like a real phone receptionist for a painting company:

- Uses a three-layer flow: company cache first, rule extraction second, LLM fallback only for messy language.
- Keeps replies short and asks one question at a time.
- Handles painting-specific lead details: interior/exterior/cabinets/touch-ups, walls/ceiling/trim, rooms, square footage, stories, property type, repairs, photos, and project scope.
- Captures lead operations fields: urgency, preferred callback time, lead score, and Hot/Warm/Normal priority.
- Avoids unsafe promises: no guaranteed exact price and no guaranteed exact availability.
- Saves final structured JSON with transcript, project details, missing fields, score, and summary.
- Dashboard now shows priority, score, callback time, scope, and photo availability.

### Run scenario tests

```bash
python tests/test_receptionist_scenarios.py
```

These tests cover:

1. An urgent exterior rental lead where tenants are moving in soon.
2. An interior bedroom price flow with dimensions and walls-only extraction.
3. A human handoff request.

### Optional LLM model setting

The fallback extractor reads `OPENAI_MODEL` from `.env`. If it is not set, the code uses a lightweight default. The app still works without an API key because the rule-based extraction is the primary path.

## v14 architecture note: fast AI when in doubt

This version treats rules as safety guards and fast extraction, not as the only source of understanding.

Live call mode stays fast by returning immediately with deterministic/heuristic extraction. When real AI is configured and the turn is complex, the app starts a background LLM reasoner so the current lead can be refined after the immediate reply. Use the **Refresh Live AI Result** button or send another message to see the updated session state. For a blocking/smart web-chat test, uncheck **Live call mode**.

High-priority safe intents such as price/budget questions are caught before normal lead capture. For example, “Can you repaint one bedroom for under $500?” is handled as a price question and does not generate a fake dollar estimate.

## v19 LLM-first receptionist mode

This build adds an LLM-first live receptionist path to avoid regex whack-a-mole.
If `OPENAI_API_KEY` or an OpenAI-compatible Qwen endpoint is configured, `/chat` uses a fast LLM to return both:

1. a short, warm receptionist reply, and
2. a structured lead patch.

Deterministic guardrails still run after the LLM:

- no exact price promises
- no guaranteed appointments
- one-question-at-a-time
- phone/email validation
- assistant echo guard
- final lead scoring and saving

Environment options:

```text
AI_RECEPTIONIST_LLM_FIRST=true
OPENAI_API_KEY=your_key_here
OPENAI_FAST_MODEL=gpt-4.1-mini
# or use OPENAI_BASE_URL for a local OpenAI-compatible Qwen server
```

To fall back to the older rule-heavy flow:

```text
AI_RECEPTIONIST_LLM_FIRST=false
```

## v20: FAQ cache + AI response cache

This version keeps LLM-first understanding, but avoids unnecessary AI latency:

- Standalone company FAQ questions such as hours, service area, licensed/insured, free estimate, and cabinet capability are answered instantly from the deterministic FAQ cache.
- The live LLM prompt includes a compact company FAQ sheet so model answers are more consistent.
- Repeated identical LLM turns are cached in-process using a conservative key: model + prompt version + customer message + current lead + recent transcript.
- The UI reasoner line shows cache hits: `cache hit: faq` or `cache hit: ai_response`.
- Deterministic safety guardrails still override unsafe price/schedule replies after the LLM responds.

Useful environment variables:

```bash
AI_RESPONSE_CACHE=true
AI_RESPONSE_CACHE_TTL_SECONDS=600
AI_RECEPTIONIST_LLM_FIRST=true
OPENAI_FAST_MODEL=gpt-4.1-mini
```

## v22: ElevenLabs voice output

This build adds ElevenLabs text-to-speech for the receptionist reply.

Flow:

```text
Customer message
→ /chat returns text reply + lead JSON
→ browser calls /tts/speak
→ backend calls ElevenLabs
→ browser plays MP3 audio
```

The demo still falls back to the browser's built-in speech synthesis if ElevenLabs is not configured or if the TTS request fails.

### Configure ElevenLabs

Add these to `.env` in the project root:

```bash
ELEVENLABS_API_KEY=your_elevenlabs_api_key_here
ELEVENLABS_VOICE_ID=your_elevenlabs_voice_id_here
ELEVENLABS_MODEL=eleven_turbo_v2_5
ELEVENLABS_TTS_CACHE=true
```

Restart the backend after editing `.env`:

```bash
python3 -m uvicorn app.main:app --reload
```

Open the app and check the line under the voice status:

```text
ElevenLabs: ready | model: eleven_turbo_v2_5 | audio cache: on
```

Then click **Test ElevenLabs Voice** or send a normal receptionist message.

### TTS endpoints

```text
GET  /tts/config     Safe TTS status for the browser UI.
POST /tts/speak      Body: {"text": "..."}; returns audio/mpeg.
```

The TTS endpoint uses ElevenLabs' HTTP API through Python's standard library, so no extra SDK is required. It also includes a small MP3 cache keyed by text + voice + model so repeated demo replies are faster and cheaper.

### Recommended voice settings

For a painting receptionist, use a voice that is warm, clear, calm, and not overly dramatic. Start with:

```bash
ELEVENLABS_MODEL=eleven_turbo_v2_5
ELEVENLABS_STABILITY=0.50
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0.10
```

For lower latency, try:

```bash
ELEVENLABS_MODEL=eleven_flash_v2_5
```

## ElevenLabs troubleshooting

If the page says `ElevenLabs: ready` but the voice sounds like the browser voice, open the browser console or click **Diagnose ElevenLabs**.

Useful checks:

```bash
curl http://127.0.0.1:8000/tts/config
curl http://127.0.0.1:8000/tts/diagnose
curl http://127.0.0.1:8000/tts/voices
```

Common causes of `/tts/speak` errors:

- `ELEVENLABS_VOICE_ID` is not a voice in your ElevenLabs account.
- API key is invalid or has no TTS access.
- Model name is invalid for your account. Try `eleven_turbo_v2_5` or `eleven_flash_v2_5`.
- Output format is invalid for your plan. Try `mp3_44100_128`.
- Environment values have extra spaces or quotes.

The frontend now shows the actual ElevenLabs error instead of silently falling back to browser speech.

## Twilio phone-call demo with ElevenLabs voice

v24 adds real phone-call webhooks:

- `POST /twilio/voice` — Twilio incoming-call webhook
- `POST /twilio/handle` — receives Twilio speech transcription
- `GET /twilio/audio/{audio_id}.mp3` — temporary ElevenLabs MP3 files for Twilio `<Play>`
- `GET /twilio/status` — setup/debug status

### Local setup

1. Start the FastAPI app:

```bash
python3 -m uvicorn app.main:app --reload
```

2. Expose it publicly with ngrok:

```bash
ngrok http 8000
```

3. Put the ngrok HTTPS URL into `.env`:

```env
TWILIO_PUBLIC_BASE_URL=https://your-ngrok-url.ngrok-free.app
TWILIO_USE_ELEVENLABS=true
```

4. In Twilio Console → Phone Numbers → your number → Voice Configuration, set:

```text
A call comes in: Webhook
URL: https://your-ngrok-url.ngrok-free.app/twilio/voice
Method: HTTP POST
```

5. Call your Twilio number.

### Call flow

```text
Caller speaks
↓
Twilio <Gather input="speech"> transcribes speech
↓
/twilio/handle calls the existing receptionist logic
↓
ElevenLabs generates an MP3 reply
↓
Twilio plays the MP3 with <Play>
↓
Twilio listens again
```

If ElevenLabs fails, the app automatically falls back to Twilio `<Say>` so the call keeps working.

### Debugging

Open:

```text
http://127.0.0.1:8000/twilio/status
http://127.0.0.1:8000/tts/diagnose
```

In the terminal, successful phone audio should show Twilio fetching `/twilio/audio/...mp3` after `/twilio/handle`.

## v25 phone latency and barge-in improvements

v25 adds a fast-ack phone mode for Twilio calls.

Instead of making Twilio wait silently while your backend does LLM + ElevenLabs TTS, `/twilio/handle` now immediately returns TwiML that says a short filler such as:

```text
Got it, one moment.
```

Then it redirects to `/twilio/wait/{job_id}` while a background thread generates the real receptionist reply and prefetches ElevenLabs audio. When the job is ready, Twilio plays the MP3 or falls back to `<Say>`.

Recommended `.env` settings:

```env
TWILIO_FAST_ACK=true
TWILIO_FAST_ACK_TEXT=Got it, one moment.
TWILIO_PREFETCH_ELEVENLABS=true
ELEVENLABS_MODEL=eleven_flash_v2_5
ELEVENLABS_OPTIMIZE_STREAMING_LATENCY=3
ELEVENLABS_TTS_CACHE=true
TWILIO_MAX_REPLY_CHARS=360
TWILIO_GATHER_TIMEOUT=7
TWILIO_SPEECH_TIMEOUT=auto
```

### Latency logging

The terminal now logs per-turn latency:

```text
TWILIO latency job=... llm_ms=842 tts_ms=611 total_ms=1530 voice=elevenlabs error=None
```

Use this to decide whether your bottleneck is the LLM, ElevenLabs, or Twilio speech recognition.

### Barge-in notes

Basic `<Gather input="speech">` turn-taking is still not true full-duplex barge-in. v25 improves perceived latency and sets up optional partial-result logging:

```env
TWILIO_PARTIAL_RESULTS=true
```

For real interruption while the AI is already speaking, use Twilio Media Streams or Twilio ConversationRelay later. The current webhook + `<Gather>` approach is simpler and works well for a prototype, but it cannot reliably interrupt an MP3 already being played with `<Play>`.


## v26 simple Twilio latency mode

This build disables the awkward filler phrase by default. The call flow is simpler:

```text
Caller speaks -> Twilio Gather -> /twilio/handle -> fast LLM -> ElevenLabs audio -> Twilio plays reply
```

Recommended latency settings in `.env`:

```text
TWILIO_FAST_ACK=false
OPENAI_FAST_MODEL=gpt-4.1-mini
ELEVENLABS_MODEL=eleven_flash_v2_5
ELEVENLABS_TTS_CACHE=true
TWILIO_MAX_REPLY_CHARS=260
TWILIO_GATHER_TIMEOUT=5
TWILIO_SPEECH_TIMEOUT=auto
```

If latency is still high, experiment with a faster `OPENAI_FAST_MODEL` first. The biggest delay is usually the live LLM response, then ElevenLabs generation. True barge-in while the bot is speaking requires Twilio Media Streams or ConversationRelay; this webhook version is turn-based.

## v27 Twilio phone-call fixes

For real phone calls, Twilio sends the caller's number in the `From` field. The app now uses that as the callback phone number automatically, so the receptionist will not ask callers for a phone number unless caller ID is unavailable.

Recommended phone settings:

```env
TWILIO_AUTO_HANGUP=false
TWILIO_FAST_ACK=false
TWILIO_USE_ELEVENLABS=true
```

`TWILIO_AUTO_HANGUP=false` keeps the call open after each answer and lets Twilio gather the next caller turn. This prevents the bot from hanging up just because the lead looks complete.
>>>>>>> 3895666 (Deploy AI receptionist)
