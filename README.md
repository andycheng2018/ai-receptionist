# AI Receptionist Prototype

An AI-powered receptionist prototype for a painting contractor. The app chats with customers, collects painting lead details, applies safety guardrails, saves completed leads to SQLite, supports a browser demo, and can connect to Twilio phone calls with optional ElevenLabs voice output.

## Current Highlights

- FastAPI backend
- Browser chat interface
- Browser speech recognition input
- Browser speech synthesis fallback
- Optional ElevenLabs text-to-speech
- Optional Twilio phone-call integration
- LLM-first receptionist flow
- Deterministic company FAQ/cache answers
- AI response cache for repeated LLM turns
- Multi-customer session memory
- SQLite lead storage
- Saved lead dashboard
- Lead status workflow: New, Contacted, Scheduled, Closed, Lost
- Lead scoring and Hot/Warm/Normal priority labels
- Guardrails for pricing, scheduling, service area, phone numbers, and business hours

## Current Architecture

The current version uses **one main receptionist engine**:

```text
Customer message
  -> app.main /chat or app.voice /twilio/handle
  -> app.llm_receptionist.handle_message()
     -> company cache / FAQ fast path when possible
     -> one LLM call for natural reply + structured lead patch
     -> deterministic guardrails
     -> lead scoring + saving
  -> browser or Twilio receives the reply
```

Important: the old duplicate `app/receptionist.py`, old `llm_extractor.py`, live-mode toggle, background AI cleanup, and photos requirement have been removed or should no longer be used.

## Main Files

```text
ai-receptionist/
  app/
    main.py              # FastAPI app setup and API routes
    llm_receptionist.py  # Main AI receptionist brain and lead workflow
    models.py            # Pydantic schemas for messages, leads, and responses
    database.py          # SQLite persistence for leads and call records
    company_config.py    # Painting company facts: areas, services, hours, policies
    company_cache.py     # Fast deterministic company answers
    faq_sheet.py         # FAQ text and deterministic FAQ matching
    ai_response_cache.py # In-memory cache for repeated LLM turns
    tts.py               # ElevenLabs text-to-speech routes
    voice.py             # Twilio phone-call routes
  index.html             # Browser demo UI
  requirements.txt
  README.md
  .env                   # Local secrets/config; do not commit
```

## What the Receptionist Collects

The lead capture flow is intentionally short. The required fields are:

```text
service / scope
project city
timeline
name
valid callback phone number
```

The receptionist may also collect optional fields such as:

```text
property type
address
repairs needed
rooms / square footage
walls / ceiling / trim
cabinet count
preferred callback time
notes
```

Photos are no longer required and should not be asked for in the normal flow.

## Guardrails

The receptionist applies deterministic guardrails after the LLM responds.

### Service Area Guardrail

Only supported service-area cities from `COMPANY_CONFIG["service_areas"]` are accepted. Unsupported cities are rejected politely, and the customer is asked for the actual project city again.

Example:

```text
Customer: I need painting in Los Angeles.
AI: Sorry, we don’t currently service projects in Los Angeles. We serve ... What city is the project in?
```

### Phone Number Guardrail

Phone numbers are normalized to an E.164-style format when possible.

Accepted examples:

```text
650-333-3333
(650) 333-3333
+1 650 333 3333
```

Invalid examples are rejected:

```text
12345
abc-def-ghij
```

Expected reply for an invalid phone:

```text
Sorry, could you repeat your phone number? I need a valid callback number.
```

### Business Hours Guardrail

The receptionist does not accept callback/project times outside configured business hours.

Current business hours are stored in `company_config.py` and should also match your `.env`/README examples:

```text
Monday-Friday: 8 AM - 6 PM
Saturday: 9 AM - 2 PM
Sunday: Closed
```

Examples that should be rejected:

```text
Sunday
next Tuesday at 7pm
Saturday at 3pm
```

Examples that should be accepted:

```text
next Tuesday
next Tuesday at 10am
Saturday at 11am
```

### Pricing Guardrail

The receptionist should never give an exact price or price range. It should say pricing depends on details and collect the lead for a reliable follow-up.

### Availability Guardrail

The receptionist should never promise an exact appointment or same-day availability. It can note a preferred time and say someone will confirm.

### Clean Ending Guardrail

Once the required lead details are collected, the receptionist asks one final check:

```text
Is there anything else you’d like me to note for the painter?
```

If the customer says `no`, `nope`, `that’s all`, `thanks`, or similar, the receptionist closes politely and does not ask more questions.

## Browser Demo

Start the backend:

```bash
source venv/bin/activate
uvicorn app.main:app --reload
```

Open the app:

```text
http://127.0.0.1:8000
```

The browser UI supports:

- typed customer messages
- browser voice input
- optional spoken replies
- ElevenLabs voice toggle
- OpenAI model status
- ElevenLabs model status
- latency display
- current lead summary
- saved lead cards
- lead status updates

## API Endpoints

### Core

```text
GET  /                  Browser demo
GET  /health            Backend and LLM configuration status
POST /chat              Main AI receptionist chat endpoint
GET  /sessions/{id}/lead Current in-memory lead for a session
GET  /leads             Saved leads
GET  /call-records      Saved call records
POST /leads/{id}/status Update saved lead status
```

### TTS

```text
GET  /tts/config        Safe ElevenLabs configuration status
GET  /tts/voices        Available ElevenLabs voices
GET  /tts/diagnose      Debug ElevenLabs setup
POST /tts/speak         Generate MP3 audio for text
```

### Twilio

```text
POST /twilio/voice              Incoming-call webhook
POST /twilio/handle             Twilio speech transcription handler
GET  /twilio/audio/{id}.mp3     Temporary MP3 endpoint for Twilio <Play>
GET  /twilio/status             Twilio setup/debug status
```

Backward-compatible legacy aliases may still exist, but new Twilio setup should use `/twilio/voice`.

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

Typical dependencies:

```text
fastapi
uvicorn
pydantic
python-dotenv
openai
twilio
python-multipart
```

## Environment Variables

Create a `.env` file in the project root. Do not commit it.

```env
# OpenAI / LLM
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
OPENAI_FAST_MODEL=gpt-4.1-mini
AI_RECEPTIONIST_TEMPERATURE=0.2
AI_RECEPTIONIST_MAX_TOKENS=450
AI_RESPONSE_CACHE=true
AI_RESPONSE_CACHE_TTL_SECONDS=600

# ElevenLabs Text-to-Speech
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
ELEVENLABS_MODEL=eleven_flash_v2_5
ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_TTS_CACHE=true
ELEVENLABS_TIMEOUT_SECONDS=15
ELEVENLABS_MAX_CHARS=700
ELEVENLABS_STABILITY=0.50
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0.10
ELEVENLABS_SPEAKER_BOOST=true
ELEVENLABS_OPTIMIZE_STREAMING_LATENCY=

# Twilio Phone Demo
TWILIO_PUBLIC_BASE_URL=https://your-ngrok-url.ngrok-free.app
TWILIO_USE_ELEVENLABS=true
TWILIO_GREETING=Hi, thanks for calling. How can I help with your painting project today?
TWILIO_GOODBYE=Thank you. The team will follow up with you. Goodbye.
TWILIO_SAY_VOICE=Polly.Joanna
TWILIO_SAY_LANGUAGE=en-US
TWILIO_SPEECH_LANGUAGE=en-US
TWILIO_SPEECH_MODEL=phone_call
TWILIO_SPEECH_TIMEOUT=auto
TWILIO_GATHER_TIMEOUT=5
TWILIO_AUDIO_CACHE_DIR=.cache/twilio_audio
TWILIO_SPEECH_HINTS=painting,painter,interior,exterior,cabinets,touch up,estimate,San Mateo,Daly City,Foster City,San Bruno,Redwood City

# Twilio UX / Latency
TWILIO_FAST_ACK=false
TWILIO_FAST_ACK_TEXT=Got it, one moment.
TWILIO_FAST_ACK_PAUSE=0.15
TWILIO_PREFETCH_ELEVENLABS=true
TWILIO_WAIT_POLL_SECONDS=0.6
TWILIO_WAIT_MAX_ATTEMPTS=6
TWILIO_MAX_REPLY_CHARS=260
TWILIO_POST_REPLY_PAUSE=0.1
TWILIO_AUTO_HANGUP=false
TWILIO_PARTIAL_RESULTS=false
```

Removed/obsolete variables:

```text
AI_RECEPTIONIST_LIVE_MODE
AI_RECEPTIONIST_LLM_FIRST
```

The simplified version has one main LLM-first flow and no live-mode toggle.

## Twilio Phone Call Testing

1. Start the backend:

```bash
uvicorn app.main:app --reload
```

2. Expose it publicly:

```bash
ngrok http 8000
```

3. Copy the HTTPS ngrok URL into `.env`:

```env
TWILIO_PUBLIC_BASE_URL=https://your-ngrok-url.ngrok-free.app
```

4. In Twilio Console, configure your phone number:

```text
A call comes in: Webhook
URL: https://your-ngrok-url.ngrok-free.app/twilio/voice
Method: HTTP POST
```

5. Call your Twilio number.

The phone flow is turn-based:

```text
Caller speaks
-> Twilio <Gather input="speech"> transcribes speech
-> /twilio/handle calls app.llm_receptionist.handle_message()
-> ElevenLabs MP3 is played with <Play> when configured
-> Twilio falls back to <Say> if ElevenLabs is unavailable
-> Twilio listens for the next caller turn
```

True full-duplex barge-in is not implemented in this webhook flow. For real interruption while the AI is speaking, use Twilio ConversationRelay or bidirectional Media Streams later.

## Example Happy Path Test

```text
Customer: Hello
AI: Hi there! How can I help with your painting project today?

Customer: I need touch-up painting in San Mateo.
AI: Got it — touch-up painting in San Mateo. When are you hoping to get this done?

Customer: Next Tuesday at 10am.
AI: Sounds good. May I get your name?

Customer: Andy.
AI: Thanks, Andy. What is the best phone number for a callback?

Customer: 650-333-3333.
AI: Thanks. Is there anything else you’d like me to note for the painter?

Customer: Nope.
AI: Thanks, Andy. I have your touch-up painting in San Mateo for next Tuesday at 10am. The painter will follow up with you.
```

Expected:

```text
service = touch-up painting
city = San Mateo
timeline = next Tuesday at 10am
name = Andy
phone = +16503333333
lead saved = true
no photos question
no extra final question after nope
```

## Guardrail Test Cases

### Unsupported city

```text
I need painting in Los Angeles.
```

Expected: reject unsupported city and ask for a supported project city.

### Invalid phone

```text
12345
```

Expected: do not save the phone; ask the customer to repeat a valid callback number.

### Outside business hours

```text
Can someone call me Sunday?
```

Expected: say Sunday is closed and ask for a time during business hours.

```text
next Tuesday at 7pm
```

Expected: reject because weekdays close at 6 PM.

### Pricing

```text
Can you do it for under $500?
```

Expected: do not promise a price; say pricing depends on project details and collect the lead.

### Availability

```text
Can you guarantee someone can come today?
```

Expected: do not guarantee availability; collect details for follow-up.

### Correction

```text
Actually not exterior, it is interior touch-up in Daly City.
```

Expected: update service and city, and clear stale conflicting exterior details.

## SQLite Leads

Saved leads are stored in SQLite, usually in:

```text
leads.db
```

View all leads:

```text
http://127.0.0.1:8000/leads
```

Inspect manually:

```bash
sqlite3 leads.db
```

```sql
.headers on
.mode column
SELECT id, name, phone, city, service, timeline, lead_score, lead_priority, status FROM leads;
.quit
```

## GitHub Safety

Make sure `.gitignore` includes:

```gitignore
venv/
__pycache__/
*.pyc
.env
leads.db
.cache/
.DS_Store
*.log
```

Do not commit:

```text
.env
leads.db
.cache/
venv/
```

## Current Limitations

- Twilio call flow is turn-based, not true barge-in.
- Browser speech recognition depends on browser support.
- Twilio speech recognition can mishear names, cities, or numbers.
- The in-memory session store resets when the backend restarts.
- The dashboard is a prototype and does not include authentication.
- SQLite is fine for demos but should be replaced or hardened for production.

## Future Improvements

- Add authentication for the dashboard.
- Add duplicate lead detection.
- Add CSV export.
- Add persistent session storage.
- Add better appointment/calendar integration.
- Add SMS/email lead notifications.
- Add ConversationRelay or Media Streams for real barge-in.
- Add production deployment and HTTPS without ngrok.

## Development Workflow

Run backend:

```bash
source venv/bin/activate
uvicorn app.main:app --reload
```

Open browser demo:

```text
http://127.0.0.1:8000
```

Check backend status:

```text
http://127.0.0.1:8000/health
```

Check ElevenLabs:

```text
http://127.0.0.1:8000/tts/config
http://127.0.0.1:8000/tts/diagnose
```

Check Twilio setup:

```text
http://127.0.0.1:8000/twilio/status
```
