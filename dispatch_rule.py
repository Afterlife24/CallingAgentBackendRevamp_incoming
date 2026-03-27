"""
Creates the SIP inbound trunk and dispatch rule on LiveKit.

Run once to set up the connection between Twilio, your phone number, and the agent:
    python dispatch_rule.py

Requires in .env:
    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
    SIP_INBOUND_NUMBER, TWIML_USERNAME, TWIML_PASSWORD
    INBOUND_ALLOWED_NUMBERS (optional, comma-separated)
"""

import asyncio
import os
import logging
from typing import List, Optional

from dotenv import load_dotenv
from livekit import api

try:
    from livekit.api.twirp_client import TwirpError  # type: ignore
except Exception:
    TwirpError = Exception  # type: ignore

load_dotenv()
logger = logging.getLogger("dispatch-setup")
logging.basicConfig(level=logging.INFO)

AGENT_NAME = "inbound-caller"


def _parse_allowed_numbers() -> Optional[List[str]]:
    allowed = os.getenv("INBOUND_ALLOWED_NUMBERS", "").strip()
    if not allowed:
        return None
    numbers = [n.strip() for n in allowed.split(",") if n.strip()]
    return numbers or None


async def main():
    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    phone_number = os.getenv("SIP_INBOUND_NUMBER")
    if not phone_number:
        logger.error("SIP_INBOUND_NUMBER not set in .env")
        await lk.aclose()
        return

    try:
        # ── 1. Create inbound trunk (with Twilio auth) ──────────────
        allowed_numbers = _parse_allowed_numbers()

        trunk_info = api.SIPInboundTrunkInfo(
            name="livekit-trunk",
            numbers=[phone_number],
            auth_username=os.getenv("TWIML_USERNAME"),
            auth_password=os.getenv("TWIML_PASSWORD"),
            krisp_enabled=True,
            allowed_numbers=allowed_numbers,  # type: ignore[attr-defined]
        )

        try:
            trunk = await lk.sip.create_inbound_trunk(
                api.CreateSIPInboundTrunkRequest(trunk=trunk_info)
            )
            trunk_id = trunk.sip_trunk_id
            logger.info(f"✅ Inbound trunk created: {trunk_id}")
        except TwirpError as e:
            if "Conflicting inbound SIP Trunks" in str(e):
                logger.warning(
                    "Inbound trunk already exists or conflicts — skipping creation.")
                # List existing trunks to find the ID
                trunks = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
                trunk_id = None
                for t in trunks.items:
                    if phone_number in (t.numbers or []):
                        trunk_id = t.sip_trunk_id
                        break
                if not trunk_id:
                    logger.error(
                        "Could not find existing trunk ID. Create it manually.")
                    return
                logger.info(f"Using existing trunk: {trunk_id}")
            else:
                raise

        # ── 2. Create dispatch rule ──────────────────────────────────
        rule = api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                room_prefix="call-",
            )
        )

        try:
            dispatch = await lk.sip.create_dispatch_rule(
                api.CreateSIPDispatchRuleRequest(
                    dispatch_rule=api.SIPDispatchRuleInfo(
                        rule=rule,
                        name="Inbound call dispatch",
                        trunk_ids=[trunk_id],
                        room_config=api.RoomConfiguration(
                            agents=[
                                api.RoomAgentDispatch(agent_name=AGENT_NAME)
                            ]
                        ),
                    )
                )
            )
            logger.info(
                f"✅ Dispatch rule created: {dispatch.sip_dispatch_rule_id}")
        except TwirpError as e:
            if "already exists" in str(e):
                logger.info("✅ Dispatch rule already exists — skipping.")
            else:
                raise

        logger.info(
            f"\nSetup complete! Calls to {phone_number} → agent '{AGENT_NAME}'")

    except Exception as e:
        logger.error(f"Setup failed: {e}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
