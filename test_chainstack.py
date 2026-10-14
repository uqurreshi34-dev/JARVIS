"""Throwaway: verify Chainstack delivers full tx bodies on the WS feed."""
import asyncio
import json

import websockets


WS_URL = "wss://ethereum-mainnet.core.chainstack.com/e6eeccc12a117b7083379f48a513f5b3"


async def test():
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": ["newPendingTransactions", True],
        }))
        ack = json.loads(await ws.recv())
        print("Subscribed:", ack.get("result", ack))

        if "result" not in ack:
            return

        count = 0
        async for raw in ws:
            msg = json.loads(raw)
            tx = (msg.get("params") or {}).get("result")
            if not isinstance(tx, dict):
                continue
            tx_hash = tx.get("hash")
            if not tx_hash:
                continue
            count += 1
            to_addr = tx.get("to") or "(contract creation)"
            data = tx.get("input") or ""
            selector = str(data)[:10] if isinstance(data, str) else "(bytes)"
            print(f"[{count}] to={to_addr} selector={selector}")
            if count >= 10:
                break


asyncio.run(test())
