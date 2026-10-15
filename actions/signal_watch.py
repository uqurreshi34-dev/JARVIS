"""Watching for setups: when one confirms on any Signal Desk market, JARVIS says so, unasked.

"Shall I buy or sell copper" answers when asked. This asks for you: after every fifteen-minute close,
each market in trade-plan.json's `signal_markets` is read exactly as that question reads it
(actions/trade_plan.py), and a buy or sell setup that has confirmed -- on the four-hour chart or the
fifteen-minute one -- is said once, with its entry, stop, target and how long it stays live, and the
plan opens. It goes to the phone as every unprompted announcement does, and into the journal. A setup
is live only until the next close on its own chart, so each is said once, when it confirms (or, for
one already live when JARVIS starts, then).

After every four-hour close a Signal Desk briefing is kept as well (actions/signal_desk.py), with
charts for the markets that have a setup, so there is a record of what was live and when.

Four-hour candles close on OANDA's own day, from 17:00 New York time (or on UTC's hours with
utc_candles), worked out each time so the clocks changing in either country moves nothing.

The line watch (actions/line_watch.py) is a different thing: it says when price reaches a line, for
alert_markets only. Nothing is traded here either: this reads and says.
"""

import threading
from datetime import datetime, timedelta, timezone

from actions import gold_strategy, journal, oanda, signal_desk, trade_plan

# How long after a fifteen-minute close before OANDA is asked: after the line watch's twenty seconds, so
# the two do not ask at once.
SETTLE_SECONDS = 45

# The first look after JARVIS starts, once the greeting has been said.
FIRST_LOOK_SECONDS = 90

_told = set()     # (instrument, granularity, side, line, confirmed_at) -- each setup said once
_say = None
_show = None
_stop = threading.Event()
_thread = None


def set_listeners(on_say=None, on_show=None):
    """on_say(text): speaks an unprompted announcement. on_show(found, command): opens the plan's panel."""
    global _say, _show
    _say, _show = on_say, on_show


def is_close(boundary, chosen):
    """Whether [boundary], a fifteen-minute boundary in UTC, is also when a candle of the plan's own size
    (four hours, unless set otherwise) closes."""
    step = trade_plan.GRANULARITY_SECONDS[chosen["granularity"]] // 60

    if chosen["utc_candles"]:
        minutes = boundary.hour * 60 + boundary.minute
    else:
        local = gold_strategy.new_york_time(boundary)
        minutes = ((local.hour - 17) % 24) * 60 + local.minute

    return minutes % step == 0


def live(found):
    """[(page, side, plan)]: the setups confirmed on the last close of either page -- the four-hour plan
    and the fifteen-minute one -- that the rules say to take (not skip)."""
    setups = []

    for page in (found, found.get("entry")):
        if page and page.get("verdict") in ("buy", "sell") and page.get(page["verdict"]):
            setups.append((page, page["verdict"], page[page["verdict"]]))

    return setups


def words(found, page, side, plan):
    """What JARVIS says about one setup: where, the figures, and until when it is live."""
    d = found["decimals"]

    def price(value):
        return f"{value:,.{0 if value >= 1000 else d}f}"

    step = timedelta(seconds=trade_plan.GRANULARITY_SECONDS[page["granularity"]])
    until = trade_plan._uk(plan["confirmed_at"] + step, "%H:%M") if plan.get("confirmed_at") else None
    said = (f"{found['instrument']}: a {side} setup has confirmed on the {page['timeframe']} chart, at"
            f" {price(plan['line'])}, sir. In near {price(plan['entry'])}, stop {price(plan['stop'])}, target"
            f" {price(plan['target'])}, {plan['ratio']:.1f} to 1.")

    if until:
        said += f" It is live until the next {page['timeframe']} close, {until} UK, and not chased after."

    if page is not found:
        said += (f" The {found['timeframe']} chart has confirmed it too." if found.get("verdict") == side
                 else f" The {found['timeframe']} chart has not confirmed it yet.")

    return said


def check(client=None, brief=False):
    """One look at every Signal Desk market: say any setup newly confirmed, open the first one's plan, and
    -- with [brief] -- keep a briefing. Returns what was said, a list of sentences."""
    chosen = trade_plan.settings()
    markets = chosen.get("signal_markets") or ()

    if not markets:
        return []

    results = signal_desk.scan(markets, client)

    # The markets shut (a weekend): nothing has happened, so nothing to say or keep.
    if not any(found and found.get("live") for _name, found in results):
        return []

    said, first = [], None

    for name, found in results:
        if not found or not found.get("live"):
            continue

        for page, side, plan in live(found):
            key = (found["symbol"], page["granularity"], side, plan["line"], plan.get("confirmed_at"))

            if key in _told:
                continue

            _told.add(key)
            sentence = words(found, page, side, plan)

            if first is None:
                first = (found, name)

                if _show:
                    sentence += " The plan is on the panel."

            said.append(sentence)
            journal.alert(sentence)

    if first and _show:
        _show(first[0], f"the {first[1]} plan")

    for sentence in said:
        if _say:
            _say(sentence)

    # A day on, every setup said has long since passed its next close.
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)

    for key in [key for key in _told if key[4] is not None and key[4] < day_ago]:
        _told.discard(key)

    if brief:
        path, _text = signal_desk.write(results, every_chart=False)
        print(f"[JARVIS] signal watch: briefing kept in {path}")

    return said


def next_look(now):
    """When to look next: [SETTLE_SECONDS] after the next fifteen-minute close, and that close."""
    boundary = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 15) + timedelta(minutes=15)
    return boundary + timedelta(seconds=SETTLE_SECONDS), boundary


def _run():
    first = True

    while not _stop.is_set():
        now = datetime.now(timezone.utc)

        if first:
            wait, boundary = FIRST_LOOK_SECONDS, None
        else:
            look, boundary = next_look(now)
            wait = (look - now).total_seconds()

        if _stop.wait(max(1.0, wait)):
            return

        try:
            check(brief=boundary is not None and is_close(boundary, trade_plan.settings()))
        except Exception as error:   # noqa: BLE001 - a bad look must never stop the watching
            print(f"[JARVIS] signal watch: {error}")

        first = False


def start():
    """Watch in the background while JARVIS runs, if OANDA is set up and a market is named."""
    global _thread

    if _thread or not oanda.configured() or not trade_plan.settings().get("signal_markets"):
        return False

    _stop.clear()
    _thread = threading.Thread(target=_run, name="signal-watch", daemon=True)
    _thread.start()
    return True


def stop():
    global _thread
    _stop.set()
    _thread = None
