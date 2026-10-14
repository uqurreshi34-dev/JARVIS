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
from actions.mev.tx_decoder_v3 import decode_swap_v3, ROUTER_ADDRS_V3
from actions.mev.tx_decoder_ur import decode_swap_ur


Handler = Callable[[dict], Awaitable[None]]

# Filter target: only txs going to a known router (V2 or V3).
ROUTER_ADDRS = [a.lower() for a in ROUTERS.keys()] + ROUTER_ADDRS_V3


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
        # TEMP DIAG: unique (to, selector) pairs seen since startup,
        # capped at 30 so the log stays readable. Remove once we know
        # what's actually arriving.
        self._diag_targets: set = set()
        self._stats = {
            "raw": 0, "fetched": 0, "fetch_err": 0,
            "no_to": 0, "not_swap": 0,
            "decoded_v2": 0, "decoded_v3": 0, "decoded_ur": 0,
            "dup": 0,
        }
        # Watchdog bookkeeping only. The filtered subscription proved
        # unreliable on Alchemy's free tier, so we always subscribe
        # unfiltered now — the watchdog only exists to notice a dead
        # WebSocket and force a reconnect.
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

            # Direct V2 router — fully wired (dispatches to on_swap).
            decoded_v2 = decode_swap(tx)
            if decoded_v2 is not None:
                self._stats["decoded_v2"] += 1
                print(f"[mempool] decoded swap (V2): {decoded_v2.function} "
                      f"token={decoded_v2.base_token[:10]} "
                      f"buy={decoded_v2.is_buy} "
                      f"amount_in={decoded_v2.amount_in}")
                try:
                    await self.on_swap(decoded_v2)
                except Exception as e:
                    print(f"[mempool] on_swap error: "
                          f"{type(e).__name__}: {e}")
                return

            # Direct V3 router — decoded for observation (math next).
            decoded_v3 = decode_swap_v3(tx)
            if decoded_v3 is not None:
                self._stats["decoded_v3"] += 1
                print(f"[mempool] decoded swap (V3): {decoded_v3.function} "
                      f"fee={decoded_v3.fee} "
                      f"token={decoded_v3.base_token[:10]} "
                      f"buy={decoded_v3.is_buy} "
                      f"amount_in={decoded_v3.amount_in}")
                return

            # UniversalRouter — where most modern traffic lives.
            decoded_ur = decode_swap_ur(tx)
            if decoded_ur is not None:
                self._stats["decoded_ur"] += 1
                print(f"[mempool] decoded swap (UR): {decoded_ur.function} "
                      f"fee={decoded_ur.fee} "
                      f"token={decoded_ur.base_token[:10]} "
                      f"buy={decoded_ur.is_buy} "
                      f"amount_in={decoded_ur.amount_in}")
                return

            # TEMP DIAG: log the first 30 unique (to, selector) pairs
            # that we couldn't decode. Tells us which routers are
            # actually arriving and what their selectors look like.
            if len(self._diag_targets) < 30:
                to_addr = tx.get("to")
                data = tx.get("input") or tx.get("data") or b""
                if isinstance(data, bytes):
                    sel = data[:4].hex() if data else ""
                else:
                    sel = data[2:10] if data.startswith("0x") else data[:8]
                key = (str(to_addr).lower() if to_addr else "", sel)
                if key not in self._diag_targets:
                    self._diag_targets.add(key)
                    print(f"[diag] to={to_addr} selector=0x{sel}")

            self._stats["not_swap"] += 1

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
        """Subscribe to the unfiltered firehose.

        The filtered alchemy_pendingTransactions subscription accepts at
        most two toAddress entries on the free tier before going silent,
        and modern DEX traffic splits across V2 routers, two V3 routers,
        and UniversalRouter — five addresses, minimum. Rather than
        juggle multiple subscriptions we take the full firehose and
        filter client-side (which we already do via decode_*).
        """
        sub_id = next(self._id_counter)
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": sub_id,
            "method": "eth_subscribe",
            "params": [
                "newPendingTransactions",
                True,
            ],
        }))
        ack = json.loads(await ws.recv())
        if "error" in ack:
            raise RuntimeError(f"subscribe failed: {ack['error']}")
        print(f"[mempool] subscribed (unfiltered, full tx bodies) "
              f"id={ack['result']}")

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
