from __future__ import annotations

import logging
import os

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

from prompts import AGENT_INSTRUCTION, GREETING_INSTRUCTION

load_dotenv()
logger = logging.getLogger("inbound-caller")
logger.setLevel(logging.INFO)


def _register_session_events(session: AgentSession) -> None:
    """Register observability event listeners on an AgentSession."""

    @session.on("close")
    def on_session_close():
        usage = session.usage
        if usage and usage.model_usage:
            for mu in usage.model_usage:
                logger.info(f"[USAGE] Session totals: {mu}")

    @session.on("conversation_item_added")
    def on_conversation_item(ev: ConversationItemAddedEvent):
        item = ev.item
        logger.info(f"[CONVERSATION] {item.role}: {item.text_content}")

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

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def hangup(self):
        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(room=job_ctx.room.name)
        )

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
        await ctx.wait_for_playout()
        await self.hangup()


# ── Server & Entrypoint ──────────────────────────────────────────────

server = AgentServer()


@server.rtc_session(agent_name="inbound-caller")
async def entrypoint(ctx: JobContext):
    logger.info(f"Inbound call — connecting to room {ctx.room.name}")
    await ctx.connect()

    agent = InboundCaller()

    # In console mode there's no SIP participant — skip waiting
    is_console = ctx.room.name == "console"

    if not is_console:
        participant = await ctx.wait_for_participant(
            kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        )
        logger.info(f"Caller joined: {participant.identity}")
        agent.set_participant(participant)

    session = AgentSession(
        stt=cartesia.STT(model="ink-whisper", language="en"),
        llm=openai.LLM(
            model="llama-3.1-8b-instant",
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

    _register_session_events(session)

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
        instructions=GREETING_INSTRUCTION,
        allow_interruptions=False,
    )
    logger.info("Greeting sent — agent is now listening")


if __name__ == "__main__":
    cli.run_app(server)
