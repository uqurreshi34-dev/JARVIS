# WebSocket listener that feeds decoded swaps to a callback.

"""Subscribe to pending transactions over raw JSON-RPC WebSocket.

We bypass web3.py's async subscription API on purpose:
- In web3 v6, `AsyncWeb3(...)` is not an async context manager; only
  `AsyncWeb3.persistent_websocket(...)` is, and it has its own quirks.
- Raw JSON-RPC over `websockets` is stable and gives us precise control
  over reconnects and back-pressure.
"""
import asyncio
import itertools
import json
from typing import Awaitable, Callable

import websockets
from web3 import Web3

from actions.mev.tx_decoder import decode_swap


Handler = Callable[[dict], Awaitable[None]]


class MempoolListener:
    def __init__(self, ws_url: str, http_url: str, on_swap: Handler,
                 max_inflight: int = 64):
        self.ws_url = ws_url
        self.http = Web3(Web3.HTTPProvider(http_url))
        self.on_swap = on_swap
        self._id_counter = itertools.count(1)
        # Bound concurrency: without this, a busy mempool can spawn
        # thousands of HTTP getTransaction calls at once and DoS your RPC.
        self._sem = asyncio.Semaphore(max_inflight)
        self._tasks: set = set()
        self._stats = {
            "raw": 0, "fetched": 0, "fetch_err": 0,
            "no_to": 0, "not_swap": 0, "decoded": 0,
        }

    async def _handle_pending(self, tx_hash_hex: str):
        self._stats["raw"] += 1
        async with self._sem:
            try:
                tx = await asyncio.to_thread(
                    self.http.eth.get_transaction, tx_hash_hex
                )
            except Exception as e:
                self._stats["fetch_err"] += 1
                if self._stats["fetch_err"] <= 3:
                    print(
                        f"[mempool] get_transaction failed: {type(e).__name__}: {e}")
                return

            self._stats["fetched"] += 1

            if tx.get("to") is None:
                self._stats["no_to"] += 1
                return

            decoded = decode_swap(tx)
            if decoded is None:
                self._stats["not_swap"] += 1
                return

            self._stats["decoded"] += 1
            print(f"[mempool] decoded swap: {decoded.function} "
                  f"token={decoded.base_token[:10]} buy={decoded.is_buy} "
                  f"amount_in={decoded.amount_in}")

            try:
                await self.on_swap(decoded)
            except Exception as e:
                print(f"[mempool] on_swap error: {type(e).__name__}: {e}")

        # Print on the very first msg, then every 20 after — catches trickles
        if self._stats["raw"] == 1 or self._stats["raw"] % 20 == 0:
            print(f"[stats] {self._stats}")

    async def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def run(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=30, ping_timeout=20
                ) as ws:
                    sub_id = next(self._id_counter)
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": sub_id,
                        "method": "eth_subscribe",
                        "params": ["newPendingTransactions"],
                    }))
                    ack = json.loads(await ws.recv())
                    if "error" in ack:
                        raise RuntimeError(f"subscribe failed: {ack['error']}")
                    print(f"[mempool] subscribed id={ack.get('result')}")
                    backoff = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        params = msg.get("params") or {}
                        h = params.get("result")
                        if isinstance(h, str):
                            await self._spawn(self._handle_pending(h))
            except Exception as e:
                print(
                    f"[mempool] disconnected: {e}  (reconnecting in {backoff}s)")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
