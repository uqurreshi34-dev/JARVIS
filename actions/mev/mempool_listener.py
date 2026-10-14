# WebSocket listener that feeds decoded swaps to a callback.

"""Subscribe to pending transactions over raw JSON-RPC WebSocket.

Uses Alchemy's `alchemy_pendingTransactions` (server-side filtered) when
available, falling back to `newPendingTransactions` (unfiltered) if the
free tier rejects it.
"""
import asyncio
import itertools
import json
from typing import Awaitable, Callable

import websockets
from web3 import Web3

from actions.mev.tx_decoder import decode_swap, ROUTERS


Handler = Callable[[dict], Awaitable[None]]

# Filter target: only txs going to a known V2 router
ROUTER_ADDRS = [a.lower() for a in ROUTERS.keys()]


class MempoolListener:
    def __init__(self, ws_url: str, http_url: str, on_swap: Handler,
                 max_inflight: int = 64):
        self.ws_url = ws_url
        self.http = Web3(Web3.HTTPProvider(http_url))
        self.on_swap = on_swap
        self._id_counter = itertools.count(1)
        self._sem = asyncio.Semaphore(max_inflight)
        self._tasks: set = set()
        # Dedup: hashes seen recently. Bounded to prevent unbounded growth.
        self._seen: set = set()
        self._seen_order: list = []
        self._stats = {
            "raw": 0, "fetched": 0, "fetch_err": 0,
            "no_to": 0, "not_swap": 0, "decoded": 0, "dup": 0,
        }
        # Set True once we confirm the filtered subscription works.
        self._filtered = False
        # How many times the watchdog has caught a silent feed. After two,
        # we give up on the filtered subscription entirely and use the
        # unfiltered firehose. Reset only on process restart.
        self._stuck_cycles = 0
        self._last_raw_seen = 0

    # ---- dedup ---------------------------------------------------------
    def _is_duplicate(self, tx_hash: str) -> bool:
        if tx_hash in self._seen:
            return True
        self._seen.add(tx_hash)
        self._seen_order.append(tx_hash)
        if len(self._seen_order) > 5000:
            old = self._seen_order.pop(0)
            self._seen.discard(old)
        return False

    # ---- handle a single pending tx ------------------------------------
    async def _handle_pending(self, tx_or_hash):
        """Accept either a hash string or a full tx dict."""
        self._stats["raw"] += 1
        async with self._sem:
            # 1) Normalise the input into (tx_dict, hash)
            if isinstance(tx_or_hash, dict):
                tx = tx_or_hash
                h = tx.get("hash")
                tx_hash = h.hex() if isinstance(h, bytes) else (h or "")
            else:
                tx = None
                tx_hash = tx_or_hash

            if self._is_duplicate(tx_hash):
                self._stats["dup"] += 1
                return

            # 2) Only fetch if the payload didn't include the tx body
            if tx is None:
                try:
                    tx = await asyncio.to_thread(
                        self.http.eth.get_transaction, tx_hash
                    )
                except Exception:
                    self._stats["fetch_err"] += 1
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

    async def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _stats_loop(self, every: int = 15):
        prev_raw = 0
        while True:
            await asyncio.sleep(every)
            raw = self._stats["raw"]
            delta = raw - prev_raw
            prev_raw = raw
            print(f"[stats] {self._stats}  (+{delta} since last)")

    async def _watchdog(self, ws):
        """Force a reconnect if the feed goes quiet.

        Alchemy's filtered subscription on the free tier can accept the
        subscribe request, keep the socket open, and then simply never
        deliver another message. No error, no disconnect. This notices
        that (V2 router traffic on mainnet never stops for 45 seconds)
        and closes the socket, which makes run() reconnect.
        """
        while True:
            await asyncio.sleep(45)
            if ws.state.name != "OPEN":
                return
            now_raw = self._stats["raw"]
            if now_raw == self._last_raw_seen:
                self._stuck_cycles += 1
                print(
                    f"[mempool] WATCHDOG: feed silent for 45s "
                    f"(raw={now_raw}, cycle #{self._stuck_cycles}). "
                    f"Reconnecting."
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            self._last_raw_seen = now_raw

    # ---- subscribe helper ----------------------------------------------
    async def _subscribe(self, ws):
        """Try filtered subscription first, fall back to unfiltered.

        After two watchdog cycles on the filtered feed, give up on it
        entirely: it goes silent on the free tier in a way the subscribe
        call cannot detect, and we would rather have the noisy firehose
        than a quiet socket that never delivers.
        """
        # Attempt 1: server-side filter to V2 routers only — skipped
        # permanently once it has been caught being silent twice.
        if self._stuck_cycles < 2:
            sub_id = next(self._id_counter)
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": sub_id,
                "method": "eth_subscribe",
                "params": ["alchemy_pendingTransactions", {
                    "toAddress": ROUTER_ADDRS,
                }],
            }))
            ack = json.loads(await ws.recv())
            if "result" in ack:
                self._filtered = True
                print(
                    f"[mempool] subscribed (FILTERED to V2 routers) id={ack['result']}")
                return
            print(
                f"[mempool] filtered subscription rejected: {ack.get('error')}")
            print(f"[mempool] falling back to unfiltered stream")
        else:
            print(
                f"[mempool] filtered feed proven silent "
                f"({self._stuck_cycles} cycles) — using unfiltered"
            )

        # Attempt 2: unfiltered.
        sub_id = next(self._id_counter)
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": sub_id,
            "method": "eth_subscribe",
            "params": ["newPendingTransactions", {"fullTransactions": True}],
        }))
        ack = json.loads(await ws.recv())
        if "error" in ack:
            raise RuntimeError(f"subscribe failed: {ack['error']}")
        self._filtered = False
        print(f"[mempool] subscribed (unfiltered) id={ack['result']}")

    async def run(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=30, ping_timeout=20
                ) as ws:
                    await self._subscribe(ws)
                    asyncio.create_task(self._stats_loop())
                    asyncio.create_task(self._watchdog(ws))
                    # Reset the last-seen counter for this fresh connection.
                    self._last_raw_seen = self._stats["raw"]
                    backoff = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        params = msg.get("params") or {}
                        h = params.get("result")
                        # Unfiltered subs push a hash string.
                        if isinstance(h, str):
                            await self._spawn(self._handle_pending(h))
                        elif isinstance(h, dict) and h.get("hash"):
                            # Pass the whole dict — no HTTP round-trip needed
                            await self._spawn(self._handle_pending(h))
            except Exception as e:
                print(
                    f"[mempool] disconnected: {e}  (reconnecting in {backoff}s)")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
