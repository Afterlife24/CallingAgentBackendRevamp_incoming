# Autonomic Inbound Caller Agent

An AI-powered inbound phone call agent built with [LiveKit Agents](https://docs.livekit.io/agents/) for **Autonomic**. It answers incoming calls via SIP/Twilio and acts as a consultative sales assistant, recommending Autonomic's AI products based on the caller's business needs.

## How It Works

1. A phone call comes in through Twilio → SIP trunk → LiveKit room
2. The agent greets the caller and begins a consultative conversation
3. Speech-to-text (Cartesia), LLM reasoning (Groq/Llama 3.1), and text-to-speech (Cartesia) run in real time
4. The agent follows a sales flow: understand the business → identify channels → recommend products → handle interest
5. The caller or agent can end the call at any time

## Tech Stack

| Component             | Provider                                              |
| --------------------- | ----------------------------------------------------- |
| Voice Agent Framework | LiveKit Agents SDK                                    |
| STT                   | Cartesia (ink-whisper)                                |
| LLM                   | Groq (llama-3.1-8b-instant) via OpenAI-compatible API |
| TTS                   | Cartesia (sonic-3)                                    |
| VAD                   | Silero                                                |
| Turn Detection        | LiveKit Multilingual Model                            |
| Noise Cancellation    | LiveKit BVC Telephony                                 |
| SIP Trunking          | Twilio                                                |

## Prerequisites

- Python 3.10+
- A [LiveKit Cloud](https://cloud.livekit.io/) account (or self-hosted LiveKit server)
- A [Twilio](https://www.twilio.com/) account with a SIP trunk and phone number
- API keys for [Groq](https://console.groq.com/) and [Cartesia](https://cartesia.ai/)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Download model files

Download required ML model files (Silero VAD, turn detector, etc.):

```bash
python agent.py download-files
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```env
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret

GROQ_API_KEY=your_groq_api_key
CARTESIA_API_KEY=your_cartesia_api_key

SIP_INBOUND_NUMBER=+1234567890
TWIML_USERNAME=your_twiml_username
TWIML_PASSWORD=your_twiml_password

# Optional: restrict which numbers can call in (comma-separated)
INBOUND_ALLOWED_NUMBERS=+1111111111,+2222222222
```

### 4. Set up SIP trunk and dispatch rule

Run this once to create the inbound trunk and dispatch rule on LiveKit:

```bash
python dispatch_rule.py
```

If you need to start fresh (delete all trunks/rules and recreate):

```bash
python cleanup_and_setup.py
```

### 5. Run the agent

```bash
python agent.py dev
```

The agent registers as `inbound-caller` and listens for calls routed to rooms with the `call-` prefix.

## Console Mode

For local testing without a real phone call, the agent supports console mode. When the room name is `"console"`, it skips waiting for a SIP participant.

## Project Structure

```
├── agent.py              # Agent definition, session config, server entrypoint
├── prompts.py            # LLM system prompts (sales instructions, greeting)
├── dispatch_rule.py      # One-time SIP trunk + dispatch rule setup
├── cleanup_and_setup.py  # Tear down and recreate all SIP config
├── requirements.txt      # Python dependencies
├── .env                  # Secrets and config (not committed)
├── assets/
│   └── greeting.wav      # Audio greeting asset
└── KMS/
    └── logs/             # Log output
```

## Observability

The agent logs structured events during each session:

- `[CONVERSATION]` — transcribed dialogue (role + text)
- `[STATE]` — agent and user state transitions
- `[TOOL]` — function tool calls and their results
- `[STT]` — final speech-to-text transcriptions
- `[USAGE]` — model usage totals at session close
