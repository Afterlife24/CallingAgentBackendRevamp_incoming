from __future__ import annotations

import logging
import os
import re
import asyncio
import aiohttp
from datetime import datetime

from dotenv import load_dotenv
from livekit import agents, rtc, api
from livekit.agents import (
    AgentServer,
    AgentSession,
    Agent,
    JobContext,
    function_tool,
    RunContext,
    get_job_context,
    cli,
    room_io,
    TurnHandlingOptions,
    InterruptionOptions,
    UserStateChangedEvent,
    AgentStateChangedEvent,
    FunctionToolsExecutedEvent,
    ConversationItemAddedEvent,
)
from livekit.plugins import (
    cartesia,
    groq,
    noise_cancellation,
)
from livekit.agents import inference

from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION

load_dotenv()
logger = logging.getLogger("inbound-caller")
logger.setLevel(logging.INFO)

# Regex to strip Llama-style function call syntax that leaks into text output
_FUNC_CALL_RE = re.compile(
    r"<function=\w+.*?</function>|<\|.*?\|>",
    re.DOTALL,
)


def _sanitize_tts_text(text: str) -> str:
    """Strip leaked tool-call syntax so it never reaches TTS / the caller's ear."""
    cleaned = _FUNC_CALL_RE.sub("", text).strip()
    if cleaned != text:
        logger.warning(
            f"[TTS FILTER] Stripped leaked function call from speech: {len(text) - len(cleaned)} chars removed")
    return cleaned


# Suppress harmless Windows IPC errors
logging.getLogger("livekit.agents.utils.aio.duplex_unix").setLevel(
    logging.CRITICAL)


def _register_session_events(session: AgentSession, agent) -> None:
    """Register observability event listeners on an AgentSession."""

    @session.on("close")
    def on_session_close():
        # Log final call data with lead score to backend when session closes
        import asyncio
        asyncio.create_task(agent.log_call_to_backend(
            status="completed", end_time=datetime.now()))

        # Print lead score summary
        ls = agent.lead_score
        logger.info("=" * 60)
        logger.info("[LEAD SCORE SUMMARY]")
        logger.info(f"  Score:       {ls['totalScore']}/100")
        logger.info(f"  Priority:    {ls['priority']}")
        logger.info(f"  Business:    {ls['businessType'] or 'N/A'}")
        logger.info(
            f"  Channels:    {', '.join(ls['customerChannels']) if ls['customerChannels'] else 'N/A'}")
        logger.info(
            f"  Pain Points: {', '.join(ls['painPoints']) if ls['painPoints'] else 'N/A'}")
        logger.info(f"  Timeline:    {ls['timeline'] or 'N/A'}")
        logger.info(
            f"  Confidence:  {', '.join(ls['confidenceSignals']) if ls['confidenceSignals'] else 'N/A'}")
        logger.info(f"  Reasoning:   {ls['recommendedSolution'] or 'N/A'}")
        logger.info("=" * 60)

        usage = session.usage
        if usage and usage.model_usage:
            for mu in usage.model_usage:
                logger.info(f"[USAGE] Session totals: {mu}")

    @session.on("conversation_item_added")
    def on_conversation_item(ev: ConversationItemAddedEvent):
        item = ev.item
        if hasattr(item, 'role') and hasattr(item, 'text_content'):
            logger.info(f"[CONVERSATION] {item.role}: {item.text_content}")
            # Add to transcript buffer
            agent.transcript_buffer.append({
                "role": item.role,
                "text": item.text_content,
                "timestamp": datetime.now().isoformat()
            })

    @session.on("agent_state_changed")
    def on_agent_state(ev: AgentStateChangedEvent):
        logger.info(f"[STATE] Agent: {ev.old_state} → {ev.new_state}")

    @session.on("user_state_changed")
    def on_user_state(ev: UserStateChangedEvent):
        logger.info(f"[STATE] User: {ev.old_state} → {ev.new_state}")

    @session.on("function_tools_executed")
    def on_tools_executed(ev: FunctionToolsExecutedEvent):
        for call, output in ev.zipped():
            logger.info(
                f"[TOOL] {call.name}({call.arguments}) → {output.output if output else 'None'}"
            )

    @session.on("user_input_transcribed")
    def on_transcription(ev):
        if ev.is_final:
            logger.info(f"[STT] Final: {ev.transcript}")


SUPPORTED_LANGUAGES = {
    "en": "English",
    "ar": "Arabic",
    "fr": "French",
}


class InboundCaller(Agent):
    def __init__(self):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self.participant: rtc.RemoteParticipant | None = None
        self.current_language: str = "en"
        self.call_start_time: datetime | None = None
        self.call_id: str | None = None
        self.room_name: str | None = None
        self.transcript_buffer: list = []

        # Tool sequencing guards (Task B)
        # _form_done is set when no send_form is in flight. end_call waits on it
        # so we never hang up mid-request. _form_sent marks a successful send.
        self._form_done = asyncio.Event()
        self._form_done.set()
        self._form_sent = False
        self._ending = False  # guards against end_call running twice
        self._scored = False  # set when score_and_route_lead has been called

        # Lead scoring attributes
        self.lead_score = {
            "totalScore": 0,
            "priority": "LOW",
            "businessType": "",
            "customerChannels": [],
            "painPoints": [],
            "timeline": "",
            "confidenceSignals": [],
            "recommendedSolution": "",
            "breakdown": {
                "businessType": 0,
                "channels": 0,
                "painPoints": 0,
                "timeline": 0,
                "confidenceSignals": 0
            }
        }

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def tts_node(self, text, model_settings):
        """Filter leaked function-call syntax from text before it reaches TTS."""
        async def _filtered_text():
            async for chunk in text:
                cleaned = _FUNC_CALL_RE.sub("", chunk)
                if cleaned:
                    yield cleaned
        return Agent.default.tts_node(self, _filtered_text(), model_settings)

    def extract_phone_number(self, identity: str) -> str:
        """Extract phone number from SIP identity like 'sip_+917780313547'"""
        if identity.startswith("sip_"):
            return identity[4:]  # Remove 'sip_' prefix
        return identity

    async def log_call_to_backend(self, status: str = "ongoing", end_time: datetime | None = None):
        """Send call log to backend API"""
        if not self.participant or not self.call_id:
            return

        backend_url = os.getenv("BACKEND_API_URL", "http://localhost:5000")
        phone_number = self.extract_phone_number(self.participant.identity)

        payload = {
            "callId": self.call_id,
            "phoneNumber": phone_number,
            "roomName": self.room_name,
            "startTime": self.call_start_time.isoformat() if self.call_start_time else None,
            "endTime": end_time.isoformat() if end_time else None,
            "status": status,
            "language": self.current_language,
            "transcript": self.transcript_buffer,
            "leadScoring": self.lead_score,  # Include lead scoring data
            "metadata": {
                "participantIdentity": self.participant.identity,
                "callDirection": "inbound"
            }
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{backend_url}/api/call-logs/log",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        logger.info(
                            f"[BACKEND] Call log sent successfully for {phone_number} with score {self.lead_score['totalScore']}")
                    else:
                        logger.error(
                            f"[BACKEND] Failed to send call log: {response.status}")
        except Exception as e:
            logger.error(f"[BACKEND] Error sending call log: {e}")

    async def hangup(self):
        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(room=job_ctx.room.name)
        )

    @function_tool()
    async def score_and_route_lead(
        self,
        ctx: RunContext,
        total_score: str,
        priority: str,
        business_type: str = "",
        channels: str = "",
        pain_points: str = "",
        timeline: str = "",
        confidence_signals: str = "",
        reasoning: str = ""
    ):
        """Score the lead after catching their intent (2-3 qualifying questions). Do NOT call this on the first message.
        You should understand their business and main problem before calling.

        Scoring guide:
        - Business Stage (0-20): Established=20, Growing=15, Startup=10
        - Channels needed (0-20): 3+=20, 2=15, 1=10
        - Pain Points (0-25): Quantified problem=25, Clear pain=20, General need=10
        - Timeline (0-20): ASAP=20, This month=15, Next quarter=10, Exploring=5
        - Confidence Signals (0-15): Pricing ask=15, Budget mention=12, Decision maker=10

        Args:
            total_score: Your assessed score from 0-100 as a number (e.g. "80")
            priority: One of "HOT" (75-100), "WARM" (50-74), "COOL" (25-49), "LOW" (0-24)
            business_type: What kind of business they run (from conversation)
            channels: Communication channels they mentioned needing
            pain_points: Their pain points as you understood them
            timeline: Their timeline/urgency as you understood it
            confidence_signals: Buying signals you detected (pricing questions, decision maker, etc.)
            reasoning: Brief explanation of why you gave this score
        """
        # Coerce score to int — LLMs often emit numbers as strings.
        try:
            score_int = int(str(total_score).strip())
        except (ValueError, TypeError):
            score_int = 0
        score_int = max(0, min(100, score_int))

        self._scored = True

        # Normalize priority; derive from score if the model sent something odd.
        priority = (priority or "").strip().upper()
        if priority not in ("HOT", "WARM", "COOL", "LOW"):
            if score_int >= 75:
                priority = "HOT"
            elif score_int >= 50:
                priority = "WARM"
            elif score_int >= 25:
                priority = "COOL"
            else:
                priority = "LOW"

        # Store the LLM's assessment
        self.lead_score["totalScore"] = score_int
        self.lead_score["priority"] = priority
        self.lead_score["businessType"] = business_type
        self.lead_score["customerChannels"] = [
            c.strip() for c in channels.split(",") if c.strip()] if channels else []
        self.lead_score["painPoints"] = [pain_points] if pain_points else []
        self.lead_score["timeline"] = timeline
        self.lead_score["confidenceSignals"] = [c.strip() for c in confidence_signals.split(
            ",") if c.strip()] if confidence_signals else []
        self.lead_score["recommendedSolution"] = reasoning

        logger.info(
            f"[LEAD SCORE] LLM assessed: {score_int}/100 | Priority: {priority} | Reasoning: {reasoning}")

        # Return routing instruction — first REASSURE, then offer with explanation.
        if priority in ("HOT", "WARM"):
            return (f"Lead scored {score_int}/100 — {priority}. "
                    f"FIRST: Acknowledge their problem and reassure them briefly — tell them you can definitely help "
                    f"with that and your team has done this for similar businesses. Keep it to 1 sentence. "
                    f"THEN: Offer a booking link — explain that your team can walk them through the exact setup "
                    f"and pricing, and you can send a booking link to their WhatsApp to pick a time — no commitment. "
                    f"ASK if they'd like that. Do NOT call send_form yet — wait for yes.")
        elif priority == "COOL":
            return (f"Lead scored {score_int}/100 — {priority}. "
                    f"FIRST: Acknowledge their situation positively — tell them that's something you can definitely "
                    f"help with when they're ready. Keep it to 1 sentence. "
                    f"THEN: Offer a short requirements form — explain it takes a minute to fill out, "
                    f"and your team will put together options tailored to their business — no commitment. "
                    f"ASK if they'd like it sent. Do NOT call send_form yet — wait for yes.")
        else:
            return (f"Lead scored {score_int}/100 — {priority}. "
                    f"FIRST: Acknowledge their situation warmly — say something supportive like 'that makes sense' "
                    f"or 'no rush at all.' Keep it brief. "
                    f"THEN: Offer a quick info form — explain your team can share tailored info when they're ready — "
                    f"no commitment. ASK if they'd like it. Do NOT call send_form unless they say yes.")

    @function_tool()
    async def switch_language(self, ctx: RunContext, language: str):
        """Switch the conversation language. Call this when the caller explicitly asks to speak in Arabic ('ar'), French ('fr'), or English ('en')."""
        lang = language.strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            return f"Unsupported language '{language}'. Supported: English (en), Arabic (ar), French (fr)."

        if lang == self.current_language:
            return f"Already speaking in {SUPPORTED_LANGUAGES[lang]}."

        session: AgentSession = ctx.session
        session.stt.update_options(language=lang)
        session.tts.update_options(language=lang)
        self.current_language = lang
        logger.info(
            f"[LANGUAGE] Switched to {SUPPORTED_LANGUAGES[lang]} ({lang})")
        return f"Switched to {SUPPORTED_LANGUAGES[lang]}. Continue the conversation in {SUPPORTED_LANGUAGES[lang]} now."

    @function_tool()
    @function_tool()
    async def end_call(self, ctx: RunContext):
        """End the call. A warm goodbye will be spoken automatically before hanging up.
        Call this when the conversation is over — either after send_form, or when the user declines and you want to close."""
        # Guard: don't run the hangup sequence more than once
        if self._ending:
            return
        self._ending = True

        identity = self.participant.identity if self.participant else "console"
        logger.info(f"ending the call for {identity}")

        # If a send_form request is still in flight, wait for it to finish
        try:
            await asyncio.wait_for(self._form_done.wait(), timeout=12.0)
        except asyncio.TimeoutError:
            logger.warning(
                "[END] send_form still in flight after 12s, proceeding to hang up")

        # Speak a fixed goodbye — no LLM generation needed.
        # If the form was sent, mention it; otherwise, just a warm close.
        if self._form_sent:
            goodbye = "You'll get it on WhatsApp in just a moment. Our team will reach out from there. Thanks so much for your time today, and have a great day!"
        else:
            goodbye = "No problem at all. Feel free to reach out whenever you're ready. Thanks for your time — have a great day!"

        try:
            await ctx.session.say(goodbye, allow_interruptions=False)
        except Exception as e:
            logger.warning(f"[END] failed to speak goodbye: {e}")
            try:
                await ctx.wait_for_playout()
            except Exception:
                pass

        # Log call completion to backend
        await self.log_call_to_backend(status="completed", end_time=datetime.now())

        # Hang up the call (skip in console mode)
        if self.participant:
            await self.hangup()

    @function_tool()
    async def send_form(self, ctx: RunContext):
        """Send the form/booking link via WhatsApp. Call ONLY after you have asked "Want me to send it?" AND the user said yes. Never call this before offering and getting their consent. After this, speak a closing message, then call end_call."""
        # Guard: must call score_and_route_lead before sending
        if not self._scored:
            logger.warning(
                "[FORM] send_form called before score_and_route_lead — forcing a score call first")
            return "ERROR: You must call score_and_route_lead BEFORE send_form. Score the lead first, then offer, then send."

        if not self.participant:
            logger.error("[FORM] No participant available to send form")
            return "Error: No participant information available"

        phone_number = self.extract_phone_number(self.participant.identity)
        logger.info(f"[FORM] Sending form to {phone_number}")

        # Get WhatsApp API URL from environment
        whatsapp_api_url = os.getenv("WHATSAPP_API_URL")
        if not whatsapp_api_url:
            logger.error("[FORM] WHATSAPP_API_URL not set in environment")
            return "Error: WhatsApp API URL not configured"

        logger.info(f"[FORM] Using WhatsApp API URL: {whatsapp_api_url}")

        # Mark a send as in flight so end_call waits for us to finish.
        self._form_done.clear()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{whatsapp_api_url}/sendFormTemplate",
                    json={
                        "phone_number": phone_number,
                        "call_id": self.call_id
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    result = await response.json()

                    if result.get("success"):
                        channel = result.get("channel", "unknown")
                        self._form_sent = True
                        logger.info(
                            f"[FORM] Successfully sent via {channel} to {phone_number}")
                        return f"Form sent successfully via {channel.upper()}"
                    else:
                        error = result.get("error", "Unknown error")
                        logger.error(f"[FORM] Failed to send: {error}")
                        return f"Failed to send form: {error}"
        except Exception as e:
            logger.error(f"[FORM] Error sending form: {e}")
            return f"Error sending form: {str(e)}"
        finally:
            # Always release end_call, even if the request failed.
            self._form_done.set()


# ── Server & Entrypoint ──────────────────────────────────────────────

server = AgentServer()


@server.rtc_session(agent_name="inbound-caller")
async def entrypoint(ctx: JobContext):
    logger.info(f"Inbound call — connecting to room {ctx.room.name}")
    await ctx.connect()

    agent = InboundCaller()
    agent.call_start_time = datetime.now()
    agent.call_id = ctx.job.id
    agent.room_name = ctx.room.name

    # In console mode there's no SIP participant — skip waiting
    is_console = ctx.room.name == "console"

    if not is_console:
        participant = await ctx.wait_for_participant(
            kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        )
        logger.info(f"Caller joined: {participant.identity}")
        agent.set_participant(participant)

        # Log call start to backend
        await agent.log_call_to_backend(status="ongoing")

    session = AgentSession(
        stt=cartesia.STT(
            # Cartesia's multilingual STT model (supports English, Arabic, French)
            model="ink-whisper",
            language="en"  # Start with English, can switch dynamically
        ),
        llm=groq.LLM(
            model="llama-3.3-70b-versatile",
        ),
        tts=cartesia.TTS(
            model="sonic-3",
            voice="f786b574-daa5-4673-aa0c-cbe3e8534c02",
            language="en",
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            interruption=InterruptionOptions(
                enabled=True,
                mode="adaptive",
                min_duration=0.5,
                min_words=1,
                resume_false_interruption=True,
                false_interruption_timeout=2.0,
            ),
        ),
    )

    _register_session_events(session, agent)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        ),
    )

    # Greet the caller — agent speaks first
    await session.generate_reply(
        instructions=SESSION_INSTRUCTION,
        allow_interruptions=False,
    )
    logger.info("Greeting sent — agent is now listening")


if __name__ == "__main__":
    cli.run_app(server)
