from __future__ import annotations

import logging
import os
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
    openai,
    noise_cancellation,
    silero,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION

load_dotenv()
logger = logging.getLogger("inbound-caller")
logger.setLevel(logging.INFO)

# Suppress harmless Windows IPC errors
logging.getLogger("livekit.agents.utils.aio.duplex_unix").setLevel(logging.CRITICAL)


def _register_session_events(session: AgentSession, agent) -> None:
    """Register observability event listeners on an AgentSession."""

    @session.on("close")
    def on_session_close():
        # Log final call data with lead score to backend when session closes
        import asyncio
        asyncio.create_task(agent.log_call_to_backend(status="completed", end_time=datetime.now()))
        
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
                        logger.info(f"[BACKEND] Call log sent successfully for {phone_number} with score {self.lead_score['totalScore']}")
                    else:
                        logger.error(f"[BACKEND] Failed to send call log: {response.status}")
        except Exception as e:
            logger.error(f"[BACKEND] Error sending call log: {e}")

    async def hangup(self):
        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(room=job_ctx.room.name)
        )

    @function_tool()
    async def calculate_lead_score(
        self, 
        ctx: RunContext,
        business_type: str = "",
        channels: str = "",
        pain_points: str = "",
        timeline: str = "",
        confidence_signals: str = ""
    ):
        """Calculate and store lead score based on conversation signals. Call this before routing the customer.
        
        Args:
            business_type: Type of business (e.g., "established e-commerce", "new startup", "growing agency")
            channels: Customer channels mentioned (e.g., "WhatsApp", "phone and WhatsApp", "multi-channel")
            pain_points: Pain points described (e.g., "500+ messages daily", "overwhelmed", "need automation")
            timeline: Timeline mentioned (e.g., "ASAP", "this month", "exploring")
            confidence_signals: Confidence signals detected (e.g., "asks pricing", "mentions budget", "decision maker")
        """
        score_breakdown = {
            "businessType": 0,
            "channels": 0,
            "painPoints": 0,
            "timeline": 0,
            "confidenceSignals": 0
        }
        
        # Business Type Scoring (0-20)
        business_lower = business_type.lower()
        if any(word in business_lower for word in ["established", "years", "running for"]):
            score_breakdown["businessType"] = 20
            self.lead_score["businessType"] = business_type
        elif any(word in business_lower for word in ["growing", "expanding"]):
            score_breakdown["businessType"] = 15
            self.lead_score["businessType"] = business_type
        elif any(word in business_lower for word in ["startup", "new", "just launched"]):
            score_breakdown["businessType"] = 10
            self.lead_score["businessType"] = business_type
        
        # Channels Scoring (0-20)
        channels_lower = channels.lower()
        channel_list = []
        if "whatsapp" in channels_lower:
            channel_list.append("WhatsApp")
        if any(word in channels_lower for word in ["phone", "call", "voice"]):
            channel_list.append("Phone")
        if any(word in channels_lower for word in ["web", "website", "chat"]):
            channel_list.append("Web")
        
        if len(channel_list) >= 3:
            score_breakdown["channels"] = 20
        elif len(channel_list) == 2:
            score_breakdown["channels"] = 15
        elif len(channel_list) == 1:
            score_breakdown["channels"] = 10
        
        self.lead_score["customerChannels"] = channel_list
        
        # Pain Points Scoring (0-25)
        pain_lower = pain_points.lower()
        pain_list = []
        if any(num in pain_points for num in ["100+", "200+", "500+", "1000+", "50+"]):
            score_breakdown["painPoints"] = 25
            pain_list.append(f"Quantified problem: {pain_points}")
        elif any(word in pain_lower for word in ["overwhelmed", "can't handle", "missing", "losing"]):
            score_breakdown["painPoints"] = 20
            pain_list.append(pain_points)
        elif any(word in pain_lower for word in ["need", "want", "looking for", "automation"]):
            score_breakdown["painPoints"] = 10
            pain_list.append(pain_points)
        
        self.lead_score["painPoints"] = pain_list
        
        # Timeline Scoring (0-20)
        timeline_lower = timeline.lower()
        if any(word in timeline_lower for word in ["asap", "urgent", "immediately", "now", "this week"]):
            score_breakdown["timeline"] = 20
            self.lead_score["timeline"] = "ASAP/Urgent"
        elif any(word in timeline_lower for word in ["this month", "soon", "next few weeks"]):
            score_breakdown["timeline"] = 15
            self.lead_score["timeline"] = "This month"
        elif any(word in timeline_lower for word in ["next month", "next quarter", "q2", "q3"]):
            score_breakdown["timeline"] = 10
            self.lead_score["timeline"] = "Next quarter"
        elif any(word in timeline_lower for word in ["exploring", "looking into", "researching", "just browsing"]):
            score_breakdown["timeline"] = 5
            self.lead_score["timeline"] = "Exploring"
        
        # Confidence Signals Scoring (0-15)
        confidence_lower = confidence_signals.lower()
        confidence_list = []
        if any(word in confidence_lower for word in ["price", "pricing", "cost", "how much"]):
            score_breakdown["confidenceSignals"] += 15
            confidence_list.append("Asks about pricing")
        if "budget" in confidence_lower:
            score_breakdown["confidenceSignals"] += 12
            confidence_list.append("Mentions budget")
        if any(word in confidence_lower for word in ["owner", "i run", "my business", "decision maker"]):
            score_breakdown["confidenceSignals"] += 10
            confidence_list.append("Decision maker")
        if any(word in confidence_lower for word in ["team", "employees", "agents", "staff"]):
            score_breakdown["confidenceSignals"] += 8
            confidence_list.append("Mentions team size")
        if any(word in confidence_lower for word in ["comparing", "looking at options", "other solutions"]):
            score_breakdown["confidenceSignals"] += 8
            confidence_list.append("Comparing solutions")
        
        # Cap confidence signals at 15
        score_breakdown["confidenceSignals"] = min(score_breakdown["confidenceSignals"], 15)
        self.lead_score["confidenceSignals"] = confidence_list
        
        # Calculate total score
        total_score = sum(score_breakdown.values())
        self.lead_score["totalScore"] = total_score
        self.lead_score["breakdown"] = score_breakdown
        
        # Determine priority
        if total_score >= 75:
            priority = "HOT"
        elif total_score >= 50:
            priority = "WARM"
        elif total_score >= 25:
            priority = "COOL"
        else:
            priority = "LOW"
        
        self.lead_score["priority"] = priority
        
        # Generate recommended solution
        solution_parts = []
        if channel_list:
            if len(channel_list) > 1:
                solution_parts.append(f"Multi-channel AI agent ({', '.join(channel_list)})")
            else:
                solution_parts.append(f"Custom {channel_list[0]} AI agent")
        
        if business_type:
            solution_parts.append(f"for {business_type}")
        
        self.lead_score["recommendedSolution"] = " ".join(solution_parts) if solution_parts else "Custom AI agent solution"
        
        logger.info(f"[LEAD SCORE] Total: {total_score}/100 | Priority: {priority} | Breakdown: {score_breakdown}")
        
        return f"Lead scored: {total_score}/100 points - {priority} priority. Route accordingly: HOT/WARM → Appointment, COOL → Form, LOW → Soft close."

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
    async def end_call(self, ctx: RunContext):
        """Called when the user wants to end the call"""
        logger.info(f"ending the call for {self.participant.identity}")
        
        # Log call completion to backend
        await self.log_call_to_backend(status="completed", end_time=datetime.now())
        
        # Wait for the goodbye message to finish playing
        await ctx.wait_for_playout()
        
        # Wait 5 seconds before hanging up
        import asyncio
        await asyncio.sleep(5)
        
        # Hang up the call
        await self.hangup()

    @function_tool()
    async def send_form(self, ctx: RunContext):
        """Automatically send the form via WhatsApp/SMS when user agrees to receive it. Call this when user says yes, yeah, ok, sure, or similar affirmative responses after the form offer."""
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
                        logger.info(f"[FORM] Successfully sent via {channel} to {phone_number}")
                        return f"Form sent successfully via {channel.upper()}"
                    else:
                        error = result.get("error", "Unknown error")
                        logger.error(f"[FORM] Failed to send: {error}")
                        return f"Failed to send form: {error}"
        except Exception as e:
            logger.error(f"[FORM] Error sending form: {e}")
            return f"Error sending form: {str(e)}"


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
        stt=cartesia.STT(model="ink-whisper", language="en"),
        llm=openai.LLM(
            model="llama-3.3-70b-versatile",  # This model supports prompt caching
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
        ),
        tts=cartesia.TTS(
            model="sonic-3",
            voice="f786b574-daa5-4673-aa0c-cbe3e8534c02",
            language="en",
        ),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(
            turn_detection=MultilingualModel(),
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
