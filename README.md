# AI Receptionist Prototype

An AI-powered receptionist prototype for a painting business. The system can chat with customers, extract painting project details, ask follow-up questions, save leads to SQLite, manage lead statuses, and support Twilio phone-call testing.

## Features

- FastAPI backend
- Browser chat interface
- Browser voice input/output
- Hybrid regex + LLM information extraction
- Latency tracking
- Multi-customer sessions
- SQLite lead storage
- CRM-style lead dashboard
- Lead status workflow: New, Contacted, Scheduled, Closed, Lost
- Twilio phone-call integration
- ngrok webhook testing

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

Create a `.env` file in the project root:

```text
OPENAI_API_KEY=your_openai_api_key_here
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
```