"""Safety guard: hard limits on bundle size, daily loss, wallet floor.

Every bundle must pass `check_before_bundle`. Any trip sets a halt flag
that can only be cleared by restarting the process. This is intentional —
if something is very wrong, we want a human to look, not a loop to keep
firing.
"""
import time
from datetime import date
from web3 import Web3


class SafetyGuard:
    def __init__(
        self,
        max_bundle_eth: float,
        max_daily_loss_eth: float,
        max_consec_failures: int,
        min_wallet_balance_eth: float,
        enabled: bool = True,
    ):
        self.max_bundle_wei = Web3.to_wei(max_bundle_eth, "ether")
        self.max_daily_loss_wei = Web3.to_wei(max_daily_loss_eth, "ether")
        self.max_consec_failures = max_consec_failures
        self.min_wallet_wei = Web3.to_wei(min_wallet_balance_eth, "ether")
        self.enabled = enabled

        self._today = date.today()
        self._today_pnl_wei = 0
        self._consec_failures = 0
        self._halted = False
        self._halted_reason = ""
        self._started = time.time()

    # ---- internal -------------------------------------------------------
    def _roll_day_if_needed(self):
        today = date.today()
        if today != self._today:
            print(
                f"[safety] day rollover: yesterday pnl={self._today_pnl_wei/1e18:.6f} ETH")
            self._today = today
            self._today_pnl_wei = 0
            self._consec_failures = 0
            # Note: don't un-halt here — a halt persists until restart.

    # ---- public API -----------------------------------------------------
    def check_before_bundle(self, front_in_wei: int, wallet_wei: int) -> tuple[bool, str]:
        """Return (allow, reason_if_blocked)."""
        if not self.enabled:
            return False, "MEV_ENABLED is false"
        if self._halted:
            return False, f"halted: {self._halted_reason}"

        self._roll_day_if_needed()

        if front_in_wei > self.max_bundle_wei:
            return False, (f"bundle {front_in_wei/1e18:.4f} ETH "
                           f"> max {self.max_bundle_wei/1e18:.4f} ETH")

        if wallet_wei < self.min_wallet_wei:
            return False, (f"wallet {wallet_wei/1e18:.4f} ETH "
                           f"< floor {self.min_wallet_wei/1e18:.4f} ETH")

        if self._today_pnl_wei <= -self.max_daily_loss_wei:
            self._halted = True
            self._halted_reason = "daily loss limit reached"
            return False, self._halted_reason

        return True, ""

    def record_result(self, net_profit_wei: int, success: bool):
        """Call once per bundle, with its real outcome.

        Counts toward the consecutive-failure halt:
          - A landed bundle that lost money (systematic bugs like a
            honeypot token or wrong router).
          - A relay rejection (broken submission path).

        Does NOT count:
          - A bundle that never got included (outbid, block moved past).
            That's normal competition, not a bug.

        A success resets the counter.
        """
        self._roll_day_if_needed()
        self._today_pnl_wei += net_profit_wei

        if success:
            self._consec_failures = 0
        else:
            self._consec_failures += 1
            if self._consec_failures >= self.max_consec_failures:
                self._halted = True
                self._halted_reason = f"{self._consec_failures} consecutive landed losses"
                print(f"[safety] HALTED: {self._halted_reason}")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "halted": self._halted,
            "reason": self._halted_reason,
            "today_pnl_eth": round(self._today_pnl_wei / 1e18, 6),
            "consec_failures": self._consec_failures,
            "uptime_sec": int(time.time() - self._started),
        }
