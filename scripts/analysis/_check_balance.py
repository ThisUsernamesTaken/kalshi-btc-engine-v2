"""Throwaway: check Kalshi balance via the live trader's creds path."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Trading\btc-bias-engine")
sys.path.insert(0, r"C:\Trading\kalshi-btc-engine-v2\scripts")

from live_v5_unified import load_kalshi_creds
from kalshi_client import KalshiClient


async def go():
    kid, pem = load_kalshi_creds()
    async with KalshiClient(key_id=kid, private_key_pem=pem, demo=False) as c:
        b = await c.get_balance()
        print(f"balance_cents={b.balance} dollars={b.balance/100:.2f}")


asyncio.run(go())
