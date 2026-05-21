from fastapi import APIRouter, Form, Request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather

from app.receptionist import handle_message


router = APIRouter()


def twiml_response(response: VoiceResponse) -> Response:
    """
    Converts Twilio VoiceResponse into XML response.
    """
    return Response(
        content=str(response),
        media_type="application/xml"
    )


def add_gather(response: VoiceResponse, prompt: str):
    """
    Adds a speech gather step.
    Twilio will speak the prompt, listen to the caller, then send the transcript
    to /voice/handle as SpeechResult.
    """
    gather = Gather(
        input="speech",
        action="/voice/handle",
        method="POST",
        speech_timeout="auto",
        timeout=5,
    )

    gather.say(
        prompt,
        voice="alice",
        language="en-US",
    )

    response.append(gather)

    # If the user says nothing, Twilio continues here.
    response.say(
        "I did not hear anything. Please say your painting request after the tone.",
        voice="alice",
        language="en-US",
    )

    response.redirect("/voice")

    return response


@router.post("/voice")
async def start_call(request: Request):
    """
    Entry point when someone calls your Twilio phone number.
    """
    response = VoiceResponse()

    greeting = (
        "Hi, thanks for calling. I am the AI receptionist for the painting business. "
        "Please tell me what painting project you need help with."
    )

    add_gather(response, greeting)

    return twiml_response(response)


@router.post("/voice/handle")
async def handle_speech(
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
):
    """
    Receives speech transcript from Twilio Gather.
    Uses CallSid as the session_id so every phone call has separate memory.
    """

    response = VoiceResponse()

    user_message = SpeechResult.strip()
    print("TWILIO SPEECH RESULT:", user_message)

    if not user_message:
        add_gather(
            response,
            "Sorry, I did not catch that. Please describe your painting project again."
        )
        return twiml_response(response)

    result = handle_message(
        session_id=CallSid,
        message=user_message,
    )

    bot_reply = result["reply"]

    # If lead is complete, finish call politely.
    if result["ready_to_send_to_painter"]:
        response.say(
            bot_reply,
            voice="alice",
            language="en-US",
        )
        response.say(
            "Thank you. The painter will follow up with you. Goodbye.",
            voice="alice",
            language="en-US",
        )
        response.hangup()
        return twiml_response(response)

    # Otherwise, answer and listen again.
    add_gather(response, bot_reply)

    return twiml_response(response)