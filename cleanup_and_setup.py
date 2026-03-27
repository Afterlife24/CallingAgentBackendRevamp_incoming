"""
Lists all trunks + dispatch rules, deletes them all, then recreates fresh.
Run once: python cleanup_and_setup.py
"""

import asyncio
import os
import logging

from dotenv import load_dotenv
from livekit import api

load_dotenv()
logger = logging.getLogger("cleanup")
logging.basicConfig(level=logging.INFO)

AGENT_NAME = "inbound-caller"


async def main():
    lk = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    try:
        # ── Delete ALL dispatch rules ────────────────────────────────
        rules = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        for r in rules.items:
            try:
                await lk.sip.delete_dispatch_rule(
                    api.DeleteSIPDispatchRuleRequest(
                        sip_dispatch_rule_id=r.sip_dispatch_rule_id)
                )
                logger.info(
                    f"🗑️  Deleted dispatch rule: {r.sip_dispatch_rule_id} ({r.name})")
            except Exception as e:
                logger.warning(
                    f"Could not delete dispatch rule {r.sip_dispatch_rule_id}: {e}")

        # ── Delete ALL inbound trunks ────────────────────────────────
        trunks = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        for t in trunks.items:
            try:
                await lk.sip.delete_trunk(
                    api.DeleteSIPTrunkRequest(sip_trunk_id=t.sip_trunk_id)
                )
                logger.info(f"🗑️  Deleted trunk: {t.sip_trunk_id} ({t.name})")
            except Exception as e:
                logger.warning(f"Could not delete trunk {t.sip_trunk_id}: {e}")

        # ── Create new trunk WITH Twilio auth ────────────────────────
        phone_number = os.getenv("SIP_INBOUND_NUMBER")
        trunk = await lk.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(
                    name="livekit-trunk",
                    numbers=[phone_number],
                    auth_username=os.getenv("TWIML_USERNAME"),
                    auth_password=os.getenv("TWIML_PASSWORD"),
                    krisp_enabled=True,
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        logger.info(f"✅ New trunk created: {trunk_id}")

        # ── Create new dispatch rule ─────────────────────────────────
        dispatch = await lk.sip.create_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(
                dispatch_rule=api.SIPDispatchRuleInfo(
                    rule=api.SIPDispatchRule(
                        dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                            room_prefix="call-",
                        )
                    ),
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
            f"✅ New dispatch rule created: {dispatch.sip_dispatch_rule_id}")
        logger.info(f"\nDone! Calls to {phone_number} → agent '{AGENT_NAME}'")

    except Exception as e:
        logger.error(f"Failed: {e}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
