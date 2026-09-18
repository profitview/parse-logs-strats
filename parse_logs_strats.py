#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
ProfitView Trading Log Parser
Parses profitview log files and extracts trade entry/exit statistics per strategy.
"""

import re
import io
import os
import sys
import glob
import csv
import json
import math
import time
import shutil
import fnmatch
import argparse
import subprocess
import contextlib
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from html import escape
from typing import Optional


# ══ CONFIGURATION ═════════════════════════════════════════════════════════════
# Everything intended to be tweaked by hand lives in this block.

# ── Names of the fields inside the '2: #...' data line ────────────────────────
# The line looks like:  2: #open,high,low,close,volume,side:1,q:7.91,l:53,tp:...
# Rename these if the alert payload ever uses different keys. A field that is
# not found simply comes back as None (its dependent stats then read "—").
FIELD_SIDE = "side"   # long: 1 / 1:0 / long / buy — short: -1 / 0:1 / short / sell
FIELD_QTY  = "q"      # position size, in percent of the account
FIELD_LEV  = "l"      # leverage multiplier (optional, see DEFAULT_LEVERAGE)
FIELD_TP   = "tp"     # take-profit price (TP1 — the level the _TP1 exit fires at)
FIELD_SL   = "sl"     # stop-loss price

# Leverage assumed for an entry whose alert carries no FIELD_LEV value. At 1.0
# the P/L figures are unleveraged returns — still fine for comparing strategies.
DEFAULT_LEVERAGE = 1.0

# ── Signal classification ─────────────────────────────────────────────────────
EXIT_SUFFIXES = ("_TP1", "_SPIKE", "_SL", "_TIMEOUT")

# Signals to silently ignore. Entries are case-insensitive glob patterns
# (same syntax as --strat), so a plain name matches only itself while
# "*_DEBUG" matches any signal with that suffix.
IGNORED_SIGNALS = (
    "bal",
    "foo",
    "*_DEBUG",
    "*_FILTER",
    "*_FILTERED",
    "XXX",

    "OLD_EXF6B",
    "OLD_EXF5B",
    "OLD_EEV9",
    "TEST_EEV9",
    "TEST_EDF3B",
    "TEST_EXF5B",
    "TEST2_EXF5B",
    "TEST_EXF3B",
)

# ── Telegram commands ─────────────────────────────────────────────────────────
# Signals sent through the Telegram bot carry no market/timeframe header; they
# come from the signal line's inline parameters instead, e.g.
#   1: EDF3B(e=bybit,s=dogeusdt,res=5m)
# Keys are matched case-insensitively.
TELEGRAM_FIELD_MARKET    = "s"
TELEGRAM_FIELD_TIMEFRAME = "res"

# Market assumed for a Telegram signal without a TELEGRAM_FIELD_MARKET parameter,
# as a bare symbol like "BTCUSDT". None leaves the market unknown.
TELEGRAM_DEFAULT_MARKET = "BTCUSDT"

# A manually sent exit carries no candle, so it has no exit price. ProfitView
# usually queries the position right after, and that response's 'markPrice' is
# within a few ticks of where the position actually closed. This is how many log
# lines after the command are searched for one; 0 turns the fallback off.
# The window deliberately reaches past the end of the command's own output: the
# position query sometimes belongs to the next command a few seconds later. Only
# a markPrice whose symbol matches the signal's market is accepted, so a wide
# window cannot pick up an unrelated instrument's price.
TELEGRAM_MARK_PRICE_LINES = 150

# ...and how old that mark price may be, in seconds. A quiet log can put the next
# position query minutes or hours later, where the price says nothing about the
# exit any more; the line window alone does not catch that.
TELEGRAM_MARK_PRICE_SECONDS = 300

# An alert that was disabled in ProfitView still fires and is logged, and is then
# often re-sent by hand through Telegram with the same payload pasted in. That is
# one signal, not two, but it would otherwise open a second trade and close the
# first as a FLIP seconds later. Within this many seconds, a Telegram signal that
# repeats an earlier one — same name, side, price, size, TP and SL — is dropped.
# Requiring the whole payload to match is what makes a window this wide safe: a
# genuine second signal at the very same price, size and levels does not happen.
# 0 turns the de-duplication off.
TELEGRAM_DEDUPE_SECONDS = 3600

# ── Repeated alerts ───────────────────────────────────────────────────────────
# An alert configured twice in TradingView fires twice for the same bar, a
# fraction of a second apart and from two alert IDs. ProfitView acts on the first
# and its filters swallow the rest, so a repeat within this many seconds — same
# name, side and price — is dropped here too. Unlike the Telegram case the rest
# of the payload is not compared: the two copies are computed moments apart, so
# size and levels can differ slightly.
# This is why the window must stay tiny. Every repeat in the author's log arrived
# within 0.8s, which is far below even a 1m bar; widening it towards one bar
# would start swallowing genuine signals at an unchanged price.
DUPLICATE_ALERT_SECONDS = 5

# ── Trade filters ─────────────────────────────────────────────────────────────
# A trade that breaks one of these rules is treated as an erroneous or stale
# opening entry and handled according to FILTER_ACTION (the P/L filter always
# removes, see MAX_TRADE_PNL_PCT). Each can be overridden
# on the command line (--no-flips / --allow-flips, --max-trade-hours,
# --max-trade-pnl, --filter-action).

# Whether a repeated entry signal may close the open trade as a FLIP. When False,
# the trade that was open at the flip is filtered instead; the new entry still
# opens a trade of its own.
ALLOW_FLIPS = True

# Trades open longer than this are filtered. That covers trades closed after the
# limit and trades still open at the end of the log (measured to the last alert
# in the data). 0 or None disables the check.
MAX_TRADE_HOURS = 168            # 7 days

# Trades whose closed account P/L exceeds this many percent in either direction
# (profit or loss) are implausible: the trade was never really placed. They are
# always removed, whatever FILTER_ACTION says, since closing them as a stop-loss
# would book a loss that never happened. 0 or None disables the check.
MAX_TRADE_PNL_PCT = 10.0

# What happens to a trade caught by the flip or max-time filter:
#   "remove" — drop it completely
#   "sl"     — close it as a stop-loss at the entry's 'sl:' price. The close time
#              is the flip time, or entry + MAX_TRADE_HOURS for an over-age trade.
#              Trades without 'sl:' data are removed instead.
FILTER_ACTIONS = ("remove", "sl")
FILTER_ACTION  = "remove"

# ── Risk-adjusted return ──────────────────────────────────────────────────────
# Days per year used to annualise the Sortino ratio (daily ratio * sqrt(this)).
# 365 because crypto trades every calendar day; use 252 for exchange-hours
# markets. The target (minimum acceptable) return is 0% per day.
TRADING_DAYS_PER_YEAR = 365

# ── Console report defaults ───────────────────────────────────────────────────
# Which column the STRATEGY SUMMARY table is sorted by. Must be one of:
#   name, market, entries, profit, loss, spike, timeout, winrate, rr, pnl, avgPnl,
#   avgMonth, sortino
# (the same keys as the HTML report, where the two tables share a column).
# Strategies with no value for the column always sink to the bottom.
# Override per run with --sort.
CONSOLE_SORT_COLUMNS = (
    "name", "market", "entries", "profit", "loss", "spike", "timeout",
    "winrate", "rr", "pnl", "avgPnl", "avgMonth", "sortino",
)
CONSOLE_DEFAULT_SORT      = "pnl"   # default: compounded account P/L
CONSOLE_DEFAULT_SORT_DESC = True    # True = highest first (Z→A for text columns)

# ── HTML report defaults ──────────────────────────────────────────────────────
# Which column the table is sorted by when the page opens. Override per run
# with --html-sort. Must be one of:
#   name, entries, profit, loss, spike, timeout, winrate, pnl, avgPnl, avgMonth,
#   sortino, dur
HTML_SORT_COLUMNS = (
    "name", "entries", "profit", "loss", "spike", "timeout",
    "winrate", "pnl", "avgPnl", "avgMonth", "sortino", "dur",
)
HTML_DEFAULT_SORT     = "pnl"    # default: compounded account P/L
HTML_DEFAULT_SORT_DESC = True    # True = highest first
HTML_EXPAND_TOP_ROW   = True     # expand the leading strategy's detail on load
HTML_EQUITY_START     = 100.0    # starting balance for the equity curve
HTML_LOG_SCALE_DURATION = True   # start with log-scaled duration bars (trade times
                                 # span seconds to weeks, which a linear bar squashes)

# Date pre-selected in the report's "From" filter on first load, as YYYY-MM-DD,
# or one of the Quick range keys in HTML_FROM_PRESETS (e.g. "31d") to open on
# that preset instead; it is resolved in the browser against the last day in the
# data, just like picking it from the dropdown.
# "" means the earliest day in the data, i.e. show everything. A date before the
# data starts is clamped to the first day; one after it ends is rejected with a
# warning, since it would open the report on an empty table.
# "Reset filters" returns here, not to the full range. Override per run with
# --html-from (pass --html-from "" to open on the full range).
HTML_DEFAULT_FROM_DATE = "6m"

# The value="…" keys of the report's Quick range dropdown, as understood by
# presetRange() in the page script. Keep the three in sync.
HTML_FROM_PRESETS = ("7d", "31d", "3m", "6m", "month", "quarter", "year")

# Logo shown in place of the word "ProfitView" in the page heading. Two files,
# because the artwork has to invert with the theme. Set either to "" to fall
# back to plain text. These are remote URLs: with no network the alt text
# ("ProfitView") shows instead, so the heading still reads correctly.
HTML_LOGO_FOR_LIGHT_THEME = "https://profitview.app/logo/logo-wide-dark-trans.png"
HTML_LOGO_FOR_DARK_THEME  = "https://profitview.app/logo/logo-wide-bright-trans.png"
HTML_LOGO_HEIGHT_PX       = 34   # rendered height; width follows the 1010x193 ratio

# ══ END CONFIGURATION ═════════════════════════════════════════════════════════


# ── Regex patterns ────────────────────────────────────────────────────────────

# Matches the timestamp line above the signal, e.g.:
#   2026-03-02 19:59:30.857 NOTICE [47162080552] Received Alert (S) for BYBIT:BTCUSDT 5m @ ...
#   2026-09-16 23:26:46.590 NOTICE [TelegramBot:110492452] Received commands from @user ...
# The kind (S)/(A) and the market/timeframe are optional groups: a header that
# does not carry them still has to match, or its signal would be lost entirely.
# Group 4 is set only for a Telegram header.
ALERT_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+NOTICE\b.*Received\s+(?:"
    r"Alert(?:\s+\([A-Z]\))?(?:\s+for\s+(\S+)\s+(\S+))?"
    r"|(commands)\s+from\b)"
)

# One 'key:value' or 'key=value' item of a parameter list.
PARAM_RE = re.compile(r"\s*([^:=]+?)\s*[:=](.*)")

# TradingView's perpetual-contract suffix, as in DOGEUSDT.P.
PERP_SUFFIX = ".P"

# 'markPrice: 64140.95' and 'symbol: BTCUSDT' in an exchange response, which the
# log prints either as one line per field or as one line per position. Both
# layouts put the symbol before the mark price.
MARK_PRICE_RE = re.compile(r"\bmarkPrice:\s*([0-9]*\.?[0-9]+)")
SYMBOL_RE     = re.compile(r"\bsymbol:\s*([^\s,]+)")

# Leading timestamp of a log line. Response bodies are printed over many
# unstamped continuation lines, so the last match is the time of what follows.
LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")

# The source system writes this when it has no market to report; it is not a
# symbol, so it is treated as "unknown" rather than displayed.
UNKNOWN_MARKET = "undefined"

# Quote currency trimmed off displayed symbols: BTCUSDT reads as BTC. Only this
# exact suffix is stripped — see StrategyStats.market_label.
QUOTE_SUFFIX = "USDT"

# Matches the signal line, e.g.:  1: XPX4_TP1   or   1: XPX4(side:1,q:3,l:50)
# Group 1 is the bare strategy name; group 2 the optional inline parameters,
# which override same-named parameters from the data line. A leading '!' or '!!'
# tells the app to skip its filtering; it is not part of the name (!XPX3 = XPX3).
SIGNAL_LINE_RE = re.compile(r"^1:\s+!{0,2}([^\s(!][^\s(]*)(?:\((.*)\))?\s*$")

# Matches the data line, e.g.:  2: #82674.6,82740.6,82674.6,82730.1,...,side:1,...
# The parameter tail (group 6) is optional: with inline parameters on the signal
# line, the data line may carry only the OHLCV candle.
DATA_LINE_RE = re.compile(r"^2:\s+#([^,]+),([^,]+),([^,]+),([^,]+),([^,]+)(?:,(.*))?")

# Every notation the trading tool accepts for the side parameter, lowercased.
# '1:0' / '0:1' are long:short flags. Anything not listed reads as unknown.
SIDE_VALUES = {
    "1": 1,    "+1": 1,     "-1": -1,
    "1:0": 1,  "0:1": -1,
    "long": 1, "short": -1,
    "buy": 1,  "sell": -1,
}


# ── Console progress and paging ───────────────────────────────────────────────
# Progress, clock() and human() are copied verbatim from merge_logs.py rather
# than imported: both tools are standalone single-file scripts meant to be
# dropped anywhere on their own, and an import would make this one refuse to run
# without its sibling. Keep the two copies in step when either is touched.


class Progress:
    """Single-line progress bar on stderr, redrawn in place.

    Dependency-free on purpose. Draws nothing when stderr is not a terminal, so
    output redirected to a file stays free of carriage-return noise.
    """

    REFRESH = 0.1  # seconds between redraws

    def __init__(self, label, total, enabled=True):
        self.label = label
        self.total = max(total, 1)
        self.done = 0
        self.enabled = enabled and sys.stderr.isatty()
        self.start = time.monotonic()
        self._last_draw = 0.0
        # Legacy Windows code pages cannot encode block characters.
        try:
            "\u2588\u2591".encode(sys.stderr.encoding or "ascii")
            self.fill, self.empty = "\u2588", "\u2591"
        except (UnicodeEncodeError, LookupError):
            self.fill, self.empty = "#", "-"

    def advance(self, n):
        self.done += n
        if self.enabled:
            now = time.monotonic()
            if now - self._last_draw >= self.REFRESH:
                self._last_draw = now
                self._draw(now)

    def write(self, message):
        """Print a message above the bar without garbling it."""
        if not self.enabled:
            print(message, file=sys.stderr)
            return
        cols = shutil.get_terminal_size((80, 20)).columns
        sys.stderr.write("\r" + message.ljust(cols - 1) + "\n")
        self._draw(time.monotonic())

    def close(self):
        """Mark complete (scans may legitimately stop early) and end the line."""
        self.done = self.total
        if self.enabled:
            self._draw(time.monotonic(), final=True)
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _draw(self, now, final=False):
        frac = min(self.done / self.total, 1.0)
        elapsed = now - self.start
        if final:
            tail = f"{human(self.total):>8}  done in {clock(elapsed)}"
        else:
            rate = self.done / elapsed if elapsed > 0 else 0
            eta = (self.total - self.done) / rate if rate else None
            tail = (
                f"{human(self.done):>8}/{human(self.total)}  "
                f"{human(rate):>8}/s  ETA {clock(eta)}"
            )
        stats = f" {frac:6.1%}  {tail}"
        cols = shutil.get_terminal_size((80, 20)).columns
        # Size from the widest (in-progress) stats text so the bar keeps its
        # width when the final "done" line replaces it.
        width = max(10, min(40, cols - len(self.label) - 60))
        filled = int(width * frac)
        bar = self.fill * filled + self.empty * (width - filled)
        line = f"  {self.label} |{bar}|{stats}"
        # Pad instead of using ANSI erase codes: legacy Windows consoles
        # print those literally.
        sys.stderr.write("\r" + line[: cols - 1].ljust(cols - 1))
        sys.stderr.flush()


def clock(seconds):
    if seconds is None:
        return "--:--"
    m, sec = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def page_text(text: str) -> None:
    """
    Show `text` through a pager so a long report can be scrolled instead of
    flying past. Falls back to plain output whenever paging is not possible —
    no pager on PATH, or it could not be started — so the report is never lost.

    $PAGER wins if set. Otherwise `less -RFX`: -R passes control characters
    through, -F skips the pager entirely when the report already fits on one
    screen, and -X leaves the output on screen after quitting instead of
    restoring the terminal and wiping it.
    """
    env_pager = os.environ.get("PAGER", "").strip()
    cmd = shell = None
    if env_pager:
        # $PAGER runs through the shell so its arguments work as the user wrote
        # them, which means the executable has to be checked here: a command the
        # shell cannot find still starts the shell *successfully*, so Popen
        # raises nothing and the report would vanish into a failed cmd.exe.
        exe = env_pager.split()[0].strip("\"'")
        if shutil.which(exe):
            cmd, shell = env_pager, True
        else:
            print(f"  [note] $PAGER '{exe}' not found; printing directly.", file=sys.stderr)
            sys.stdout.write(text)
            return
    elif shutil.which("less"):
        cmd, shell = ["less", "-RFX"], False
    elif shutil.which("more"):
        cmd, shell = ["more"], False
    else:
        sys.stdout.write(text)
        return

    try:
        proc = subprocess.Popen(cmd, shell=shell, stdin=subprocess.PIPE,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  [note] could not start pager ({exc}); printing directly.", file=sys.stderr)
        sys.stdout.write(text)
        return

    try:
        proc.communicate(text)
    except (BrokenPipeError, KeyboardInterrupt):
        # Quitting the pager early closes the pipe. That is a normal way to
        # finish reading, not an error.
        try:
            proc.terminate()
        except OSError:
            pass


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Trade:
    strategy:    str
    entry_time:  datetime
    entry_price: Optional[float] = None   # close price from entry signal data line
    direction:   Optional[int]   = None   # 1 = long, -1 = short
    exit_time:   Optional[datetime] = None
    exit_type:   Optional[str]   = None   # "TP1", "SPIKE", "SL", "TIMEOUT", "FLIP"
    exit_price:  Optional[float] = None   # close price from exit signal data line
    qty_pct:     Optional[float] = None   # 'q:' — position size as % of account (from entry)
    leverage:    Optional[float] = None   # 'l:' — leverage multiplier (from entry)
    tp_price:    Optional[float] = None   # 'tp:' — take-profit target (from entry)
    sl_price:    Optional[float] = None   # 'sl:' — stop-loss level (from entry)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.exit_time:
            return (self.exit_time - self.entry_time).total_seconds()
        return None

    @property
    def profitable(self) -> Optional[bool]:
        """
        If we have both entry and exit prices + direction, use price logic:
          long  → profitable if exit > entry
          short → profitable if exit < entry
        Otherwise fall back to signal-type heuristic.
        """
        if (self.entry_price is not None
                and self.exit_price is not None
                and self.direction is not None):
            if self.direction == 1:
                return self.exit_price > self.entry_price
            else:
                return self.exit_price < self.entry_price
        # Fallback: signal-type heuristic
        if self.exit_type in ("TP1", "SPIKE"):
            return True
        if self.exit_type in ("SL", "FLIP", "TIMEOUT"):
            return False
        return None

    @property
    def price_change_pct(self) -> Optional[float]:
        """Directional price move between entry and exit, in percent."""
        if (self.entry_price is None or self.exit_price is None
                or self.direction is None or not self.entry_price):
            return None
        return (self.exit_price / self.entry_price - 1.0) * self.direction * 100.0

    @property
    def pnl_fraction(self) -> Optional[float]:
        """
        Effect of this trade on the account, as a fraction (0.045 = +4.5%).

        The position's notional is (qty_pct / 100) * leverage times the account,
        so the account return is the price move scaled by that exposure.
        """
        move = self.price_change_pct
        if move is None or self.qty_pct is None or self.leverage is None:
            return None
        return (move / 100.0) * (self.qty_pct / 100.0) * self.leverage

    @property
    def pnl_pct(self) -> Optional[float]:
        """Effect of this trade on the account, in percent."""
        f = self.pnl_fraction
        return None if f is None else f * 100.0

    # ── Planned risk / reward, as configured at entry ─────────────────────────
    # These describe the trade's *setup*, not its outcome: the distance from the
    # entry price to the take-profit and to the stop-loss. They are read from the
    # entry alert, before any trailing of the stop.

    @property
    def reward_pct(self) -> Optional[float]:
        """Distance from entry to take-profit, in percent of entry price."""
        if (self.tp_price is None or self.entry_price is None
                or self.direction is None or not self.entry_price):
            return None
        return (self.tp_price - self.entry_price) * self.direction / self.entry_price * 100.0

    @property
    def risk_pct(self) -> Optional[float]:
        """Distance from entry to stop-loss, in percent of entry price."""
        if (self.sl_price is None or self.entry_price is None
                or self.direction is None or not self.entry_price):
            return None
        return (self.entry_price - self.sl_price) * self.direction / self.entry_price * 100.0

    @property
    def risk_reward(self) -> Optional[float]:
        """
        Planned reward-to-risk ratio, e.g. 1.65 = target is 1.65x the stop distance.

        None when either leg is missing or non-positive — a target on the wrong
        side of entry, or a stop that is not actually protective, is not a
        meaningful ratio and must not be averaged in.
        """
        reward, risk = self.reward_pct, self.risk_pct
        if reward is None or risk is None or risk <= 0 or reward <= 0:
            return None
        return reward / risk


def compound_return(trades) -> Optional[float]:
    """
    Combined account return of a set of trades, as a fraction.

    Returns compound rather than add: a +50% trade followed by a -50% trade
    leaves the account at 1.5 * 0.5 = 0.75, i.e. -25%, not break-even. So this
    is prod(1 + r) - 1 over every trade with a computable P/L. Because it is a
    product it does not depend on trade order, which is what makes it safe to
    apply to any filtered subset.

    Trades without q/l or without both prices are skipped (they contribute a
    neutral factor of 1.0); returns None if no trade had a computable P/L.
    """
    factor = 1.0
    seen   = False
    for t in trades:
        f = t.pnl_fraction
        if f is not None:
            factor *= (1.0 + f)
            seen = True
    return (factor - 1.0) if seen else None


# Average calendar month, in days. Periods are measured in these fractional
# months rather than counted by calendar, so a 45-day window is 1.48 months.
DAYS_PER_MONTH = 365.25 / 12


def period_months(trades) -> float:
    """
    Length of the report period in months: first to last entry day, both
    inclusive, across *all* the given trades. Every strategy is divided by this
    same period, so a strategy that only traded for a week is not extrapolated
    as if that week were its whole history. Clamped to at least one month so a
    short window is not raised to a large power.
    """
    days = [t.entry_time.date() for t in trades]
    if not days:
        return 1.0
    n_days = (max(days) - min(days)).days + 1
    return max(1.0, n_days / DAYS_PER_MONTH)


def monthly_return(total: Optional[float], months: float) -> Optional[float]:
    """
    Geometric mean return per month — the constant monthly return that would
    compound to `total` over `months`.
    """
    if total is None:
        return None
    growth = 1.0 + total
    if growth <= 0:
        return -1.0            # account fully wiped out — no real root exists
    return growth ** (1.0 / months) - 1.0


def sortino_days(trades) -> Optional[tuple]:
    """
    (first, last) calendar day of the period Sortino ratios are measured over:
    first entry day to the last entry *or exit* day across all the given trades.
    Exits count here, unlike in period_months(), because the daily returns are
    booked on the exit day, and one landing past the last entry must still fall
    inside the period. Shared by every strategy, like period_months().
    """
    days = [t.entry_time.date() for t in trades]
    days += [t.exit_time.date() for t in trades if t.exit_time]
    return (min(days), max(days)) if days else None


def sortino_ratio(trades, span: Optional[tuple],
                  days_per_year: float = TRADING_DAYS_PER_YEAR) -> Optional[float]:
    """
    Annualised Sortino ratio of a set of trades, with a 0% target return.

    Trades are turned into a daily return series first: each trade's P/L is
    booked on its exit day, several exits on one day compound, and every other
    day of `span` (see sortino_days()) is a flat 0%. Counting those quiet days
    matters — they dilute the mean and the downside alike, so a strategy that
    trades once a week is not scored as if it traded every day.

        mean     = sum(daily returns) / N
        downside = sqrt(sum(min(r, 0)^2) / N)
        sortino  = mean / downside * sqrt(days_per_year)

    Returns None when no trade has a P/L, or when no day lost money: with zero
    downside the ratio is unbounded, and a number would be meaningless.
    """
    daily = {}
    for t in trades:
        f = t.pnl_fraction
        if f is None or t.exit_time is None:
            continue
        d = t.exit_time.date()
        daily[d] = daily.get(d, 1.0) * (1.0 + f)
    if not daily or span is None:
        return None
    first, last = min(span[0], min(daily)), max(span[1], max(daily))
    n = (last - first).days + 1
    rets = [g - 1.0 for g in daily.values()]
    downside = math.sqrt(sum(r * r for r in rets if r < 0) / n)
    if downside <= 0:
        return None
    return (sum(rets) / n) / downside * math.sqrt(days_per_year)


@dataclass
class StrategyStats:
    name:   str
    trades: list = field(default_factory=list)
    # Market and timeframe the strategy trades, taken from its first entry
    # signal. Both stay None if that alert header carried neither.
    market:    Optional[str] = None
    timeframe: Optional[str] = None

    @property
    def market_label(self) -> str:
        """
        'BTC 15s' for the tables; empty when the log never said.

        The USDT quote currency is dropped as noise — everything here is quoted
        in it. USD-quoted contracts (BTCUSD) keep their suffix on purpose: that
        is a different instrument, not the same one abbreviated, and collapsing
        both to 'BTC' would hide the distinction.
        """
        m = self.market
        if m and m.endswith(QUOTE_SUFFIX) and len(m) > len(QUOTE_SUFFIX):
            m = m[: -len(QUOTE_SUFFIX)]
        return " ".join(x for x in (m, self.timeframe) if x)

    @property
    def total(self):
        return len(self.trades)

    @property
    def closed(self):
        return [t for t in self.trades if t.exit_type is not None]

    @property
    def open(self):
        return [t for t in self.trades if t.exit_type is None]

    @property
    def profitable_trades(self):
        return [t for t in self.closed if t.profitable]

    @property
    def losing_trades(self):
        return [t for t in self.closed if t.profitable is False]

    def exits_of_type(self, exit_type: str) -> list:
        return [t for t in self.closed if t.exit_type == exit_type]

    @staticmethod
    def _dur_stats(trades):
        durations = [t.duration_seconds for t in trades if t.duration_seconds is not None]
        if not durations:
            return None
        return {
            "min": min(durations),
            "max": max(durations),
            "avg": sum(durations) / len(durations),
            "n":   len(durations),
        }

    @property
    def duration_stats_profit(self):
        return self._dur_stats(self.profitable_trades)

    @property
    def duration_stats_loss(self):
        return self._dur_stats(self.losing_trades)

    # ── Account P/L ───────────────────────────────────────────────────────────

    @property
    def pnl_fractions(self) -> list:
        return [f for f in (t.pnl_fraction for t in self.closed) if f is not None]

    @property
    def total_return(self) -> Optional[float]:
        """Compounded account return over this strategy's closed trades."""
        return compound_return(self.closed)

    @property
    def avg_return(self) -> Optional[float]:
        """
        Geometric mean return per trade — the constant per-trade return that
        would compound to the same total. The arithmetic mean would overstate it.
        """
        fr = self.pnl_fractions
        if not fr:
            return None
        growth = 1.0 + self.total_return
        if growth <= 0:
            return -1.0            # account fully wiped out — no real root exists
        return growth ** (1.0 / len(fr)) - 1.0

    # ── Planned risk / reward ─────────────────────────────────────────────────
    # Averaged over *all* trades, open ones included: this describes how the
    # strategy is configured, not how its trades turned out.

    @property
    def risk_rewards(self) -> list:
        return [v for v in (t.risk_reward for t in self.trades) if v is not None]

    @property
    def avg_risk_reward(self) -> Optional[float]:
        rr = self.risk_rewards
        return sum(rr) / len(rr) if rr else None

    @property
    def median_risk_reward(self) -> Optional[float]:
        rr = sorted(self.risk_rewards)
        if not rr:
            return None
        mid = len(rr) // 2
        return rr[mid] if len(rr) % 2 else (rr[mid - 1] + rr[mid]) / 2.0

    @property
    def risk_reward_range(self) -> Optional[tuple]:
        rr = self.risk_rewards
        return (min(rr), max(rr)) if rr else None

    @staticmethod
    def _mean(values) -> Optional[float]:
        vals = [v for v in values if v is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def avg_reward_pct(self) -> Optional[float]:
        return self._mean(t.reward_pct for t in self.trades)

    @property
    def avg_risk_pct(self) -> Optional[float]:
        return self._mean(t.risk_pct for t in self.trades)

    @property
    def best_return(self) -> Optional[float]:
        fr = self.pnl_fractions
        return max(fr) if fr else None

    @property
    def worst_return(self) -> Optional[float]:
        fr = self.pnl_fractions
        return min(fr) if fr else None


# ── Log parsing ───────────────────────────────────────────────────────────────

def parse_timestamp(ts_str: str) -> datetime:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")


EMPTY_DATA = {"close": None, "side": None, "qty": None, "lev": None, "tp": None, "sl": None}


def split_params(text: Optional[str]) -> dict:
    """
    'side:1,Res=5m' -> {'side': '1', 'res': '5m'}. Keys are lowercased, since
    hand-typed Telegram commands vary in case; items without a separator are
    skipped. The first ':' or '=' splits, so a value like '1:0' stays whole.
    """
    out = {}
    for part in (text or "").split(","):
        m = PARAM_RE.match(part)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def parse_params(text: Optional[str]) -> dict:
    """
    Parse a 'side:1,q:3,l:50,...' parameter list into data-dict keys.

    Only fields that are present and parsable are returned, so the result can be
    merged over another dict without blanking values it does not mention. Used
    for both the data line's tail and the signal line's inline '(...)' block.
    """
    out = {}
    wanted = {FIELD_SIDE.lower(): "side", FIELD_QTY.lower(): "qty",
              FIELD_LEV.lower(): "lev", FIELD_TP.lower(): "tp", FIELD_SL.lower(): "sl"}
    for key, value in split_params(text).items():
        if key not in wanted:
            continue
        if wanted[key] == "side":
            side = SIDE_VALUES.get(value.strip().lower())
            if side is not None:
                out["side"] = side
            continue
        try:
            out[wanted[key]] = float(value)
        except ValueError:
            pass
    return out


def parse_data_line(raw: str) -> dict:
    """
    Parse the '2: #open,high,low,close,volume[,<side>:N,<q>:P,<l>:L,...]' line.

    Field names come from the FIELD_* constants in the configuration block at
    the top of this file. Returns a dict with keys:
      close — close price of the signal candle
      side  — 1 (long) or -1 (short); every SIDE_VALUES notation is accepted
      qty   — position size as a percentage of the account
      lev   — leverage multiplier
      tp    — take-profit price
      sl    — stop-loss price
    Any field that is missing or unparsable comes back as None.

    A dict rather than a tuple: these values are threaded through two more
    functions, and positional unpacking of six-plus optional numbers is an easy
    place to introduce a silent ordering bug.
    """
    m = DATA_LINE_RE.match(raw.strip())
    if not m:
        return dict(EMPTY_DATA)

    out = dict(EMPTY_DATA)
    try:
        out["close"] = float(m.group(4))
    except ValueError:
        pass

    out.update(parse_params(m.group(6)))
    return out


def collect_log_files(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(glob.glob(os.path.join(p, "profitview-*.log")))
        else:
            files.extend(glob.glob(p))
    return sorted(set(files), key=os.path.basename)


def split_market(raw: Optional[str]) -> Optional[str]:
    """
    'BYBIT:BTCUSDT' -> 'BTCUSDT'. The exchange prefix is dropped because it never
    varies within a report and only costs width in the tables. A market with no
    prefix is kept as-is; 'undefined' is treated as no market at all.
    """
    if not raw or raw == UNKNOWN_MARKET:
        return None
    return raw.split(":")[-1] or None


def telegram_market(params: dict) -> Optional[str]:
    """
    Market of a Telegram signal from its inline parameters: 'dogeusdt.p' ->
    'DOGEUSDT', uppercased to match alert headers. Falls back to
    TELEGRAM_DEFAULT_MARKET when the parameter is absent or empty.
    """
    market = split_market(params.get(TELEGRAM_FIELD_MARKET.lower(), "").strip())
    if not market:
        return TELEGRAM_DEFAULT_MARKET
    market = market.upper()
    if market.endswith(PERP_SUFFIX):
        market = market[: -len(PERP_SUFFIX)]
    return market or TELEGRAM_DEFAULT_MARKET


def find_mark_price(lines: list[str], start: int, market: Optional[str],
                    ts: Optional[datetime] = None,
                    window: int = TELEGRAM_MARK_PRICE_LINES,
                    max_age: Optional[float] = TELEGRAM_MARK_PRICE_SECONDS) -> Optional[float]:
    """
    First 'markPrice' for `market` in the `window` lines after `start`, or None.

    Used as the exit price of a manually sent exit, which has no candle of its
    own. Two things keep an unrelated price out:

      * the symbol. The log is full of mark prices for other instruments (whole
        ticker dumps), so a price only counts once a 'symbol' line has named the
        market we are after — which works for both layouts the exchange
        responses come in, since each prints the symbol before the mark price.
      * the time. Scanning stops at the first line stamped more than `max_age`
        seconds after `ts`, since by then the price has moved on. Pass ts=None
        or max_age=None to search the whole window regardless of age.
    """
    if not market or window <= 0:
        return None
    symbol = None
    for line in lines[start:start + window]:
        if ts is not None and max_age is not None:
            m = LINE_TS_RE.match(line)
            if m and (parse_timestamp(m.group(1)) - ts).total_seconds() > max_age:
                return None
        m = SYMBOL_RE.search(line)
        if m:
            symbol = (split_market(m.group(1)) or "").upper()
        if symbol != market:
            continue
        m = MARK_PRICE_RE.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def extract_events(files: list[str], show_progress: bool = True) -> list[tuple]:
    """
    Yield (timestamp, signal_token, data, market, timeframe) for every Received
    Alert block, where `data` is the dict returned by parse_data_line() (all-None
    if the alert had no '2: #...' line), overlaid with any inline parameters from
    the signal line ('1: NAME(side:1,q:3,...)'). `market` is the bare symbol, with the
    exchange stripped; both it and `timeframe` are None when the header did not
    carry them.

    A Telegram signal that only repeats an alert already in the list is dropped
    on the way out; see drop_duplicates().

    The progress bar is sized in bytes rather than lines so it can be set up
    before anything is read; character counts are trued up to the real file size
    at each file's end, so multi-byte text cannot make it drift.
    """
    events = []
    bar = Progress("Parsing", sum(os.path.getsize(f) for f in files if os.path.exists(f)),
                   enabled=show_progress)
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            bar.write(f"  [WARN] Cannot read {fpath}: {exc}")
            continue

        counted = pending = mark = 0
        i = 0
        while i < len(lines):
            line = lines[i].rstrip("\n")
            m = ALERT_HEADER_RE.match(line)
            if m:
                ts           = parse_timestamp(m.group(1))
                market       = split_market(m.group(2))
                timeframe    = m.group(3)
                signal_token = None
                inline       = None
                data         = dict(EMPTY_DATA)
                header_i     = i

                for j in range(i + 1, min(i + 15, len(lines))):
                    stripped = lines[j].strip()
                    if signal_token is None:
                        sig_m = SIGNAL_LINE_RE.match(stripped)
                        if sig_m:
                            signal_token, inline = sig_m.group(1), sig_m.group(2)
                    elif stripped.startswith("2:"):
                        data = parse_data_line(stripped)
                        i = j
                        break
                    elif stripped:
                        # Non-empty line that isn't the data line — stop looking
                        break

                if signal_token and m.group(4):
                    # Telegram also carries chat commands ('bal', 'Logvars', ...)
                    # that look like signal names. A real entry always comes with
                    # a data line; exits often don't, so only entries need one.
                    is_entry = classify_signal(signal_token)[1] == "ENTRY"
                    if is_entry and data["close"] is None:
                        signal_token = None
                    else:
                        params    = split_params(inline)
                        market    = telegram_market(params)
                        timeframe = params.get(TELEGRAM_FIELD_TIMEFRAME.lower(), "").strip() or None
                        # Manually sent exit, hence no candle: fall back to the
                        # mark price of the position query that follows it.
                        if not is_entry and data["close"] is None:
                            data["close"] = find_mark_price(lines, header_i + 1, market, ts)

                if signal_token:
                    # Inline '(...)' parameters win over the data line's, key by
                    # key; the close price only ever comes from the data line.
                    data.update(parse_params(inline))
                    events.append((ts, signal_token, data, market, timeframe,
                                   bool(m.group(4))))

            i += 1

            # Charge every line the scan has passed, including those skipped by
            # the `i = j` jump over an alert block, then report in ~256KB
            # batches so the redraw cost stays negligible.
            for k in range(mark, i):
                pending += len(lines[k])
            mark = i
            if pending >= 1 << 18:
                bar.advance(pending)
                counted += pending
                pending = 0

        # True up character count to actual bytes on disk.
        try:
            bar.advance(os.path.getsize(fpath) - counted)
        except OSError:
            bar.advance(pending)

    bar.close()
    events.sort(key=lambda e: e[0])
    return drop_duplicates(events)


def drop_duplicates(events: list[tuple],
                    telegram_window: float = TELEGRAM_DEDUPE_SECONDS,
                    repeat_window: float = DUPLICATE_ALERT_SECONDS) -> list[tuple]:
    """
    Drop signals that only repeat an earlier one, and strip the is-Telegram flag
    from the 6-tuples extract_events() collects.

    Taken at face value a repeat opens a second trade and closes the first as a
    FLIP at the same price, which shows up in the report as a zero-length trade
    at 0% P/L. Two kinds occur, and they need different tests:

      * A disabled alert still fires and is logged; re-sending it by hand through
        Telegram logs the same signal again, with the payload pasted in unchanged
        and only '(e=bybit)' added. Here the whole payload is compared — name,
        side, price, size, TP and SL — because the copy is identical and the gap
        can be an hour. Only the Telegram copy is dropped: the alert is what
        actually ran, and if the alert was disabled, the Telegram copy is the
        first of its payload and is kept.
      * An alert configured twice in TradingView fires twice for the same bar,
        within a second. Those copies are computed moments apart, so only name,
        side and price are compared — but within a window small enough that
        nothing but a repeat can fall inside it.
    """
    def payload(token: str, data: dict) -> tuple:
        return (token.upper(), data["side"], data["close"],
                data["qty"], data["tp"], data["sl"])

    keep_ms = max(telegram_window, repeat_window)
    kept, recent = [], []     # recent: (ts, payload) of signals still in window
    for ev in events:
        ts, token, data, _, _, telegram = ev
        recent = [r for r in recent if r[0] >= ts - timedelta(seconds=keep_ms)]
        this = payload(token, data)
        # A payload of all-Nones carries nothing to compare, so it never matches.
        if any(v is not None for v in this[1:]):
            dupe = False
            for old_ts, old in recent:
                age = (ts - old_ts).total_seconds()
                if telegram and telegram_window > 0 and age <= telegram_window \
                        and old == this:
                    dupe = True
                elif repeat_window > 0 and age <= repeat_window and old[:3] == this[:3]:
                    dupe = True
                if dupe:
                    break
            if dupe:
                continue
        recent.append((ts, this))
        kept.append(ev[:5])
    return kept


def is_ignored_signal(token: str) -> bool:
    """True if the signal token matches any IGNORED_SIGNALS glob (case-insensitive)."""
    upper = token.upper()
    return any(fnmatch.fnmatchcase(upper, pat.upper()) for pat in IGNORED_SIGNALS)


def classify_signal(token: str) -> tuple[str, str]:
    """Returns (root, signal_type). signal_type is ENTRY / TP1 / SPIKE / SL / TIMEOUT."""
    for suffix in EXIT_SUFFIXES:
        if token.upper().endswith(suffix):
            return token[: -len(suffix)], suffix.lstrip("_")
    return token, "ENTRY"


@dataclass
class FilterCounts:
    """How many trades each filter rule caught, and what was done with them."""
    removed_flip:    int = 0
    removed_maxtime: int = 0
    sl_flip:         int = 0
    sl_maxtime:      int = 0
    removed_maxpnl:  int = 0   # always removed, never closed as SL

    @property
    def total(self) -> int:
        return (self.removed_flip + self.removed_maxtime + self.removed_maxpnl
                + self.sl_flip + self.sl_maxtime)

    def describe(self) -> str:
        parts = []
        for reason, removed, sl in (("flip", self.removed_flip, self.sl_flip),
                                    ("max time", self.removed_maxtime, self.sl_maxtime)):
            if removed or sl:
                parts.append(f"{reason}: {removed} removed, {sl} closed as SL")
        if self.removed_maxpnl:
            parts.append(f"max P/L: {self.removed_maxpnl} removed")
        return "; ".join(parts)


def build_trades(events: list[tuple], allow_flips: bool = ALLOW_FLIPS,
                 max_trade_hours: Optional[float] = MAX_TRADE_HOURS,
                 max_trade_pnl_pct: Optional[float] = MAX_TRADE_PNL_PCT,
                 filter_action: str = FILTER_ACTION,
                 counts: Optional[FilterCounts] = None) -> dict[str, StrategyStats]:
    stats:      dict[str, StrategyStats] = {}
    open_trade: dict[str, Trade]         = {}
    if counts is None:
        counts = FilterCounts()
    max_age = timedelta(hours=max_trade_hours) if max_trade_hours else None
    max_pnl = max_trade_pnl_pct / 100.0 if max_trade_pnl_pct else None

    def filter_trade(trade: Trade, reason: str, close_time: datetime) -> None:
        """
        Apply the filter action to `trade`, which is already in stats[...].trades.
        An SL close needs both the stop level and a direction to mean anything, so
        a trade missing either falls back to removal.
        """
        if (filter_action == "sl" and trade.sl_price is not None
                and trade.direction is not None):
            trade.exit_time  = close_time
            trade.exit_type  = "SL"
            trade.exit_price = trade.sl_price
            setattr(counts, f"sl_{reason}", getattr(counts, f"sl_{reason}") + 1)
        else:
            stats[trade.strategy].trades.remove(trade)
            setattr(counts, f"removed_{reason}", getattr(counts, f"removed_{reason}") + 1)

    def over_age(trade: Trade, now: datetime) -> bool:
        return max_age is not None and now - trade.entry_time > max_age

    def close_trade(trade: Trade, ts: datetime, exit_type: str, price: float) -> None:
        """
        Close `trade` normally, then remove it if its account P/L is implausibly
        large in either direction. The check needs the exit price, so it runs
        after the close. Such a trade was never really placed, so it is removed
        regardless of filter_action.
        """
        trade.exit_time  = ts
        trade.exit_type  = exit_type
        trade.exit_price = price
        pnl = trade.pnl_fraction
        if max_pnl is not None and pnl is not None and abs(pnl) > max_pnl:
            stats[trade.strategy].trades.remove(trade)
            counts.removed_maxpnl += 1

    # Strategies whose market or timeframe changed after the first entry signal.
    # The first one wins (it is what the tables show), but a silent change would
    # misdescribe every later trade, so it is reported.
    drifted: dict[str, set] = {}

    for ts, token, data, market, timeframe in events:
        close_price = data["close"]
        if is_ignored_signal(token):
            continue
        root, sig_type = classify_signal(token)

        if root not in stats:
            stats[root] = StrategyStats(name=root)

        if sig_type == "ENTRY":
            # First entry signal that names a market or timeframe fixes it. A
            # signal lacking one (e.g. a Telegram command without 'res=') is
            # unknown there, not a change, so it neither warns nor overrides.
            s = stats[root]
            if ((market and s.market and market != s.market)
                    or (timeframe and s.timeframe and timeframe != s.timeframe)):
                drifted.setdefault(root, set()).add((market, timeframe))
            s.market    = s.market or market
            s.timeframe = s.timeframe or timeframe

            if root in open_trade:
                existing = open_trade.pop(root)
                # Age is checked first: a stale trade is filtered as such even
                # when flips are allowed.
                if over_age(existing, ts):
                    filter_trade(existing, "maxtime", existing.entry_time + max_age)
                elif not allow_flips:
                    filter_trade(existing, "flip", ts)
                else:
                    close_trade(existing, ts, "FLIP", close_price)
            # q/l are taken from the entry alert — they describe the buy-in that
            # this trade actually opened with, so a later exit alert's values
            # (which may reflect changed settings) must not override them.
            new_trade = Trade(
                strategy    = root,
                entry_time  = ts,
                entry_price = close_price,
                direction   = data["side"],
                qty_pct     = data["qty"],
                leverage    = data["lev"] if data["lev"] is not None else DEFAULT_LEVERAGE,
                tp_price    = data["tp"],
                sl_price    = data["sl"],
            )
            stats[root].trades.append(new_trade)
            open_trade[root] = new_trade

        else:  # TP1 / SPIKE / SL / TIMEOUT
            if root in open_trade:
                trade = open_trade.pop(root)
                if over_age(trade, ts):
                    filter_trade(trade, "maxtime", trade.entry_time + max_age)
                else:
                    close_trade(trade, ts, sig_type, close_price)

    # Trades still open at the end are measured against the last alert in the
    # data, the latest moment the log vouches for.
    if events:
        last_ts = events[-1][0]
        for trade in open_trade.values():
            if over_age(trade, last_ts):
                filter_trade(trade, "maxtime", trade.entry_time + max_age)

    # Full symbols here, not market_label: the point of the warning is which
    # contract it was, so BTCUSDT must not be abbreviated to BTC next to BTCUSD.
    for root, others in sorted(drifted.items()):
        s = stats[root]
        shown = " ".join(x for x in (s.market, s.timeframe) if x) or "(none)"
        rest  = ", ".join(sorted(" ".join(x for x in o if x) or "(none)" for o in others))
        print(f"  [WARN] {root} also signalled on {rest}; the tables show {shown}, "
              f"from its first entry signal.", file=sys.stderr)

    return stats


# ── Output formatting ─────────────────────────────────────────────────────────

def fmt_duration(seconds: Optional[float]) -> str:
    """
    Compact '42s' / '59m' / '2.1h' / '1.1d'. Hours and days are rounded *up* to
    one decimal, with a trailing '.0' dropped ('2h'); above a minute, seconds
    are ignored. fmt_duration_full() keeps the precision where there is room
    for it.
    """
    if seconds is None:
        return "—"
    mins = int(seconds // 60)
    if mins == 0:
        return f"{int(seconds)}s"
    if mins < 60:
        return f"{mins}m"
    # Ceil in integer tenths (6 min = 0.1h, 144 min = 0.1d) to dodge float error.
    per, unit = (144, "d") if mins >= 1440 else (6, "h")
    t = -(-mins // per)
    return f"{t // 10}{unit}" if t % 10 == 0 else f"{t // 10}.{t % 10}{unit}"


def fmt_duration_full(seconds: Optional[float]) -> str:
    """Long form with seconds — used for HTML tooltips."""
    if seconds is None:
        return "—"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if d:
        return f"{d}d {h:02d}h {m:02d}m {s:04.1f}s"
    if h:
        return f"{h}h {m:02d}m {s:04.1f}s"
    if m:
        return f"{m}m {s:04.1f}s"
    return f"{s:.1f}s"


def fmt_price(p: Optional[float]) -> str:
    return f"{p:,.2f}" if p is not None else "—"


def fmt_pct(frac: Optional[float], decimals: int = 2) -> str:
    """Format a fraction (0.045) as a signed percentage ('+4.50%')."""
    return f"{frac * 100:+.{decimals}f}%" if frac is not None else "—"


def _win_rate(s: StrategyStats) -> Optional[float]:
    nc = len(s.closed)
    return len(s.profitable_trades) / nc if nc else None


# Sort key and header label for each CONSOLE_SORT_COLUMNS entry. A metric
# returning None means "no value", which sorts last in either direction. Metrics
# also take the report period in months, for the per-month figure, and the
# day span Sortino ratios are measured over.
CONSOLE_SORT_METRICS = {
    "name":    ("Strategy",    lambda s, months, span: s.name.upper()),
    "market":  ("Market",      lambda s, months, span: s.market_label.upper() or None),
    "entries": ("Entries",     lambda s, months, span: s.total),
    "profit":  ("Profit",      lambda s, months, span: len(s.profitable_trades)),
    "loss":    ("Loss",        lambda s, months, span: len(s.losing_trades)),
    "spike":   ("Spike",       lambda s, months, span: len(s.exits_of_type("SPIKE"))),
    "timeout": ("Timeout",     lambda s, months, span: len(s.exits_of_type("TIMEOUT"))),
    "winrate": ("Win%",        lambda s, months, span: _win_rate(s)),
    "rr":      ("R:R",         lambda s, months, span: s.avg_risk_reward),
    "pnl":     ("Account P/L", lambda s, months, span: s.total_return),
    "avgPnl":  ("Avg/trade",   lambda s, months, span: s.avg_return),
    "avgMonth": ("Avg/month",  lambda s, months, span: monthly_return(s.total_return, months)),
    "sortino": ("Sortino",     lambda s, months, span: sortino_ratio(s.closed, span)),
}


def print_report(all_stats: dict[str, StrategyStats], verbose: bool = False,
                 sort_key: Optional[str] = None, sort_desc: bool = CONSOLE_DEFAULT_SORT_DESC):
    sort_key = sort_key or CONSOLE_DEFAULT_SORT
    if sort_key not in CONSOLE_SORT_METRICS:
        print(f"  [WARN] console sort column {sort_key!r} is not a known column; "
              f"falling back to 'pnl'. Valid: {', '.join(CONSOLE_SORT_COLUMNS)}", file=sys.stderr)
        sort_key = "pnl"
    sort_label, metric_fn = CONSOLE_SORT_METRICS[sort_key]

    # One shared period for every strategy — see period_months().
    all_trades = [t for s in all_stats.values() for t in s.trades]
    months = period_months(all_trades)
    span   = sortino_days(all_trades)
    metric = lambda s: metric_fn(s, months, span)

    # Name order first so ties stay deterministic (sorted() is stable), then the
    # chosen column. Missing values are split off rather than given a sentinel,
    # so they land at the bottom whichever direction is picked.
    by_name  = sorted(all_stats.values(), key=lambda s: s.name.upper())
    present  = [s for s in by_name if metric(s) is not None]
    missing  = [s for s in by_name if metric(s) is None]
    strategies = sorted(present, key=metric, reverse=sort_desc) + missing

    # ── Summary table ─────────────────────────────────────────────────────────
    # Columns: Strategy | Market | Entries | Profit | Loss | Spike | Timeout | Win% |
    #          R:R (planned) | Account P/L (compounded) | Avg/trade (geometric) |
    #          Avg/month (geometric, over the shared report period) |
    #          Sortino (annualised, daily returns over the shared report period)
    # Entries carries any still-open trades as a parenthesised count, e.g. "27 (1)",
    # which keeps a log discrepancy visible without spending a column on it.
    C = [14, 12, 10, 7, 6, 6, 8, 7, 6, 12, 11, 11, 8]

    def summary_row(name, market, entries, profit, loss, spike, timeout, win_pct, rr, pnl, avg_pnl,
                    avg_month, sortino):
        return (
            f"{str(name):<{C[0]}} "
            f"{str(market):<{C[1]}} "
            f"{str(entries):>{C[2]}} "
            f"{str(profit):>{C[3]}} "
            f"{str(loss):>{C[4]}} "
            f"{str(spike):>{C[5]}} "
            f"{str(timeout):>{C[6]}} "
            f"{str(win_pct):>{C[7]}} "
            f"{str(rr):>{C[8]}} "
            f"{str(pnl):>{C[9]}} "
            f"{str(avg_pnl):>{C[10]}} "
            f"{str(avg_month):>{C[11]}} "
            f"{str(sortino):>{C[12]}}"
        )

    def fmt_rr(v):
        return f"{v:.2f}" if v is not None else "—"

    fmt_sortino = fmt_rr

    def entries_cell(total, n_open):
        return f"{total} ({n_open})" if n_open else str(total)

    header = summary_row(
        "Strategy", "Market", "Entries", "Profit", "Loss", "Spike", "Timeout", "Win%",
        "R:R", "Account P/L", "Avg/trade", "Avg/month", "Sortino",
    )
    sep = "-" * len(header)

    print()
    print("=" * len(header))
    print(f"  STRATEGY SUMMARY  (sorted by {sort_label}, "
          f"{'descending' if sort_desc else 'ascending'})")
    print("=" * len(header))
    print(header)
    print(sep)

    totals = dict(entries=0, closed=0, profit=0, loss=0, spike=0, timeout=0, open=0)

    for s in strategies:
        nc      = len(s.closed)
        np_     = len(s.profitable_trades)
        nl      = len(s.losing_trades)
        nspike  = len(s.exits_of_type("SPIKE"))
        ntout   = len(s.exits_of_type("TIMEOUT"))
        nopen   = len(s.open)
        win_pct = f"{np_ / nc * 100:.1f}%" if nc else "—"

        print(summary_row(s.name, s.market_label, entries_cell(s.total, nopen), np_, nl, nspike, ntout,
                          win_pct, fmt_rr(s.avg_risk_reward),
                          fmt_pct(s.total_return), fmt_pct(s.avg_return),
                          fmt_pct(monthly_return(s.total_return, months)),
                          fmt_sortino(sortino_ratio(s.closed, span))))

        totals["entries"] += s.total
        totals["closed"]  += nc
        totals["profit"]  += np_
        totals["loss"]    += nl
        totals["spike"]   += nspike
        totals["timeout"] += ntout
        totals["open"]    += nopen

    print(sep)
    tc        = totals["closed"]
    total_win = f"{totals['profit'] / tc * 100:.1f}%" if tc else "—"

    # Compound over every closed trade across all strategies. Summing (or
    # averaging) the per-strategy figures above would be wrong — returns
    # multiply, so the total is prod(1 + r) over the individual trades.
    all_closed  = [t for s in strategies for t in s.closed]
    total_pnl   = compound_return(all_closed)
    n_priced    = len([t for t in all_closed if t.pnl_fraction is not None])
    total_avg   = None
    if n_priced and total_pnl is not None:
        growth    = 1.0 + total_pnl
        total_avg = growth ** (1.0 / n_priced) - 1.0 if growth > 0 else -1.0

    # Trade-weighted mean, matching how each strategy's own R:R is averaged.
    all_rr   = [v for s in strategies for v in s.risk_rewards]
    total_rr = sum(all_rr) / len(all_rr) if all_rr else None

    print(summary_row(
        "TOTAL", "",
        entries_cell(totals["entries"], totals["open"]),
        totals["profit"], totals["loss"], totals["spike"], totals["timeout"],
        total_win, fmt_rr(total_rr), fmt_pct(total_pnl), fmt_pct(total_avg),
        fmt_pct(monthly_return(total_pnl, months)),
        fmt_sortino(sortino_ratio(all_closed, span)),
    ))
    print("=" * len(header))
    print("  Account P/L is compounded — prod(1 + trade return) - 1 — not a sum of "
          "per-trade percentages.")
    print(f"  Avg/month is the geometric monthly return over the report period "
          f"({months:.2f} month(s), first to last entry day; minimum 1).")
    if span:
        print(f"  Sortino is annualised (x sqrt({TRADING_DAYS_PER_YEAR})) from daily returns "
              f"booked on exit days, target 0%, over {(span[1] - span[0]).days + 1} day(s); "
              f"'—' = no losing day.")
    n_missing = len([t for t in all_closed if t.pnl_fraction is None])
    if n_missing:
        print(f"  [note] {n_missing} of {len(all_closed)} closed trade(s) lack q:/l: or "
              f"price data and are excluded from P/L.")

    # ── Duration statistics table ─────────────────────────────────────────────
    # Columns: Strategy | Market | Outcome | N | Min | Avg | Max
    DW = [14, 12, 11, 5, 8, 8, 8]
    dur_header = (
        f"{'Strategy':<{DW[0]}}  "
        f"{'Market':<{DW[1]}}  "
        f"{'Outcome':<{DW[2]}}  "
        f"{'N':>{DW[3]}}  "
        f"{'Min':>{DW[4]}}  "
        f"{'Avg':>{DW[5]}}  "
        f"{'Max':>{DW[6]}}"
    )
    dur_sep = "-" * len(dur_header)

    print()
    print("=" * len(dur_header))
    print("  TRADE DURATION STATISTICS")
    print("=" * len(dur_header))
    print(dur_header)
    print(dur_sep)

    any_dur = False
    for idx, s in enumerate(strategies):
        for label, ds in [("Profitable", s.duration_stats_profit), ("Losing", s.duration_stats_loss)]:
            # Name and market are printed once per strategy, on its first row.
            name_col = s.name if label == "Profitable" else ""
            mkt_col  = s.market_label if label == "Profitable" else ""
            if ds:
                print(
                    f"{name_col:<{DW[0]}}  "
                    f"{mkt_col:<{DW[1]}}  "
                    f"{label:<{DW[2]}}  "
                    f"{ds['n']:>{DW[3]}}  "
                    f"{fmt_duration(ds['min']):>{DW[4]}}  "
                    f"{fmt_duration(ds['avg']):>{DW[5]}}  "
                    f"{fmt_duration(ds['max']):>{DW[6]}}"
                )
                any_dur = True
            else:
                print(
                    f"{name_col:<{DW[0]}}  "
                    f"{mkt_col:<{DW[1]}}  "
                    f"{label:<{DW[2]}}  "
                    f"{'0':>{DW[3]}}  "
                    f"{'—':>{DW[4]}}  "
                    f"{'—':>{DW[5]}}  "
                    f"{'—':>{DW[6]}}"
                )
        if idx < len(strategies) - 1:
            print(dur_sep)

    if not any_dur:
        print("  (no closed trades with duration data)")
    print("=" * len(dur_header))

    # ── Verbose: per-trade detail ─────────────────────────────────────────────
    if verbose:
        VW = [5, 19, 10, 5, 9, 10, 19, 10, 9, 12, 9, 9]
        v_header = (
            f"{'#':>{VW[0]}}  "
            f"{'Entry time':<{VW[1]}}  "
            f"{'Entry $':>{VW[2]}}  "
            f"{'Dir':<{VW[3]}}  "
            f"{'Exit':<{VW[4]}}  "
            f"{'P/L':<{VW[5]}}  "
            f"{'Exit time':<{VW[6]}}  "
            f"{'Exit $':>{VW[7]}}  "
            f"{'Duration':>{VW[8]}}  "
            f"{'Size':>{VW[9]}}  "
            f"{'Move%':>{VW[10]}}  "
            f"{'Acct P/L':>{VW[11]}}"
        )
        v_sep = "-" * len(v_header)

        print()
        print("=" * len(v_header))
        print("  TRADE DETAIL")
        print("=" * len(v_header))

        for s in strategies:
            if not s.trades:
                continue
            print(f"\n  Strategy: {s.name}"
                  + (f"  ({s.market_label})" if s.market_label else ""))
            print(v_header)
            print(v_sep)
            for i, t in enumerate(s.trades, 1):
                dir_str  = "Long" if t.direction == 1 else ("Short" if t.direction == -1 else "—")
                etype    = t.exit_type or "OPEN"
                prof_str = ("Profit" if t.profitable else "Loss") if t.profitable is not None else "—"
                exit_ts  = t.exit_time.strftime("%Y-%m-%d %H:%M:%S") if t.exit_time else "(open)"
                size_str = (f"{t.qty_pct:.2f}%x{t.leverage:g}"
                            if t.qty_pct is not None and t.leverage is not None else "—")
                move_str = (f"{t.price_change_pct:+.2f}%"
                            if t.price_change_pct is not None else "—")
                print(
                    f"{i:>{VW[0]}}  "
                    f"{t.entry_time.strftime('%Y-%m-%d %H:%M:%S'):<{VW[1]}}  "
                    f"{fmt_price(t.entry_price):>{VW[2]}}  "
                    f"{dir_str:<{VW[3]}}  "
                    f"{etype:<{VW[4]}}  "
                    f"{prof_str:<{VW[5]}}  "
                    f"{exit_ts:<{VW[6]}}  "
                    f"{fmt_price(t.exit_price):>{VW[7]}}  "
                    f"{fmt_duration(t.duration_seconds):>{VW[8]}}  "
                    f"{size_str:>{VW[9]}}  "
                    f"{move_str:>{VW[10]}}  "
                    f"{fmt_pct(t.pnl_fraction):>{VW[11]}}"
                )
            print(v_sep)
            print(f"{'':>{VW[0]}}  {'Compounded account P/L':<{VW[1]}}  "
                  f"{fmt_pct(s.total_return)}")
        print()


# ── HTML report ───────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ProfitView Strategy Report</title>
<style>
  :root {
    --bg:        #f5f6f8;
    --panel:     #ffffff;
    --panel-2:   #fafbfc;
    --border:    #e3e6ea;
    --border-2:  #d0d5db;
    --text:      #14181d;
    --muted:     #6b7480;
    --accent:    #2f6feb;
    --green:     #1a7f37;
    --green-bg:  #1a7f371f;
    --red:       #cf222e;
    --red-bg:    #cf222e1f;
    --amber:     #9a6700;
    --track:     #e7eaee;
    --shadow:    0 1px 2px rgba(16,22,30,.06), 0 8px 24px rgba(16,22,30,.05);
    color-scheme: light;
  }
  /* Dark palette is defined twice on purpose: the media query paints the correct
     theme before the script runs (no flash of light), and the [data-theme] rule
     lets the toggle override the OS preference in both directions. */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:        #101210;
      --panel:     #1a1c1a;
      --panel-2:   #202220;
      --border:    #2b2d2b;
      --border-2:  #383c38;
      --text:      #e9efe9;
      --muted:     #909690;
      --accent:    #4c8dff;
      --green:     #3fb950;
      --green-bg:  #3fb95024;
      --red:       #f85149;
      --red-bg:    #f8514924;
      --amber:     #d29922;
      --track:     #262826;
      --shadow:    0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28);
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg:        #101210;
    --panel:     #1a1c1a;
    --panel-2:   #202220;
    --border:    #2b2d2b;
    --border-2:  #383c38;
    --text:      #e9efe9;
    --muted:     #909690;
    --accent:    #4c8dff;
    --green:     #3fb950;
    --green-bg:  #3fb95024;
    --red:       #f85149;
    --red-bg:    #f8514924;
    --amber:     #d29922;
    --track:     #262826;
    --shadow:    0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28);
    color-scheme: dark;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 28px 20px 64px;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1360px; margin: 0 auto; }

  /* ── Header ───────────────────────────────────────────── */
  header { display: flex; align-items: flex-start; gap: 16px; flex-wrap: wrap; margin-bottom: 22px; }
  h1 { font-size: 21px; font-weight: 650; letter-spacing: -.02em; margin: 0 0 4px;
       display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }

  /* Two logo files, one per theme, swapped in CSS rather than JS so the correct
     one is painted on first render. Mirrors the token rules exactly: bare :root
     is light, the media query covers "system dark" before the script sets
     data-theme, and the [data-theme] rules let the toggle win either way.
     Intrinsic width/height attributes on the <img> reserve the space so the
     heading does not reflow when the artwork loads (or fails to). */
  .logo { height: __LOGO_H__px; width: auto; display: block; }
  .logo-dark { display: none; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) .logo-light { display: none; }
    :root:not([data-theme="light"]) .logo-dark  { display: block; }
  }
  :root[data-theme="dark"] .logo-light { display: none; }
  :root[data-theme="dark"] .logo-dark  { display: block; }
  .sub { color: var(--muted); font-size: 12.5px; }
  .spacer { flex: 1 1 auto; }

  button, input, select { font: inherit; color: inherit; }
  .btn {
    background: var(--panel); border: 1px solid var(--border-2); color: var(--text);
    border-radius: 8px; padding: 7px 12px; cursor: pointer; font-size: 13px;
    display: inline-flex; align-items: center; gap: 7px; transition: background .12s, border-color .12s;
  }
  .btn:hover { background: var(--panel-2); border-color: var(--accent); }

  /* ── KPI tiles ────────────────────────────────────────── */
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .kpi {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 13px 15px; box-shadow: var(--shadow);
  }
  .kpi .label { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); }
  .kpi .value { font-size: 23px; font-weight: 620; letter-spacing: -.02em; margin-top: 5px;
                font-variant-numeric: tabular-nums; }
  .kpi .value.pos { color: var(--green); }
  .kpi .value.neg { color: var(--red); }

  /* ── Filter bar ───────────────────────────────────────── */
  .filters {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; box-shadow: var(--shadow); margin-bottom: 18px;
    display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-end;
  }
  .field { display: flex; flex-direction: column; gap: 5px; }
  .field label { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); }
  .field input[type="text"], .field input[type="date"] {
    background: var(--panel-2); border: 1px solid var(--border-2); border-radius: 8px;
    padding: 7px 10px; min-width: 140px; outline: none;
  }
  .field input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
  #q { min-width: 230px; padding-right: 28px; }   /* room for the clear button */

  /* Clear button, laid over the right edge of the name box. */
  .inwrap { position: relative; display: inline-flex; }
  .clearx { position: absolute; right: 5px; top: 50%; transform: translateY(-50%);
            width: 19px; height: 19px; padding: 0; line-height: 17px; font-size: 15px;
            border: 0; border-radius: 50%; cursor: pointer; background: transparent;
            color: var(--muted); }
  .clearx:hover { background: var(--border); color: var(--text); }
  .clearx:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

  /* ── Date range slider ────────────────────────────────────
     Two <input type="range"> on one track. Each input is transparent and
     pointer-transparent; only its thumb accepts the pointer, so whichever thumb
     is underneath can still be grabbed. Which one sits on top is decided on
     hover (see raiseNearest) — without that, two thumbs at the same spot would
     leave one permanently unreachable. */
  /* flex-basis 100% claims a row of its own in the wrapping filter bar, so the
     slider can sit hard against the right edge with the count text running
     along the left of it instead of being pushed underneath. */
  .filterfoot { flex: 1 1 100%; display: flex; flex-wrap: wrap; gap: 8px 18px;
                align-items: center; justify-content: space-between; }
  .filterfoot .count { padding-bottom: 0; }

  /* The [hidden] attribute has to be restated: the UA rule that implements it is
     weaker than this class's own display, so it would otherwise be ignored. */
  .rangefield { display: flex; flex-direction: column; gap: 5px;
                align-items: flex-end; text-align: right; }
  .rangefield[hidden] { display: none; }
  .rangefield > label { font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
                        color: var(--muted); }
  .rangewrap { position: relative; width: 270px; max-width: 100%; height: 20px; }
  .rangetrack { position: absolute; left: 0; right: 0; top: 50%; height: 4px;
                margin-top: -2px; border-radius: 3px; background: var(--track); }
  .rangefill { position: absolute; top: 0; bottom: 0; border-radius: 3px;
               background: var(--accent); opacity: .55; }
  .rangewrap input[type="range"] {
    position: absolute; left: 0; top: 0; width: 100%; height: 20px; margin: 0;
    background: none; pointer-events: none; -webkit-appearance: none; appearance: none;
  }
  .rangewrap input[type="range"]:focus { outline: none; }
  .rangewrap input[type="range"]::-webkit-slider-thumb {
    pointer-events: auto; -webkit-appearance: none; appearance: none;
    width: 14px; height: 14px; border-radius: 50%; cursor: grab;
    background: var(--panel); border: 2px solid var(--accent); box-shadow: var(--shadow);
  }
  .rangewrap input[type="range"]::-moz-range-thumb {
    pointer-events: auto; width: 14px; height: 14px; border-radius: 50%; cursor: grab;
    background: var(--panel); border: 2px solid var(--accent); box-shadow: var(--shadow);
  }
  .rangewrap input[type="range"]:focus-visible::-webkit-slider-thumb {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 30%, transparent); }
  .rangewrap input[type="range"]:focus-visible::-moz-range-thumb {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 30%, transparent); }
  .rangewrap input[type="range"]::-webkit-slider-runnable-track { height: 20px; background: none; }
  .rangewrap input[type="range"]::-moz-range-track { background: none; }
  /* Quick-range dropdown: margin-left:auto pushes it to the right end of the
     first filter row, directly above the slider. */
  .presetfield { margin-left: auto; align-items: flex-end; }
  .presetfield select {
    background: var(--panel-2); border: 1px solid var(--border-2); border-radius: 8px;
    padding: 7px 10px; min-width: 150px; outline: none; cursor: pointer;
  }
  .presetfield select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
  .presetfield select option { background: var(--panel); color: var(--text); }
  .check { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12.5px; padding-bottom: 8px; cursor: pointer; }
  .count { color: var(--muted); font-size: 12.5px; padding-bottom: 8px; }
  .hint { color: var(--muted); text-transform: none; letter-spacing: 0; font-size: 10.5px; }
  .pill { align-self: center; margin-bottom: 8px; cursor: help; font-size: 11px;
          padding: 3px 9px; border-radius: 999px; color: var(--amber);
          border: 1px solid color-mix(in srgb, var(--amber) 45%, transparent);
          background: color-mix(in srgb, var(--amber) 12%, transparent); }

  /* ── Table ────────────────────────────────────────────── */
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
          box-shadow: var(--shadow); overflow: hidden; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { padding: 9px 9px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--border); }
  /* width:1% shrinks the strategy column to its longest entry instead of letting
     it soak up the table's spare width — with auto layout the widest column takes
     the largest share of the surplus, which left a gulf before Entries. */
  th:first-child, td:first-child { text-align: left; width: 1%; }
  thead th {
    position: sticky; top: 0; z-index: 2; background: var(--panel-2);
    font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
    font-weight: 600; cursor: pointer; user-select: none;
  }
  thead th:hover { color: var(--text); }
  thead th .arrow { opacity: .35; margin-left: 4px; font-size: 10px; }
  thead th.sorted { color: var(--accent); }
  thead th.sorted .arrow { opacity: 1; }
  tbody tr.row { cursor: pointer; }
  tbody tr.row:hover > td { background: var(--panel-2); }
  tbody tr.row.open > td { background: var(--panel-2); }
  .name { font-weight: 600; }
  .mkt { font-weight: 400; font-size: 10.5px; color: var(--muted); opacity: .75;
         letter-spacing: .02em; white-space: nowrap; margin-left: 6px; }
  .name .caret { display: inline-block; width: 11px; color: var(--muted); font-size: 10px; transition: transform .15s; }
  tr.row.open .caret { transform: rotate(90deg); }
  .dim { color: var(--muted); }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  tfoot td { font-weight: 650; background: var(--panel-2); border-bottom: none; border-top: 1px solid var(--border-2); }

  /* ── Win-rate cell ────────────────────────────────────── */
  .wr { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
  .wr .num { min-width: 42px; font-weight: 600; }
  .bar { width: 72px; height: 7px; border-radius: 999px; background: var(--track); overflow: hidden; flex: none; }
  .openmark { color: var(--amber); font-size: 12px; cursor: help; }
  .bar > span { display: block; height: 100%; border-radius: 999px; }

  /* ── Account P/L cell ─────────────────────────────────────
     Diverging bar: a centre line marks zero, gains grow right, losses left,
     so the sign is readable at a glance without reading the number. */
  .pl { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
  .pl .num { min-width: 62px; font-weight: 600; }
  .plbar { position: relative; width: 76px; height: 7px; background: var(--track);
           border-radius: 999px; flex: none; }
  .plbar::before { content: ""; position: absolute; left: 50%; top: -2px; width: 1px; height: 11px;
                   background: var(--border-2); }
  .plbar > span { position: absolute; top: 0; height: 100%; border-radius: 999px; }

  /* ── Duration cell ────────────────────────────────────── */
  .dur { display: flex; align-items: center; justify-content: flex-end; gap: 8px; cursor: help; }
  .durbar { position: relative; width: 104px; height: 9px; background: var(--track); border-radius: 999px; flex: none; }
  .durbar .range { position: absolute; top: 2px; height: 5px; border-radius: 999px;
                   background: color-mix(in srgb, var(--accent) 45%, transparent); }
  .durbar .avg { position: absolute; top: -1px; width: 3px; height: 11px; border-radius: 2px; background: var(--accent); }
  .durtext { font-size: 12px; color: var(--muted); min-width: 118px; text-align: right; }
  .durtext b { color: var(--text); font-weight: 600; }

  /* ── Detail panel ─────────────────────────────────────── */
  tr.detail > td { padding: 0; background: var(--panel-2); }
  /* white-space resets the nowrap inherited from the global cell rule, so the
     chart header and chips can wrap instead of forcing the table wider. */
  .detailbox { padding: 16px 18px 18px; border-bottom: 1px solid var(--border);
               white-space: normal; }
  /* ── Equity curve ─────────────────────────────────────────
     The SVG uses a fixed 800x150 viewBox stretched to the container with
     preserveAspectRatio="none"; every stroke carries vector-effect:
     non-scaling-stroke so the horizontal stretch cannot thicken or distort it.
     Axis labels are HTML around the chart, never SVG text, for the same reason. */
  .eqwrap { border: 1px solid var(--border); border-radius: 9px; padding: 11px 13px 8px;
            background: var(--panel); margin-bottom: 14px; }
  .eqhead { display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
            flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
  .eqhead b { color: var(--text); font-weight: 600; }
  .eq { display: block; width: 100%; height: 150px; overflow: visible; }
  .eq path, .eq line { vector-effect: non-scaling-stroke; }
  .eqaxis { display: flex; justify-content: space-between; font-size: 11px;
            color: var(--muted); margin-top: 5px; }

  /* ── Month boundaries ─────────────────────────────────────
     A hairline at each 1st of the month, with the month set faintly beside it.
     The lines are SVG (non-scaling-stroke keeps them 1px through the stretch);
     the labels are HTML, because SVG <text> would be squashed horizontally by
     that same stretch. pointer-events stays off so the labels cannot intercept
     the pointer that drives the hover marker. */
  .eq line.eqmonth { stroke: var(--border-2); stroke-width: 1; opacity: .5; }
  .eqmonths { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
  .eqmonths span { position: absolute; top: 1px; transform: translateX(4px);
                   font-size: 10px; letter-spacing: .05em; white-space: nowrap;
                   color: var(--muted); opacity: .5; }

  /* ── Hover marker ─────────────────────────────────────────
     The curve and the trade table highlight each other. Either one draws the
     same marker: a band over the trade's lifetime, a line at its exit and a dot
     on the step it caused. The overlay is HTML rather than SVG for the reason
     above — the plot is stretched horizontally, which would flatten a <circle>
     into an ellipse and thicken a vertical stroke. Percent-positioned boxes
     ignore that stretch. */
  .eqplot { position: relative; cursor: crosshair; }
  .eqmark { position: absolute; inset: 0; pointer-events: none; }
  .eqband { position: absolute; top: 0; bottom: 0; border-radius: 2px;
            background: color-mix(in srgb, var(--accent) 22%, transparent); }
  .eqvline { position: absolute; top: 0; bottom: 0; width: 1px;
             background: var(--accent); opacity: .75; }
  .eqdot { position: absolute; width: 9px; height: 9px; border-radius: 50%;
           transform: translate(-50%, -50%); background: var(--accent);
           border: 2px solid var(--panel); }
  .eqdot.pos { background: var(--green); }
  .eqdot.neg { background: var(--red); }
  /* Zero-size anchor + a tip that flips around it, so the vertical flip and the
     horizontal edge clamp stay independent of each other. */
  .eqtipwrap { position: absolute; width: 0; height: 0; }
  .eqtip { position: absolute; bottom: 13px; left: 0; transform: translateX(-50%);
           width: max-content; max-width: 260px; padding: 7px 10px 8px;
           background: var(--panel); border: 1px solid var(--border-2);
           border-radius: 8px; box-shadow: var(--shadow); font-size: 11.5px;
           line-height: 1.5; color: var(--text); }
  .eqtip.below { bottom: auto; top: 13px; }
  .eqtip.l { transform: none; }
  .eqtip.r { transform: translateX(-100%); }
  .tiph { display: block; font-weight: 600; margin-bottom: 4px;
          padding-bottom: 4px; border-bottom: 1px solid var(--border); }
  .tipg { display: grid; grid-template-columns: auto auto; gap: 1px 10px; }
  .tipg > span { color: var(--muted); }
  .tipg > b { font-weight: 600; font-variant-numeric: tabular-nums; }
  .tradewrap tr[data-mx0] { cursor: crosshair; }
  .tradewrap tr.hot > td { background: color-mix(in srgb, var(--accent) 12%, transparent); }

  .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  .chip { border: 1px solid var(--border-2); border-radius: 999px; padding: 3px 11px; font-size: 12px; color: var(--muted); }
  .chip b { color: var(--text); font-weight: 600; }
  .chip.g { border-color: color-mix(in srgb, var(--green) 45%, transparent); background: var(--green-bg); }
  .chip.r { border-color: color-mix(in srgb, var(--red) 45%, transparent); background: var(--red-bg); }
  .dtitle { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin: 0 0 8px; }
  table.mini { font-size: 12.5px; }
  .dirmark { font-size: 9px; vertical-align: 1px; opacity: .75; }
  .rr { cursor: help; border-bottom: 1px dotted var(--border-2); }
  table.mini th, table.mini td { padding: 5px 10px; border-bottom: 1px solid var(--border); }
  table.mini thead th { position: static; background: transparent; cursor: default; }
  .tradewrap { max-height: 340px; overflow: auto; border: 1px solid var(--border); border-radius: 9px; }
  .empty { padding: 40px; text-align: center; color: var(--muted); }
  footer { color: var(--muted); font-size: 12px; margin-top: 18px; }

  @media (max-width: 720px) {
    .bar { width: 56px; }
    .durbar, .plbar { display: none; }
    .durtext { min-width: 0; }
    .pl .num, .wr .num { min-width: 0; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1><!--__BRAND__--> Strategy Report</h1>
      <div class="sub" id="meta"></div>
    </div>
    <div class="spacer"></div>
    <button class="btn" id="theme" title="Toggle light / dark theme"><span id="themeIcon">◐</span> <span id="themeLabel">Theme</span></button>
  </header>

  <div class="kpis" id="kpis"></div>

  <div class="filters">
    <div class="field">
      <label for="q">Filter by name <span class="hint">— comma = any of, * ? = wildcards</span></label>
      <span class="inwrap">
        <input type="text" id="q" placeholder="e.g. xf4, exf*" autocomplete="off" spellcheck="false">
        <button type="button" class="clearx" id="qclear" hidden
                title="Clear the name filter (Esc)" aria-label="Clear the name filter">×</button>
      </span>
    </div>
    <div class="field">
      <label for="from">From</label>
      <input type="date" id="from">
    </div>
    <div class="field">
      <label for="to">Until</label>
      <input type="date" id="to">
    </div>
    <label class="check"><input type="checkbox" id="logscale"> Log-scale duration bars</label>
    <button class="btn" id="reset">Reset filters</button>
    <span class="pill" id="savedpill" hidden
          title="This view differs from the report's defaults and is remembered in this browser. Reset filters clears it.">saved</span>
    <div class="field presetfield">
      <label for="preset">Quick range</label>
      <select id="preset" title="Periods end on the last day in the data">
        <option value="">Custom</option>
        <option value="7d">Last 7 days</option>
        <option value="31d">Last 31 days</option>
        <option value="3m">Last 3 months</option>
        <option value="6m">Last 6 months</option>
        <option value="month">This month</option>
        <option value="quarter">This quarter</option>
        <option value="year">This year</option>
      </select>
    </div>
    <!-- Own full-width row: the count flows along the left, the slider stays
         pinned to the right edge of the bar. -->
    <div class="filterfoot">
      <div class="count" id="count"></div>
      <div class="rangefield" id="rangefield">
        <label for="rlo">Date range <span class="hint">— drag either end</span></label>
        <!-- Two range inputs stacked on one track: the browser gives each thumb
             its own drag and keyboard handling, which a single custom-drawn
             control would have to reimplement. Only the thumbs take the pointer
             (see .rangewrap CSS), so the one underneath stays grabbable. -->
        <div class="rangewrap" id="rangewrap">
          <span class="rangetrack"><span class="rangefill" id="rangefill"></span></span>
          <input type="range" id="rlo" min="0" max="0" step="1" value="0" aria-label="Range start">
          <input type="range" id="rhi" min="0" max="0" step="1" value="0" aria-label="Range end">
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="scroll">
      <table id="tbl">
        <thead><tr id="head"></tr></thead>
        <tbody id="body"></tbody>
        <tfoot id="foot"></tfoot>
      </table>
    </div>
    <div class="empty" id="empty" hidden>No strategies match the current filters.</div>
  </div>

  <footer id="foot-note"></footer>
</div>

<script>
"use strict";
const PAYLOAD = /*__DATA__*/ null;
const TRADES  = PAYLOAD.trades;
const MARKETS = PAYLOAD.markets || {};

/* The market and timeframe a strategy trades, e.g. "BTCUSDT 15s". Set faintly
   and small beside the name: it identifies the strategy but is constant for all
   of its rows, so it must not compete with the figures. Empty when the log never
   said which market the strategy was on. */
function marketTag(name) {
  const m = MARKETS[name];
  return m ? ` <span class="mkt">${esc(m)}</span>` : "";
}

/* ── Formatting helpers (mirrors fmt_duration / fmt_price in the Python side) ── */
/* Compact "42s" / "59m" / "2.1h" / "1.1d" — hours and days rounded *up* to one
   decimal with a trailing ".0" dropped ("2h"); above a minute, seconds are ignored.
   fmtDurFull() keeps the precision and is used for hover tooltips. */
function fmtDur(s) {
  if (s === null || s === undefined) return "—";
  const mins = Math.floor(s / 60);
  if (mins === 0) return Math.floor(s) + "s";
  if (mins < 60) return mins + "m";
  /* Ceil in integer tenths (6 min = 0.1h, 144 min = 0.1d) to dodge float error. */
  const [per, unit] = mins >= 1440 ? [144, "d"] : [6, "h"];
  const t = Math.ceil(mins / per), whole = Math.floor(t / 10);
  return (t % 10 ? whole + "." + (t % 10) : whole) + unit;
}
function fmtDurFull(s) {
  if (s === null || s === undefined) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60), sec = s % 60;
  const tail = String(m).padStart(2, "0") + "m " + sec.toFixed(1).padStart(4, "0") + "s";
  if (d) return d + "d " + String(h).padStart(2, "0") + "h " + tail;
  if (h) return h + "h " + tail;
  if (m) return m + "m " + sec.toFixed(1).padStart(4, "0") + "s";
  return sec.toFixed(1) + "s";
}
function fmtPrice(p) {
  return (p === null || p === undefined) ? "—" : p.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function pct(v) { return v === null ? "—" : (v * 100).toFixed(1) + "%"; }
function signed(frac, dp) {
  if (frac === null || frac === undefined) return "—";
  const v = frac * 100;
  return (v >= 0 ? "+" : "") + v.toFixed(dp === undefined ? 2 : dp) + "%";
}
function plClass(frac) { return frac === null || frac === undefined ? "dim" : frac >= 0 ? "pos" : "neg"; }

/* ── Compounding ──────────────────────────────────────────────────────────────
   Returns multiply, they do not add: +50% then -50% leaves 1.5*0.5 = 0.75, i.e.
   -25%. So any set of trades combines as prod(1 + r) - 1. This is the JS twin of
   compound_return() in the Python side and MUST be used everywhere trades are
   pooled — per strategy, in the TOTAL row, and in the KPI tiles. Summing the
   per-strategy figures would be wrong. Being a product, it is order-independent,
   so filtering can only change the result by changing membership.             */
function compoundReturn(trades) {
  let factor = 1, seen = false;
  for (const t of trades) {
    if (t.pnl !== null && t.pnl !== undefined) { factor *= (1 + t.pnl); seen = true; }
  }
  return seen ? factor - 1 : null;
}

/* Geometric mean per trade: the constant return that compounds to the same total. */
function geoMean(trades) {
  const n = trades.filter(t => t.pnl !== null && t.pnl !== undefined).length;
  if (!n) return null;
  const growth = 1 + compoundReturn(trades);
  return growth <= 0 ? -1 : Math.pow(growth, 1 / n) - 1;
}

/* Geometric mean per month over `months` — the shared view window, never a
   strategy's own span. JS twin of monthly_return() on the Python side. */
function monthlyReturn(total, months) {
  if (total === null || total === undefined) return null;
  const growth = 1 + total;
  return growth <= 0 ? -1 : Math.pow(growth, 1 / months) - 1;
}

/* Annualised Sortino ratio, target 0%. JS twin of sortino_ratio() on the Python
   side: each trade's P/L is booked on its exit day (same-day exits compound),
   every other day of `span` — ["YYYY-MM-DD", "YYYY-MM-DD"], see sortinoSpan() —
   counts as a flat 0%, and the daily ratio is scaled by sqrt(trading days).
   null when nothing has a P/L or no day lost money (unbounded ratio).        */
function sortino(trades, span) {
  const daily = new Map();
  for (const t of trades) {
    if (t.pnl === null || t.pnl === undefined || !t.t1) continue;
    const d = t.t1.slice(0, 10);
    daily.set(d, (daily.has(d) ? daily.get(d) : 1) * (1 + t.pnl));
  }
  if (!daily.size || !span) return null;
  const keys = [...daily.keys()].sort();
  const lo = span[0] < keys[0] ? span[0] : keys[0];
  const hi = span[1] > keys[keys.length - 1] ? span[1] : keys[keys.length - 1];
  const n = Math.round((Date.parse(hi + "T00:00:00Z") - Date.parse(lo + "T00:00:00Z")) / 86400000) + 1;
  const rets = [...daily.values()].map(g => g - 1);
  const downside = Math.sqrt(rets.reduce((a, r) => a + (r < 0 ? r * r : 0), 0) / n);
  if (!(downside > 0)) return null;
  return (rets.reduce((a, r) => a + r, 0) / n) / downside * Math.sqrt(PAYLOAD.config.tradingDays);
}

/* Day span the Sortino ratios are measured over: the view's From (or first entry
   day) to its To (or last entry day), stretched to the last exit in view — trades
   are filtered by entry day, so an exit can land after To. Shared by all rows. */
function sortinoSpan(trades) {
  const days = trades.map(t => t.day).sort();
  if (!days.length) return null;
  let hi = $("to").value || days[days.length - 1];
  for (const t of trades) if (t.t1 && t.t1.slice(0, 10) > hi) hi = t.t1.slice(0, 10);
  return [$("from").value || days[0], hi];
}

function fmtSortino(v) { return v === null || v === undefined ? "—" : v.toFixed(2); }

/* Length of a viewWindow() in fractional months, clamped to at least one. */
const DAYS_PER_MONTH = 365.25 / 12;
function windowMonths(win) {
  if (!win) return 1;
  return Math.max(1, (win[1] - win[0]) / 86400000 / DAYS_PER_MONTH);
}

/* Colour ramp for the win-rate bar: red → amber → green. */
function wrColor(r) {
  if (r === null) return "var(--track)";
  if (r < 0.4)  return "var(--red)";
  if (r < 0.55) return "var(--amber)";
  return "var(--green)";
}

/* ── Aggregation: the JS twin of StrategyStats ───────────────────────────────
   Recomputed on every filter change, because win-rate and duration stats all
   depend on which trades are currently in scope. Trades themselves are already
   entry/exit-paired by the Python parser and are never re-paired here.        */
function durStats(list) {
  const d = list.map(t => t.dur).filter(v => v !== null);
  if (!d.length) return null;
  return { n: d.length, min: Math.min(...d), max: Math.max(...d),
           avg: d.reduce((a, b) => a + b, 0) / d.length };
}

/* Planned risk/reward, as configured at entry. The mean is the headline figure
   (the config is usually near-static, so it is representative); median and the
   min–max range ride along in the tooltip so drift over time is still visible. */
function mean(values) {
  const v = values.filter(x => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}
function rrStats(list) {
  const rr = list.map(t => t.rr).filter(v => v !== null && v !== undefined).sort((a, b) => a - b);
  const mid = Math.floor(rr.length / 2);
  return {
    rr:       rr.length ? rr.reduce((a, b) => a + b, 0) / rr.length : null,
    rrMedian: rr.length ? (rr.length % 2 ? rr[mid] : (rr[mid - 1] + rr[mid]) / 2) : null,
    rrMin:    rr.length ? rr[0] : null,
    rrMax:    rr.length ? rr[rr.length - 1] : null,
    rrN:      rr.length,
    avgReward: mean(list.map(t => t.rw)),
    avgRisk:   mean(list.map(t => t.rk)),
  };
}

function aggregate(trades, months, span) {
  const by = new Map();
  for (const t of trades) {
    if (!by.has(t.s)) by.set(t.s, []);
    by.get(t.s).push(t);
  }
  const rows = [];
  for (const [name, list] of by) {
    const closed  = list.filter(t => t.x !== null);
    const profit  = closed.filter(t => t.w === true);
    const loss    = closed.filter(t => t.w === false);
    const countX  = k => closed.filter(t => t.x === k).length;
    rows.push({
      name, trades: list, entries: list.length, closed: closed.length,
      open: list.length - closed.length,
      profit: profit.length, loss: loss.length,
      tp1: countX("TP1"), spike: countX("SPIKE"), sl: countX("SL"),
      timeout: countX("TIMEOUT"), flip: countX("FLIP"),
      winrate: closed.length ? profit.length / closed.length : null,
      dur: durStats(closed), durProfit: durStats(profit), durLoss: durStats(loss),
      // R:R is averaged over ALL trades, open ones included — it describes the
      // configured setup, not the outcome.
      ...rrStats(list),
      pnl: compoundReturn(closed), avgPnl: geoMean(closed),
      avgMonth: monthlyReturn(compoundReturn(closed), months),
      sortino: sortino(closed, span),
      best: closed.reduce((a, t) => t.pnl !== null && (a === null || t.pnl > a) ? t.pnl : a, null),
      worst: closed.reduce((a, t) => t.pnl !== null && (a === null || t.pnl < a) ? t.pnl : a, null),
      noPnl: closed.filter(t => t.pnl === null).length,
    });
  }
  return rows;
}

/* ── Table definition ─────────────────────────────────────────────────────── */
/* Closed and Open are deliberately absent: closed is entries minus open, and any
   still-open trade is surfaced as a "(n)" suffix on Entries instead of costing a
   whole column. Min is likewise absent — it is the first value of the duration
   cell. This keeps the table inside the page width without horizontal scrolling. */
const COLS = [
  { key: "name",    label: "Strategy", sort: r => r.name.toLowerCase(), dir: 1  },
  { key: "entries", label: "Entries",  sort: r => r.entries,            dir: -1 },
  { key: "profit",  label: "Profit",   sort: r => r.profit,             dir: -1 },
  { key: "loss",    label: "Loss",     sort: r => r.loss,               dir: -1 },
  { key: "spike",   label: "Spike",    sort: r => r.spike,              dir: -1 },
  { key: "timeout", label: "T/O",      title: "Timeout", sort: r => r.timeout,            dir: -1 },
  { key: "winrate", label: "Win rate", sort: r => r.winrate === null ? -1 : r.winrate, dir: -1 },
  { key: "rr",      label: "R:R",      sort: r => r.rr === null ? -Infinity : r.rr, dir: -1 },
  { key: "pnl",     label: "Account P/L", sort: r => r.pnl === null ? -Infinity : r.pnl, dir: -1 },
  { key: "avgPnl",  label: "Avg/trade",   sort: r => r.avgPnl === null ? -Infinity : r.avgPnl, dir: -1 },
  { key: "avgMonth", label: "Avg/month",  sort: r => r.avgMonth === null ? -Infinity : r.avgMonth, dir: -1 },
  { key: "sortino", label: "Sortino",  sort: r => r.sortino === null ? -Infinity : r.sortino, dir: -1 },
  { key: "dur",     label: "Time in trade (min · avg · max)", sort: r => r.dur ? r.dur.avg : -1, dir: -1 },
];

/* Initial sort comes from the config block at the top of the Python file. */
let sortKey = COLS.some(c => c.key === PAYLOAD.config.sortKey) ? PAYLOAD.config.sortKey : "pnl";
let sortDir = PAYLOAD.config.sortDir;
const DEFAULT_SORT_KEY = sortKey, DEFAULT_SORT_DIR = sortDir;
const expanded = new Set();
let lastRowNames = [];

const $ = id => document.getElementById(id);

/* ── Filtering ────────────────────────────────────────────────────────────────
   Dates are compared as "YYYY-MM-DD" strings against the pre-computed `day`
   field, which sidesteps timezone drift entirely — no Date objects involved. */
/* The name box takes a comma-separated list and matches ANY term (OR). Each term
   matches the whole name, case-insensitively, with glob wildcards: * = any run
   of characters, ? = exactly one. So "xf4" is XF4 only, "*xf4" adds EXF4, and
   "exf*,xpx*" shows both families — the same syntax as --strat. Empty terms from
   stray commas are dropped, so a trailing comma while typing does not blank the
   table. */
function nameTerms() {
  return $("q").value.split(",").map(s => s.trim()).filter(Boolean).map(term =>
    new RegExp("^" + term.replace(/[.+^${}()|[\]\\]/g, "\\$&")
                         .replace(/\*/g, ".*").replace(/\?/g, ".") + "$", "i"));
}

function currentTrades() {
  const terms = nameTerms();
  const from = $("from").value, to = $("to").value;
  return TRADES.filter(t => {
    return (!terms.length || terms.some(re => re.test(t.s)))
        && (!from || t.day >= from)
        && (!to   || t.day <= to);
  });
}

function render() {
  const trades = currentTrades();
  const win = viewWindow(trades);
  const months = windowMonths(win);
  const span = sortinoSpan(trades);
  let rows = aggregate(trades, months, span);
  // A strategy with no closed trades in the window has nothing to rank, chart or
  // compound, so it is never listed. Not optional — every figure below, from the
  // KPI tiles to the footer totals, is computed over the rows that survive here.
  rows = rows.filter(r => r.closed > 0);

  const col = COLS.find(c => c.key === sortKey) || COLS[0];
  rows.sort((a, b) => {
    const x = col.sort(a), y = col.sort(b);
    const c = x < y ? -1 : x > y ? 1 : 0;
    return c * sortDir || a.name.localeCompare(b.name);
  });

  lastRowNames = rows.map(r => r.name);

  renderKpis(rows, trades, months);
  renderHead();
  renderBody(rows, win);
  renderFoot(rows, months, span);

  $("count").textContent = rows.length + " strateg" + (rows.length === 1 ? "y" : "ies") +
                           " · " + trades.length + " trade" + (trades.length === 1 ? "" : "s") +
                           " (" + TRADES.length + " total)" +
                           " · " + fmtSpan($("from").value || DAY_MIN, $("to").value || DAY_MAX) +
                           " (" + fmtSpan(DAY_MIN, DAY_MAX) + " total)";
  $("empty").hidden = rows.length > 0;
  $("tbl").hidden = rows.length === 0;

  // Both controls are driven from here, so they track the filter whatever
  // changed it — a drag, a typed date, Reset filters, or a restored view.
  $("qclear").hidden = !$("q").value;
  syncSliderFromDates();
  syncPresetFromDates();

  $("savedpill").hidden = !viewIsCustom();
  saveState();
}

function renderKpis(rows, trades, months) {
  const closed = trades.filter(t => t.x !== null);
  const wins   = closed.filter(t => t.w === true).length;
  const wr     = closed.length ? wins / closed.length : null;
  const ds     = durStats(closed);
  // Compound over the individual trades of every visible strategy — never over
  // the per-strategy returns, which would multiply already-multiplied figures.
  const visible = new Set(rows.map(r => r.name));
  const pool    = closed.filter(t => visible.has(t.s));
  const total   = compoundReturn(pool);
  const nOpen = trades.length - closed.length;
  const tiles = [
    ["Strategies",   rows.length,        ""],
    // Same "(n)" convention as the Entries column: open trades are annotated, not columned.
    ["Trades",       trades.length + (nOpen ? " (" + nOpen + ")" : ""), ""],
    ["Overall win rate", pct(wr), wr === null ? "" : (wr >= 0.5 ? "pos" : "neg")],
    ["Account P/L (compounded)", signed(total), plClass(total)],
    ["Avg per trade",  signed(geoMean(pool)), plClass(geoMean(pool))],
    ["Avg per month",  signed(monthlyReturn(total, months)), plClass(monthlyReturn(total, months))],
    ["Avg time in trade", ds ? fmtDur(ds.avg) : "—", ""],
  ];
  $("kpis").innerHTML = tiles.map(([l, v, cls]) =>
    `<div class="kpi"><div class="label">${esc(l)}</div><div class="value ${cls}">${esc(String(v))}</div></div>`
  ).join("");
}

function renderHead() {
  $("head").innerHTML = COLS.map(c => {
    const on = c.key === sortKey;
    const arrow = on ? (sortDir === 1 ? "▲" : "▼") : "↕";
    return `<th data-key="${c.key}" class="${on ? "sorted" : ""}"${c.title ? ` title="${esc(c.title)}"` : ""}>${esc(c.label)}<span class="arrow">${arrow}</span></th>`;
  }).join("");
  $("head").querySelectorAll("th").forEach(th => {
    th.onclick = () => {
      const key = th.dataset.key;
      if (key === sortKey) sortDir = -sortDir;
      else { sortKey = key; sortDir = COLS.find(c => c.key === key).dir; }
      render();
    };
  });
}

/* Duration bars share one global scale so strategies stay comparable. */
function durScale(rows) {
  const max = Math.max(1, ...rows.filter(r => r.dur).map(r => r.dur.max));
  const log = $("logscale").checked;
  const f = v => log ? Math.log1p(Math.max(0, v)) / Math.log1p(max) : v / max;
  return v => Math.max(0, Math.min(1, f(v))) * 100;
}

/* Diverging P/L bar, scaled to the largest absolute return currently on screen. */
function plBar(frac, maxAbs) {
  if (frac === null || frac === undefined) return `<span class="plbar"></span>`;
  const half = maxAbs ? Math.min(50, Math.abs(frac) / maxAbs * 50) : 0;
  const style = frac >= 0
    ? `left:50%;width:${half}%;background:var(--green)`
    : `left:${50 - half}%;width:${half}%;background:var(--red)`;
  return `<span class="plbar" title="${signed(frac)}"><span style="${style}"></span></span>`;
}

/* R:R cell — mean, with the spread behind a tooltip. A near-zero spread means
   the TP/SL config never moved; a wide one means it drifted over the period. */
function rrCell(r) {
  if (r.rr === null) return `<span class="dim">—</span>`;
  const spread = (r.rrMax - r.rrMin) < 0.005
    ? "constant across all trades"
    : `median ${r.rrMedian.toFixed(2)}, range ${r.rrMin.toFixed(2)}–${r.rrMax.toFixed(2)}`;
  const title = `Planned reward ÷ risk at entry, mean of ${r.rrN} trade(s)\n` +
                `${spread}\n` +
                `avg target ${r.avgReward === null ? "—" : r.avgReward.toFixed(2) + "%"}, ` +
                `avg stop ${r.avgRisk === null ? "—" : r.avgRisk.toFixed(2) + "%"}`;
  return `<span class="rr" title="${esc(title)}">${r.rr.toFixed(2)}</span>`;
}

function renderBody(rows, win) {
  const pos = durScale(rows);
  const maxAbsPnl = Math.max(0, ...rows.filter(r => r.pnl !== null).map(r => Math.abs(r.pnl)));
  const html = [];
  for (const r of rows) {
    const wrPctText = r.winrate === null ? "—" : (r.winrate * 100).toFixed(1) + "%";
    const wrWidth   = r.winrate === null ? 0 : r.winrate * 100;
    const d = r.dur;
    const durTitle = d ? `min ${fmtDurFull(d.min)} / avg ${fmtDurFull(d.avg)} / max ${fmtDurFull(d.max)}` : "";
    const durCell = d
      ? `<div class="dur" title="${esc(durTitle)}">
           <span class="durtext"><b>${esc(fmtDur(d.min))}</b> · <b>${esc(fmtDur(d.avg))}</b> · <b>${esc(fmtDur(d.max))}</b></span>
           <span class="durbar">
             <span class="range" style="left:${pos(d.min)}%;width:${Math.max(1.5, pos(d.max) - pos(d.min))}%"></span>
             <span class="avg" style="left:calc(${pos(d.avg)}% - 1.5px)"></span>
           </span>
         </div>`
      : `<span class="dim">—</span>`;

    html.push(`<tr class="row ${expanded.has(r.name) ? "open" : ""}" data-name="${esc(r.name)}">
      <td class="name"><span class="caret">▶</span> ${esc(r.name)}${marketTag(r.name)}</td>
      <td>${r.entries}${r.open ? ` <span class="openmark" title="${r.open} trade(s) still open">(${r.open})</span>` : ""}</td>
      <td class="${r.profit ? "pos" : "dim"}">${r.profit}</td>
      <td class="${r.loss ? "neg" : "dim"}">${r.loss}</td>
      <td class="${r.spike ? "" : "dim"}">${r.spike}</td>
      <td class="${r.timeout ? "" : "dim"}">${r.timeout}</td>
      <td><div class="wr"><span class="num">${wrPctText}</span>
          <span class="bar"><span style="width:${wrWidth}%;background:${wrColor(r.winrate)}"></span></span></div></td>
      <td>${rrCell(r)}</td>
      <td><div class="pl"><span class="num ${plClass(r.pnl)}">${signed(r.pnl)}</span>${plBar(r.pnl, maxAbsPnl)}</div></td>
      <td class="${plClass(r.avgPnl)}">${signed(r.avgPnl)}</td>
      <td class="${plClass(r.avgMonth)}">${signed(r.avgMonth)}</td>
      <td class="${plClass(r.sortino)}">${fmtSortino(r.sortino)}</td>
      <td>${durCell}</td>
    </tr>`);

    if (expanded.has(r.name)) {
      html.push(`<tr class="detail"><td colspan="${COLS.length}">${detailHtml(r, win)}</td></tr>`);
    }
  }
  $("body").innerHTML = html.join("");
  $("body").querySelectorAll("tr.row").forEach(tr => {
    tr.onclick = () => {
      const n = tr.dataset.name;
      expanded.has(n) ? expanded.delete(n) : expanded.add(n);
      render();
    };
  });
  bindTradeMarkers($("body"));
}

/* ── Trade ⇄ curve marker ─────────────────────────────────────────────────────
   The trade table and the equity curve highlight each other, both ways:
   hovering a row paints that trade onto the curve, and sweeping the curve
   highlights the trade under the pointer and scrolls it into view. Both
   directions funnel through showMark(), so the two can never disagree.

   Bound directly rather than delegated because mouseenter/mouseleave do not
   bubble; renderBody() rebinds after every render, which is also how the
   expand handlers above work.                                                 */
function bindTradeMarkers(root) {
  root.querySelectorAll(".detailbox").forEach(box => {
    box.querySelectorAll("tr[data-mx0]").forEach(tr => {
      tr.onmouseenter = () => showMark(box, tr);
      tr.onmouseleave = () => clearMark(box);
    });
    const plot = box.querySelector(".eqplot");
    if (!plot) return;
    plot.onmousemove = e => {
      const r = plot.getBoundingClientRect();
      if (!r.width) return;
      const tr = rowAtX(box, (e.clientX - r.left) / r.width * 100);
      if (tr) { showMark(box, tr); revealRow(box, tr); }
    };
    plot.onmouseleave = () => clearMark(box);
  });
}

/* The rows' data-mx* attributes are already an interval index over the
   timeline, so the reverse lookup reads them back instead of keeping a second
   copy of the geometry. Cached per detail box; the cache dies with the DOM on
   the next render, which is exactly when it would go stale. */
function rowSpans(box) {
  if (!box.__spans) {
    box.__spans = [...box.querySelectorAll("tr[data-mx0]")]
      .map(tr => ({ tr, x0: +tr.dataset.mx0, x1: +tr.dataset.mx1 }));
  }
  return box.__spans;
}

/* A strategy holds one position at a time, so trade spans do not overlap: the
   pointer is either inside a trade's lifetime (distance 0) or in the gap
   between two, where the nearer one wins. Rows are newest-first, so ties go to
   the more recent trade. */
function rowAtX(box, x) {
  let best = null, bestD = Infinity;
  for (const s of rowSpans(box)) {
    const d = x < s.x0 ? s.x0 - x : x > s.x1 ? x - s.x1 : 0;
    if (d < bestD) { bestD = d; best = s.tr; }
  }
  return best;
}

function showMark(box, tr) {
  const mark = box.querySelector(".eqmark");
  if (!mark || box.__hot === tr) return;      // mousemove repeats within one trade
  if (box.__hot) box.__hot.classList.remove("hot");
  const d = tr.dataset;
  const band = mark.querySelector(".eqband");
  band.style.left  = d.mx0 + "%";
  band.style.width = Math.max(0.3, +d.mx1 - +d.mx0) + "%";
  mark.querySelector(".eqvline").style.left = d.mvl + "%";
  const dot = mark.querySelector(".eqdot");
  dot.hidden = d.my === undefined;            // open, or no P/L data: no step to point at
  if (!dot.hidden) {
    dot.style.left = d.mvl + "%";
    dot.style.top  = d.my + "%";
    dot.className  = "eqdot " + (d.mc || "");
  }
  placeTip(mark, tr, d);
  mark.hidden = false;
  tr.classList.add("hot");
  box.__hot = tr;
}

/* The tooltip is built from the row's own cells rather than from a second copy
   of the trade, so it cannot drift out of step with the table — same values,
   same formatting, same colour classes. Indices name the columns built in
   detailHtml(); they move together. */
const TIP = { n: 0, t0: 1, dir: 2, p0: 3, x: 4, t1: 5, p1: 6, dur: 8, acct: 12 };

function placeTip(mark, tr, d) {
  const wrap = mark.querySelector(".eqtipwrap");
  const tip  = mark.querySelector(".eqtip");
  if (!tip) return;
  const c = i => (tr.cells && tr.cells[i]) ? tr.cells[i].innerHTML : "—";
  const acct = tr.cells ? tr.cells[TIP.acct] : null;
  const at = (when, price) => price === "—" ? when : when + " @ " + price;   // open trade: no price yet
  tip.innerHTML =
    `<span class="tiph">#${c(TIP.n)} ${c(TIP.dir)} · ${c(TIP.x)} · ${c(TIP.dur)}</span>` +
    `<span class="tipg">` +
      `<span>Entry</span><b>${at(c(TIP.t0), c(TIP.p0))}</b>` +
      `<span>Exit</span><b>${at(c(TIP.t1), c(TIP.p1))}</b>` +
      `<span>P/L</span><b class="${acct ? acct.className : ""}">${c(TIP.acct)}</b>` +
    `</span>`;

  // The wrapper is a zero-size anchor carrying the position; the tip flips
  // around it. Keeping the two jobs on separate elements means the vertical
  // flip (top/bottom) and the horizontal clamp (a transform) never have to be
  // expressed as one combined transform.
  const x = +d.mvl, y = d.my === undefined ? 50 : +d.my;
  wrap.style.left = x + "%";
  wrap.style.top  = y + "%";
  tip.className = "eqtip" + (y < 45 ? " below" : "") +
                  (x < 18 ? " l" : x > 82 ? " r" : "");
}

function clearMark(box) {
  const mark = box.querySelector(".eqmark");
  if (mark) mark.hidden = true;
  if (box.__hot) box.__hot.classList.remove("hot");
  box.__hot = null;
}

/* Scroll the trade list to the highlighted row by hand. scrollIntoView() would
   walk up and scroll the page as well, dragging the chart out from under the
   pointer that is driving the highlight. */
function revealRow(box, tr) {
  const wrap = box.querySelector(".tradewrap");
  if (!wrap) return;
  const w = wrap.getBoundingClientRect(), r = tr.getBoundingClientRect();
  if (r.top < w.top)         wrap.scrollTop += r.top - w.top - 4;
  else if (r.bottom > w.bottom) wrap.scrollTop += r.bottom - w.bottom + 4;
}

/* ── Equity curve ─────────────────────────────────────────────────────────────
   A fictitious account that starts at config.equityStart and is multiplied by
   (1 + r) as each trade closes — the same compounding rule as the P/L column,
   drawn out over time instead of collapsed to one number. The x-axis spans the
   active filter window (or the full data range when unfiltered), so every
   strategy's curve covers the same period and they can be read against each
   other.                                                                      */
function tsOf(s) { return s ? new Date(s.replace(" ", "T")).getTime() : NaN; }
function dayOf(ts) {
  const d = new Date(ts);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
         String(d.getDate()).padStart(2, "0");
}

/* ── Date span, in calendar months and days ───────────────────────────────────
   Counted by calendar, not by dividing days by 30.44: Jan 1 → Apr 1 reads as
   "3 months" whatever the month lengths were. Both ends are inclusive, so a
   single selected day reads "1 day" rather than "0 days" — the exclusive end is
   the day after `hi`. Day arithmetic is rounded because a DST boundary inside
   the span makes one of the days 23 or 25 hours long.                          */
/* Months added with the day-of-month clamped to the target month's length.
   Plain setMonth() overflows instead — Jan 31 plus one month becomes Mar 3, not
   Feb 28 — which lands the anchor past the end of the span and yields a
   negative day count. Always measured from the start date, never accumulated,
   so repeated steps cannot drift. */
function addMonths(d0, n) {
  const t = new Date(d0.getFullYear(), d0.getMonth() + n, 1);
  const last = new Date(t.getFullYear(), t.getMonth() + 1, 0).getDate();
  t.setDate(Math.min(d0.getDate(), last));
  return t;
}

function spanOf(lo, hi) {
  const a = new Date(lo + "T00:00:00"), b = new Date(hi + "T00:00:00");
  if (isNaN(a) || isNaN(b) || b < a) return null;
  b.setDate(b.getDate() + 1);
  let m = (b.getFullYear() - a.getFullYear()) * 12 + (b.getMonth() - a.getMonth());
  let anchor = addMonths(a, m);
  if (anchor > b) anchor = addMonths(a, --m);
  return { m, d: Math.round((b - anchor) / 86400000) };
}

function fmtSpan(lo, hi) {
  const s = spanOf(lo, hi);
  if (!s) return "—";
  const parts = [];
  if (s.m) parts.push(s.m + " month" + (s.m === 1 ? "" : "s"));
  if (s.d || !s.m) parts.push(s.d + " day" + (s.d === 1 ? "" : "s"));
  return parts.join(" ");
}

/* Time window shown by the charts: explicit filter values win, else data extent. */
function viewWindow(trades) {
  const days = trades.map(t => t.day).sort();
  const lo = $("from").value || days[0];
  const hi = $("to").value   || days[days.length - 1];
  if (!lo || !hi) return null;
  return [new Date(lo + "T00:00:00").getTime(), new Date(hi + "T23:59:59.999").getTime()];
}

function equityPath(closed, t0, t1) {
  const start = PAYLOAD.config.equityStart;
  const pts = closed
    .filter(t => t.pnl !== null && t.pnl !== undefined && t.t1)
    .map(t => ({ ts: tsOf(t.t1), pnl: t.pnl, t }))
    .filter(p => !isNaN(p.ts))
    .sort((a, b) => a.ts - b.ts);
  let bal = start;
  const path = [{ ts: t0, bal }];
  for (const p of pts) {
    bal *= (1 + p.pnl);
    // An exit can fall outside the window when the filter cuts mid-trade; clamp
    // so the step still registers at the edge rather than drawing off-canvas.
    // The trade rides along so the hover marker can find its step on the curve.
    path.push({ ts: Math.min(Math.max(p.ts, t0), t1), bal, t: p.t });
  }
  path.push({ ts: t1, bal });
  return path;
}

/* Returns { html, marks, xPct } rather than a bare string: `marks` maps each
   closed trade object to the point on the curve where it settled, and `xPct`
   turns any timestamp into a horizontal position. Both are in % of the plot
   box, which is what the HTML hover overlay is positioned in. */
const MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

/* Every 1st of the month inside the window. Walked with setMonth() from day 1 —
   safe from the month-length overflow that bites addMonths(), and it stays
   pinned to the real 1st instead of drifting the way a fixed 30-day step would. */
function monthTicks(t0, t1) {
  const out = [], d = new Date(t0);
  d.setHours(0, 0, 0, 0); d.setDate(1);
  if (d.getTime() < t0) d.setMonth(d.getMonth() + 1);
  while (d.getTime() <= t1) {
    out.push({ ts: d.getTime(), m: d.getMonth(), y: d.getFullYear() });
    d.setMonth(d.getMonth() + 1);
  }
  return out;
}

function equityChart(closed, win) {
  if (!win) return { html: "", marks: new Map(), xPct: () => null };
  const start = PAYLOAD.config.equityStart;
  const path  = equityPath(closed, win[0], win[1]);
  const W = 800, H = 150, PY = 10;
  const t0 = path[0].ts, span = Math.max(1, path[path.length - 1].ts - t0);

  const bals = path.map(p => p.bal).concat([start]);
  let lo = Math.min(...bals), hi = Math.max(...bals);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const padY = (hi - lo) * 0.08;
  lo -= padY; hi += padY;

  const X = ts => (ts - t0) / span * W;
  const Y = b  => PY + (hi - b) / (hi - lo) * (H - 2 * PY);

  let d = `M ${X(path[0].ts).toFixed(2)} ${Y(path[0].bal).toFixed(2)}`;
  for (let i = 1; i < path.length; i++) {
    d += ` H ${X(path[i].ts).toFixed(2)} V ${Y(path[i].bal).toFixed(2)}`;   // step, not slope
  }
  const area = d + ` V ${H} H ${X(path[0].ts).toFixed(2)} Z`;

  /* Month boundaries. Lines are drawn for every month; the labels beside them
     are thinned when months pack tighter than ~7% of the width, so a two-year
     window shows "Jan Apr Jul …" instead of overlapping text. January carries
     the year. The last few percent are left unlabelled — there is no room to
     set the text before the right edge. */
  const ticks = monthTicks(win[0], win[1]);
  const gapPct = ticks.length > 1 ? (X(ticks[1].ts) - X(ticks[0].ts)) / W * 100 : 100;
  const every  = Math.max(1, Math.ceil(7 / Math.max(gapPct, 0.01)));
  const monthLines = ticks.map(t =>
    `<line class="eqmonth" x1="${X(t.ts).toFixed(2)}" y1="0" x2="${X(t.ts).toFixed(2)}" y2="${H}"></line>`
  ).join("");
  const monthLabels = ticks.map((t, i) => {
    const x = X(t.ts) / W * 100;
    if (i % every || x > 94) return "";
    return `<span style="left:${x.toFixed(2)}%">${MONTH_ABBR[t.m]}${t.m === 0 ? " ’" + String(t.y).slice(2) : ""}</span>`;
  }).join("");

  // Every interior path point is one trade's step, so the same X/Y transform
  // that drew the curve gives the marker its position — no second geometry.
  const marks = new Map();
  for (let i = 1; i < path.length - 1; i++) {
    if (path[i].t) marks.set(path[i].t, { x: X(path[i].ts) / W * 100, y: Y(path[i].bal) / H * 100 });
  }
  const xPct = ts => isNaN(ts) ? null : Math.min(100, Math.max(0, (ts - t0) / span * 100));

  const final = path[path.length - 1].bal;
  const peak  = Math.max(...path.map(p => p.bal));
  const trough = Math.min(...path.map(p => p.bal));
  const ret   = final / start - 1;
  const col   = final >= start ? "var(--green)" : "var(--red)";
  const n     = path.length - 2;

  const html = `<div class="eqwrap">
    <div class="eqhead">
      <span>Fictitious account balance — starts at <b>${start}</b>, compounded over ${n} closed trade${n === 1 ? "" : "s"}</span>
      <span>Peak <b>${peak.toFixed(1)}</b> · Trough <b>${trough.toFixed(1)}</b> ·
            Final <b class="${plClass(ret)}">${final.toFixed(1)}</b> (${signed(ret)})</span>
    </div>
    <div class="eqplot">
      <svg class="eq" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
           aria-label="Account balance over time, ending at ${final.toFixed(1)}">
        ${monthLines}
        <path d="${area}" fill="${col}" fill-opacity=".12" stroke="none"></path>
        <line x1="0" y1="${Y(start).toFixed(2)}" x2="${W}" y2="${Y(start).toFixed(2)}"
              stroke="var(--border-2)" stroke-width="1" stroke-dasharray="4 4"></line>
        <path d="${d}" fill="none" stroke="${col}" stroke-width="1.75"
              stroke-linejoin="round"></path>
      </svg>
      <div class="eqmonths" aria-hidden="true">${monthLabels}</div>
      <div class="eqmark" hidden aria-hidden="true">
        <span class="eqband"></span><span class="eqvline"></span><span class="eqdot"></span>
        <span class="eqtipwrap"><span class="eqtip"></span></span>
      </div>
    </div>
    <div class="eqaxis"><span>${esc(dayOf(win[0]))}</span><span>${esc(dayOf(win[1]))}</span></div>
  </div>`;
  return { html, marks, xPct };
}

function detailHtml(r, win) {
  const chip = (label, v, cls) => `<span class="chip ${cls || ""}">${esc(label)} <b>${v}</b></span>`;
  const durRow = (label, ds) => `<tr>
      <td>${esc(label)}</td><td>${ds ? ds.n : 0}</td>
      <td>${ds ? esc(fmtDur(ds.min)) : "—"}</td>
      <td>${ds ? esc(fmtDur(ds.avg)) : "—"}</td>
      <td>${ds ? esc(fmtDur(ds.max)) : "—"}</td></tr>`;

  // Numbered chronologically (#1 = oldest) but listed newest first, so the row
  // number keeps identifying which trade in the sequence it was — matching the
  // order the equity curve compounds them in.
  const trades = r.trades.slice().sort((a, b) => a.t0 < b.t0 ? -1 : 1)
                  .map((t, i) => ({ t, n: i + 1 })).reverse();
  const eq = equityChart(r.trades.filter(t => t.x !== null), win);

  /* Hover geometry, stashed on the row so the handler is pure DOM reads:
       mx0..mx1  the band spanning the trade's life on the timeline
       mvl       the vertical line — at the exit, or the entry if there is none
       my, mc    the dot on the curve and its outcome colour (closed trades with
                 a P/L only; a trade with no q/l data never moved the balance)
     An open trade has no exit, so its band runs to the right edge of the plot. */
  const markAttrs = t => {
    const x0 = eq.xPct(tsOf(t.t0));
    if (x0 === null) return "";
    const m  = eq.marks.get(t);
    const ex = m ? m.x : (t.t1 ? eq.xPct(tsOf(t.t1)) : null);
    const x1 = ex === null ? 100 : Math.max(ex, x0);
    return ` data-mx0="${x0.toFixed(2)}" data-mx1="${x1.toFixed(2)}"` +
           ` data-mvl="${(ex === null ? x0 : ex).toFixed(2)}"` +
           (m ? ` data-my="${m.y.toFixed(2)}" data-mc="${t.w === true ? "pos" : t.w === false ? "neg" : ""}"` : "");
  };

  const tradeRows = trades.map(({ t, n }) => {
    // Direction arrows stay in the normal text colour on purpose: green/red in
    // this table already means profit/loss, and a green "long" that lost money
    // would be actively misleading.
    const dir = t.d === 1  ? `<span class="dirmark">▲</span> Long`
              : t.d === -1 ? `<span class="dirmark">▼</span> Short`
              : `<span class="dim">—</span>`;
    const pl   = t.w === true ? `<span class="pos">Profit</span>`
               : t.w === false ? `<span class="neg">Loss</span>` : `<span class="dim">—</span>`;
    const size = (t.q !== null && t.l !== null) ? t.q.toFixed(2) + "% × " + t.l : "—";
    return `<tr${markAttrs(t)}>
      <td>${n}</td>
      <td>${esc(t.t0)}</td>
      <td>${dir}</td>
      <td>${esc(fmtPrice(t.p0))}</td>
      <td>${esc(t.x || "OPEN")}</td>
      <td>${esc(t.t1 || "(open)")}</td>
      <td>${esc(fmtPrice(t.p1))}</td>
      <td>${pl}</td>
      <td>${esc(fmtDur(t.dur))}</td>
      <td>${esc(size)}</td>
      <td title="target ${t.rw === null ? "—" : t.rw.toFixed(2) + "%"} / stop ${t.rk === null ? "—" : t.rk.toFixed(2) + "%"}">${t.rr === null ? "—" : t.rr.toFixed(2)}</td>
      <td class="${t.mv === null ? "dim" : t.mv >= 0 ? "pos" : "neg"}">${t.mv === null ? "—" : signed(t.mv / 100)}</td>
      <td class="${plClass(t.pnl)}">${signed(t.pnl)}</td></tr>`;
  }).join("");

  const runningNote = r.pnl === null ? "" :
    `<span class="chip">Compounded <b class="${plClass(r.pnl)}">${signed(r.pnl)}</b></span>` +
    `<span class="chip">Best <b class="pos">${signed(r.best)}</b></span>` +
    `<span class="chip">Worst <b class="neg">${signed(r.worst)}</b></span>`;

  // Long/short split over the same (window-filtered) trades as the table below.
  // Win rate per side counts only closed trades with a known outcome, like the
  // main win-rate column. Trades with an unknown side are left out of both.
  const side = d => {
    const all = r.trades.filter(t => t.d === d);
    const decided = all.filter(t => t.w === true || t.w === false);
    const wins = decided.filter(t => t.w === true).length;
    return { n: all.length, wr: decided.length ? wins / decided.length : null };
  };
  const lng = side(1), sht = side(-1);
  const sideChip = (label, s) =>
    chip(label, s.n + (s.wr === null ? "" : ` <span class="dim">· ${pct(s.wr)} win</span>`));
  const lsRatio = sht.n ? (lng.n / sht.n).toFixed(2)
                : lng.n ? "all long" : "—";
  const lsShare = lng.n + sht.n ? pct(lng.n / (lng.n + sht.n)) + " long" : null;
  const directionNote = lng.n + sht.n === 0 ? "" : `<div class="chips">
      ${sideChip("▲ Long", lng)}${sideChip("▼ Short", sht)}
      ${chip("L/S ratio", lsRatio)}${lsShare ? chip("Mix", lsShare) : ""}
    </div>`;

  return `<div class="detailbox">
    ${eq.html}
    ${directionNote}
    <div class="chips">
      ${chip("TP1", r.tp1, "g")}${chip("Spike", r.spike, "g")}
      ${chip("SL", r.sl, "r")}${chip("Timeout", r.timeout, "r")}${chip("Flip", r.flip, "r")}
      ${chip("Open", r.open)}
    </div>
    <div class="chips">${runningNote}
      ${r.noPnl ? chip("No q/l data", r.noPnl) : ""}</div>
    ${r.rr === null ? "" : `<div class="chips">
      ${chip("Planned R:R", r.rr.toFixed(2))}
      ${chip("Median", r.rrMedian.toFixed(2))}
      ${chip("Range", r.rrMin.toFixed(2) + "–" + r.rrMax.toFixed(2))}
      ${chip("Avg target", (r.avgReward === null ? "—" : r.avgReward.toFixed(2) + "%"), "g")}
      ${chip("Avg stop", (r.avgRisk === null ? "—" : r.avgRisk.toFixed(2) + "%"), "r")}
    </div>`}
    <p class="dtitle">Time in trade by outcome</p>
    <table class="mini">
      <thead><tr><th>Outcome</th><th>N</th><th>Min</th><th>Avg</th><th>Max</th></tr></thead>
      <tbody>${durRow("Profitable", r.durProfit)}${durRow("Losing", r.durLoss)}${durRow("All closed", r.dur)}</tbody>
    </table>
    <p class="dtitle" style="margin-top:16px">Trades (${trades.length})</p>
    <div class="tradewrap"><table class="mini">
      <thead><tr><th>#</th><th>Entry time</th><th>Dir</th><th>Entry $</th><th>Exit</th>
                 <th>Exit time</th><th>Exit $</th><th>P/L</th><th>Duration</th>
                 <th>Size (q × l)</th><th>R:R</th><th>Move</th><th>Acct P/L</th></tr></thead>
      <tbody>${tradeRows}</tbody>
    </table></div>
  </div>`;
}

function renderFoot(rows, months, span) {
  const sum = k => rows.reduce((a, r) => a + r[k], 0);
  const closed = sum("closed"), profit = sum("profit");
  const wr = closed ? profit / closed : null;
  const all = rows.flatMap(r => r.trades).filter(t => t.x !== null);
  const ds = durStats(all);
  // Same rule as the KPI tiles: compound the underlying trades. Summing the
  // per-strategy P/L column above would give a different, wrong number.
  const totalPnl = compoundReturn(all);
  // Trade-weighted mean over every visible trade, matching the per-row figure.
  const totalRr = mean(rows.flatMap(r => r.trades).map(t => t.rr));
  const openTotal = sum("open");
  $("foot").innerHTML = `<tr>
    <td>TOTAL</td>
    <td>${sum("entries")}${openTotal ? ` <span class="openmark" title="${openTotal} trade(s) still open">(${openTotal})</span>` : ""}</td>
    <td class="pos">${profit}</td><td class="neg">${sum("loss")}</td>
    <td>${sum("spike")}</td><td>${sum("timeout")}</td>
    <td><div class="wr"><span class="num">${pct(wr)}</span>
        <span class="bar"><span style="width:${wr === null ? 0 : wr * 100}%;background:${wrColor(wr)}"></span></span></div></td>
    <td>${totalRr === null ? '<span class="dim">—</span>' : totalRr.toFixed(2)}</td>
    <td><div class="pl"><span class="num ${plClass(totalPnl)}">${signed(totalPnl)}</span></div></td>
    <td class="${plClass(geoMean(all))}">${signed(geoMean(all))}</td>
    <td class="${plClass(monthlyReturn(totalPnl, months))}">${signed(monthlyReturn(totalPnl, months))}</td>
    <td class="${plClass(sortino(all, span))}">${fmtSortino(sortino(all, span))}</td>
    <td><div class="dur"${ds ? ` title="min ${esc(fmtDurFull(ds.min))} / avg ${esc(fmtDurFull(ds.avg))} / max ${esc(fmtDurFull(ds.max))}"` : ""}>
        <span class="durtext"><b>${ds ? esc(fmtDur(ds.min)) : "—"}</b> · <b>${ds ? esc(fmtDur(ds.avg)) : "—"}</b> · <b>${ds ? esc(fmtDur(ds.max)) : "—"}</b></span></div></td>
  </tr>`;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── Theme: follow the OS unless the viewer has chosen explicitly ──────────── */
function applyTheme(mode) {
  const dark = mode === "dark" || (mode === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  $("themeIcon").textContent = dark ? "☾" : "☀";
  $("themeLabel").textContent = mode === "auto" ? "Auto" : (dark ? "Dark" : "Light");
}
let themeMode = "auto";
try { themeMode = localStorage.getItem("pv-theme") || "auto"; } catch (e) {}
applyTheme(themeMode);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (themeMode === "auto") applyTheme("auto"); });
$("theme").onclick = () => {
  themeMode = themeMode === "auto" ? "light" : themeMode === "light" ? "dark" : "auto";
  try { localStorage.setItem("pv-theme", themeMode); } catch (e) {}
  applyTheme(themeMode);
};

/* ── Wire up filters ──────────────────────────────────────────────────────── */
const DAYS = TRADES.map(t => t.day).sort();
const DAY_MIN = DAYS[0] || "", DAY_MAX = DAYS[DAYS.length - 1] || "";
/* The "From" the report opens on, and the baseline "Reset filters" returns to —
   HTML_DEFAULT_FROM_DATE when set, otherwise the whole range. Already validated
   against the data on the Python side, so it is safe to use as-is. A quick-range
   key ("31d", "month", …) is resolved here via presetRange() (hoisted), so the
   dropdown shows that preset selected on load. */
const DEFAULT_FROM = (presetRange(PAYLOAD.config.defaultFrom) || {}).from
                  || PAYLOAD.config.defaultFrom || DAY_MIN;
$("from").min = $("to").min = DAY_MIN;
$("from").max = $("to").max = DAY_MAX;

/* ── Date range slider ────────────────────────────────────────────────────────
   The slider and the two date boxes are two views of one filter. The date boxes
   stay the source of truth: the slider writes into them and asks for a render,
   and render() pushes the values back out to the slider. That keeps them in
   step no matter what moved — a drag, a typed date, Reset, or a restored view.

   Positions are whole days from DAY_MIN, so a step is exactly one day and the
   round trip through the date boxes is lossless.                              */
const DAY0 = DAY_MIN ? new Date(DAY_MIN + "T00:00:00").getTime() : 0;
const DAY_SPAN = DAY_MAX ? Math.round((new Date(DAY_MAX + "T00:00:00").getTime() - DAY0) / 86400000) : 0;

// Rounded, not truncated: a DST change inside the range makes one day 23 or 25
// hours long, which floor() would turn into an off-by-one.
function dayIdx(iso, fallback) {
  const t = new Date(iso + "T00:00:00").getTime();
  if (isNaN(t)) return fallback;
  return Math.max(0, Math.min(DAY_SPAN, Math.round((t - DAY0) / 86400000)));
}
function idxDay(i) {
  const d = new Date(DAY0);
  d.setDate(d.getDate() + Number(i));      // calendar arithmetic, so DST cannot drift it
  return dayOf(d.getTime());
}

$("rlo").max = $("rhi").max = DAY_SPAN;
// One single day of data leaves nothing to drag; the date boxes still work.
$("rangefield").hidden = DAY_SPAN <= 0;

function syncSliderFromDates() {
  if (DAY_SPAN <= 0) return;
  const lo = dayIdx($("from").value, 0), hi = dayIdx($("to").value, DAY_SPAN);
  $("rlo").value = Math.min(lo, hi);
  $("rhi").value = Math.max(lo, hi);
  const a = +$("rlo").value / DAY_SPAN * 100, b = +$("rhi").value / DAY_SPAN * 100;
  $("rangefill").style.left  = a + "%";
  $("rangefill").style.width = (b - a) + "%";
}

/* Dragging a thumb past its partner pushes the partner along rather than
   letting the range invert. */
function onSlide(moved) {
  let lo = +$("rlo").value, hi = +$("rhi").value;
  if (lo > hi) { if (moved === "lo") hi = lo; else lo = hi; }
  $("from").value = idxDay(lo);
  $("to").value   = idxDay(hi);
  scheduleRender();
}

$("rlo").addEventListener("input", () => onSlide("lo"));
$("rhi").addEventListener("input", () => onSlide("hi"));

/* Two thumbs at the same position would leave the lower one buried and
   undraggable, so the nearer thumb is lifted before the press lands — on hover
   for a mouse, and on the press itself so a touch's second attempt works. */
function raiseNearest(e) {
  const r = $("rangewrap").getBoundingClientRect();
  if (!r.width || DAY_SPAN <= 0) return;
  const at = (e.clientX - r.left) / r.width * DAY_SPAN;
  const near = Math.abs(at - +$("rlo").value) <= Math.abs(at - +$("rhi").value) ? "rlo" : "rhi";
  $("rlo").style.zIndex = near === "rlo" ? 4 : 3;
  $("rhi").style.zIndex = near === "rhi" ? 4 : 3;
}
$("rangewrap").addEventListener("pointermove", raiseNearest);

/* Only the thumbs take the pointer, so a press on the bare track would
   otherwise do nothing; send the nearer thumb to it instead. A press that
   landed on a thumb is left alone — that one is the browser's drag to run. */
$("rangewrap").addEventListener("pointerdown", e => {
  raiseNearest(e);
  if (e.target.tagName === "INPUT" || DAY_SPAN <= 0) return;
  const r = $("rangewrap").getBoundingClientRect();
  if (!r.width) return;
  const at = Math.max(0, Math.min(DAY_SPAN, Math.round((e.clientX - r.left) / r.width * DAY_SPAN)));
  const lo = Math.abs(at - +$("rlo").value) <= Math.abs(at - +$("rhi").value);
  $(lo ? "rlo" : "rhi").value = at;
  onSlide(lo ? "lo" : "hi");
});

/* ── Quick-range dropdown ─────────────────────────────────────────────────────
   Every preset is anchored to DAY_MAX, the last day in the parsed data, not to
   today — an old report still opens on meaningful windows. Like the slider it
   only writes the date boxes; render() then re-selects whichever preset the
   dates happen to match, or "Custom" once they are edited by hand. */
function presetRange(key) {
  if (!DAY_MAX) return null;
  const end = new Date(DAY_MAX + "T00:00:00");
  const start = new Date(end);
  switch (key) {
    case "7d":      start.setDate(start.getDate() - 6); break;    // 7 days incl. the last
    case "31d":     start.setDate(start.getDate() - 30); break;
    case "3m":      start.setDate(start.getDate() - (31 * 3 - 1)); break;  // 93 days incl. the last
    case "6m":      start.setDate(start.getDate() - (31 * 6 - 1)); break;  // 186 days incl. the last
    case "month":   start.setDate(1); break;
    case "quarter": start.setMonth(start.getMonth() - start.getMonth() % 3, 1); break;
    case "year":    start.setMonth(0, 1); break;
    default: return null;
  }
  // Clamped so the date box never holds a day outside its own min.
  const from = dayOf(start.getTime());
  return { from: from < DAY_MIN ? DAY_MIN : from, to: DAY_MAX };
}

function syncPresetFromDates() {
  const from = $("from").value, to = $("to").value;
  const hit = [...$("preset").options].find(o => {
    const r = presetRange(o.value);
    return r && r.from === from && r.to === to;
  });
  $("preset").value = hit ? hit.value : "";
}

$("preset").addEventListener("change", () => {
  const r = presetRange($("preset").value);
  if (!r) return;
  $("from").value = r.from;
  $("to").value   = r.to;
  render();
});

/* Renders are coalesced to one per frame: a drag fires `input` far faster than
   the table and its expanded detail panels can be rebuilt. */
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}

/* ── Clear button on the name filter ──────────────────────────────────────── */
$("qclear").onclick = () => { $("q").value = ""; $("q").focus(); render(); };
$("q").addEventListener("keydown", e => {
  if (e.key === "Escape" && $("q").value) { e.preventDefault(); $("q").value = ""; render(); }
});

/* ── Persisting the view ──────────────────────────────────────────────────────
   Filters, display toggles and sort are written to localStorage on every render
   and restored on load, so a reload keeps whatever you were looking at.

   Two things the naive version gets wrong, both handled below:

   1. A regenerated report can carry new data. Date bounds that were merely the
      full range at save time are NOT a real filter, so they are re-derived from
      the current data — otherwise adding older logs would silently hide them
      behind a stale "from" date.
   2. Options that have a default in the Python config (sort, log scale) are only
      restored while that default is unchanged. Edit HTML_DEFAULT_SORT and
      regenerate, and the new setting wins instead of appearing to be ignored.

   Storage is per-origin and may be unavailable (private windows, file:// in some
   browsers), so every access is wrapped — the page works fine without it.       */
const STORE_KEY = "pv-report-view-v1";

function readStore() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); }
  catch (e) { return null; }
}
function writeStore(obj) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(obj)); } catch (e) {}
}
function clearStore() {
  try { localStorage.removeItem(STORE_KEY); } catch (e) {}
}

function saveState() {
  // Nothing stored means "the defaults", so a reset genuinely leaves no trace
  // rather than persisting a copy of the defaults.
  if (!viewIsCustom()) { clearStore(); return; }
  writeStore({
    q: $("q").value, from: $("from").value, to: $("to").value,
    logscale: $("logscale").checked,
    sortKey, sortDir,
    // The defaults in force when this was saved, so changes to them can win later.
    was: { sortKey: DEFAULT_SORT_KEY, sortDir: DEFAULT_SORT_DIR,
           logScale: PAYLOAD.config.logScale, dayMin: DAY_MIN, dayMax: DAY_MAX,
           defaultFrom: DEFAULT_FROM },
  });
}

function loadState() {
  const s = readStore();
  if (!s || !s.was) return false;
  $("q").value = typeof s.q === "string" ? s.q : "";
  // Only a genuinely narrowed bound is restored (see note 1 above). The baseline
  // for "from" is whatever it was pre-set to when this was saved: leave it
  // untouched and you get the *current* default, so editing
  // HTML_DEFAULT_FROM_DATE takes effect instead of being masked by a stale save.
  const wasFrom = s.was.defaultFrom || s.was.dayMin;
  $("from").value = (s.from && s.from !== wasFrom) ? s.from : DEFAULT_FROM;
  $("to").value   = (s.to   && s.to   !== s.was.dayMax) ? s.to   : DAY_MAX;
  if (s.was.logScale === PAYLOAD.config.logScale) $("logscale").checked = !!s.logscale;
  if (s.was.sortKey === DEFAULT_SORT_KEY && s.was.sortDir === DEFAULT_SORT_DIR
      && COLS.some(c => c.key === s.sortKey)) {
    sortKey = s.sortKey;
    sortDir = s.sortDir === 1 ? 1 : -1;
  }
  return true;
}

function applyDefaults() {
  $("q").value = "";
  $("from").value = DEFAULT_FROM;
  $("to").value   = DAY_MAX;
  $("logscale").checked  = PAYLOAD.config.logScale;
  sortKey = DEFAULT_SORT_KEY;
  sortDir = DEFAULT_SORT_DIR;
}

/* True when anything differs from the generated defaults — drives the badge that
   explains why the view is not the one the report was built with. */
function viewIsCustom() {
  return nameTerms().length > 0
      || $("from").value !== DEFAULT_FROM || $("to").value !== DAY_MAX
      || $("logscale").checked !== PAYLOAD.config.logScale
      || sortKey !== DEFAULT_SORT_KEY || sortDir !== DEFAULT_SORT_DIR;
}

function resetFilters() {
  applyDefaults();
  clearStore();
  render();
}

["q", "from", "to"].forEach(id => $(id).addEventListener("input", render));
$("logscale").addEventListener("change", render);
$("reset").onclick = resetFilters;

$("meta").textContent = PAYLOAD.meta.summary;
$("foot-note").innerHTML =
  "Account P/L is <b>compounded</b>: each trade returns (q ÷ 100) × leverage × price move, " +
  "and a set of trades combines as ∏(1 + r) − 1, never as a sum. " +
  "Avg/trade is the geometric mean — the constant per-trade return that compounds to the same total. " +
  "Avg/month is the same per month, over the selected date range (minimum one month) shared by every strategy. " +
  "All figures recompute against the current filters.<br>" +
  esc("Generated " + PAYLOAD.generated + " · " + PAYLOAD.meta.files.length +
      " log file(s): " + PAYLOAD.meta.files.join(", "));

applyDefaults();
loadState();      // silently a no-op when nothing was stored or storage is blocked
render();

/* Open the leading strategy's detail once, on load only — later filter changes
   must not keep re-expanding whatever floats to the top. */
if (PAYLOAD.config.expandTop && lastRowNames.length) {
  expanded.add(lastRowNames[0]);
  render();
}
</script>
</body>
</html>
"""


def trade_to_dict(t: Trade) -> dict:
    """Flatten a Trade for the browser. `day` is used for timezone-free date filtering."""
    return {
        "s":   t.strategy,
        "day": t.entry_time.strftime("%Y-%m-%d"),
        # Second precision is enough for display; sub-second detail never mattered
        # here and the strings are still sortable and parseable as-is.
        "t0":  t.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
        "t1":  t.exit_time.strftime("%Y-%m-%d %H:%M:%S") if t.exit_time else None,
        "x":   t.exit_type,
        "d":   t.direction,
        "p0":  t.entry_price,
        "p1":  t.exit_price,
        "w":   t.profitable,
        "dur": t.duration_seconds,
        "q":   t.qty_pct,
        "l":   t.leverage,
        "mv":  t.price_change_pct,   # directional price move, %
        "pnl": t.pnl_fraction,       # account effect, as a fraction
        "rr":  t.risk_reward,        # planned reward-to-risk ratio at entry
        "rw":  t.reward_pct,         # entry → take-profit distance, %
        "rk":  t.risk_pct,           # entry → stop-loss distance, %
    }


def write_html_report(all_stats: dict[str, StrategyStats], out_path: str, files: list[str],
                      date_from=None, date_until=None, strat: Optional[str] = None,
                      filtered: Optional[FilterCounts] = None,
                      default_sort: Optional[str] = None,
                      default_from: Optional[str] = None):
    # None = use the module-level config; the CLI passes its overrides here.
    if default_sort is None:
        default_sort = HTML_DEFAULT_SORT
    if default_from is None:
        default_from = HTML_DEFAULT_FROM_DATE

    trades = [trade_to_dict(t) for s in all_stats.values() for t in s.trades]
    trades.sort(key=lambda d: d["t0"])

    # Count strategies that actually have trades — build_trades() also registers a
    # strategy for an exit signal that never had a matching entry, and those empty
    # entries have nothing to show in the table.
    n_strats = len({t["s"] for t in trades})
    bits = [f"{n_strats} strategies", f"{len(trades)} trades"]
    if trades:
        bits.append(f"{trades[0]['day']} → {trades[-1]['day']}")
    if date_from or date_until:
        bits.append("date filter: "
                    f"{date_from.strftime('%Y-%m-%d') if date_from else '…'}"
                    f" → {date_until.strftime('%Y-%m-%d') if date_until else '…'}")
    if strat:
        bits.append(f"strategy filter: {strat}")
    if filtered and filtered.total:
        bits.append(f"{filtered.total} trades filtered ({filtered.describe()})")

    if default_sort not in HTML_SORT_COLUMNS:
        print(f"  [WARN] HTML default sort {default_sort!r} is not a known column; "
              f"falling back to 'pnl'. Valid: {', '.join(HTML_SORT_COLUMNS)}", file=sys.stderr)
    sort_key = default_sort if default_sort in HTML_SORT_COLUMNS else "pnl"

    # Validated here rather than in the browser: a typo should be a visible
    # warning at generation time, not a silently ignored filter in the report.
    default_from = (default_from or "").strip()
    if default_from in HTML_FROM_PRESETS:
        pass                               # a preset key; the page resolves it
    elif default_from:
        days = sorted(t["day"] for t in trades) if trades else []
        try:
            datetime.strptime(default_from, "%Y-%m-%d")
        except ValueError:
            print(f"  [WARN] HTML default from-date {default_from!r} is not a YYYY-MM-DD "
                  f"date or a quick-range key ({', '.join(HTML_FROM_PRESETS)}); "
                  f"ignoring it and opening on the full range.", file=sys.stderr)
            default_from = ""
        else:
            if days and default_from > days[-1]:
                print(f"  [WARN] HTML default from-date {default_from!r} is after the last "
                      f"day of data ({days[-1]}); ignoring it, or the report would open "
                      f"on an empty table.", file=sys.stderr)
                default_from = ""
            elif days and default_from < days[0]:
                default_from = ""          # same as the full range; let the page derive it

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "sortKey":     sort_key,
            "sortDir":     -1 if HTML_DEFAULT_SORT_DESC else 1,
            "expandTop":   bool(HTML_EXPAND_TOP_ROW),
            "equityStart": HTML_EQUITY_START,
            "logScale":    bool(HTML_LOG_SCALE_DURATION),
            "tradingDays": TRADING_DAYS_PER_YEAR,
            "defaultFrom": default_from,      # "" = earliest day in the data
        },
        # Per strategy, not per trade: the market and timeframe come from the
        # strategy's first entry signal and are the same for all of its trades,
        # so shipping them once each keeps them out of 2000 trade records.
        "markets": {s.name: s.market_label for s in all_stats.values() if s.market_label},
        "meta": {
            "summary": "  ·  ".join(bits),
            "files":   [os.path.basename(f) for f in files],
        },
        "trades": trades,
    }

    # json.dumps output is embedded in a <script>; escape the one sequence that
    # could terminate it early if a strategy name ever contained it.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    # Heading brand: both logos are emitted and CSS shows whichever matches the
    # active theme. alt="ProfitView" keeps the heading readable if the images
    # cannot be fetched (offline) or are disabled in config.
    if HTML_LOGO_FOR_LIGHT_THEME and HTML_LOGO_FOR_DARK_THEME:
        def img(url, cls):
            return (f'<img class="logo {cls}" src="{escape(url, quote=True)}" '
                    f'alt="ProfitView" width="1010" height="193" '
                    f'referrerpolicy="no-referrer">')
        brand = (img(HTML_LOGO_FOR_LIGHT_THEME, "logo-light")
                 + img(HTML_LOGO_FOR_DARK_THEME, "logo-dark"))
    else:
        brand = "ProfitView"

    html = (HTML_TEMPLATE
            .replace("/*__DATA__*/ null", blob)
            .replace("<!--__BRAND__-->", brand)
            .replace("__LOGO_H__", str(int(HTML_LOGO_HEIGHT_PX))))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


# ── CSV export ────────────────────────────────────────────────────────────────

CSV_COLUMNS = (
    "strategy", "market", "timeframe", "direction",
    "entry_time", "entry_price", "exit_time", "exit_price", "exit_type",
    "duration_s", "duration", "profitable", "price_move_pct", "pnl_pct",
    "qty_pct", "leverage", "tp_price", "sl_price", "reward_pct", "risk_pct", "risk_reward",
    "strategy_sortino",
)


def write_csv_report(all_stats: dict[str, StrategyStats], out_path: str) -> int:
    """
    One row per trade, sorted by entry time. A flip is the old trade's exit
    (exit_type FLIP) plus a separate row for the trade it opened. Still-open
    trades have empty exit columns. Numbers are written unformatted so they stay
    machine-readable; returns the number of rows written.

    strategy_sortino is a per-strategy figure, repeated on each of its trades so
    the file stays a single flat table; it is measured over the same shared
    period as the console report.
    """
    def num(v):
        return "" if v is None else v

    def calc(v):
        # Derived ratios carry float noise (1.2200000000000015); prices are left as logged.
        return "" if v is None else round(v, 6)

    span = sortino_days([t for s in all_stats.values() for t in s.trades])

    rows = []
    for s in all_stats.values():
        sortino = calc(sortino_ratio(s.closed, span))
        for t in s.trades:
            dur = t.duration_seconds
            rows.append({
                "strategy":       t.strategy,
                "market":         s.market or "",
                "timeframe":      s.timeframe or "",
                "direction":      {1: "long", -1: "short"}.get(t.direction, ""),
                "entry_time":     t.entry_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "entry_price":    num(t.entry_price),
                "exit_time":      t.exit_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if t.exit_time else "",
                "exit_price":     num(t.exit_price),
                "exit_type":      t.exit_type or "",
                "duration_s":     "" if dur is None else round(dur, 3),
                "duration":       "" if dur is None else fmt_duration_full(dur),
                "profitable":     {True: "yes", False: "no"}.get(t.profitable, "") if t.exit_type else "",
                "price_move_pct": calc(t.price_change_pct),
                "pnl_pct":        calc(t.pnl_pct),
                "qty_pct":        num(t.qty_pct),
                "leverage":       num(t.leverage),
                "tp_price":       num(t.tp_price),
                "sl_price":       num(t.sl_price),
                "reward_pct":     calc(t.reward_pct),
                "risk_pct":       calc(t.risk_pct),
                "risk_reward":    calc(t.risk_reward),
                "strategy_sortino": sortino,
            })
    rows.sort(key=lambda r: (r["entry_time"], r["strategy"]))

    # utf-8-sig so Excel detects the encoding when the file is double-clicked.
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Parse ProfitView log files and report trading strategy statistics.\n\n"
            "Signals are detected via 'Received Alert' lines followed by '1: SIGNAL'.\n"
            "Price and direction come from the '2: #OHLCV,side:N,...' data line.\n"
            "Parameters may also be given inline as '1: SIGNAL(side:N,q:P,...)';\n"
            "those override same-named ones from the data line.\n"
            "Exit signals: STRATEGY_TP1 / STRATEGY_SPIKE (profit) or STRATEGY_SL /\n"
            "STRATEGY_TIMEOUT (loss). A repeated entry signal (flip) closes the\n"
            "previous trade as a loss, unless --no-flips is given. Trades open\n"
            "longer than --max-trade-hours are filtered; --filter-action decides\n"
            "whether filtered trades are removed or closed at their stop-loss."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        default=["."],
        help=(
            "One or more log files, glob patterns, or directories containing "
            "profitview-*.log files. Defaults to current directory."
        ),
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print individual trade details after the summary.",
    )
    parser.add_argument(
        "--strat",
        metavar="NAME",
        help="Filter output to a single strategy (case-insensitive, supports wildcards e.g. *XP*).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        metavar="DATE",
        help="Ignore events before this date (inclusive). Accepts YYYY-MM-DD, YY-MM-DD, or MM-DD.",
    )
    parser.add_argument(
        "--until",
        dest="date_until",
        metavar="DATE",
        help="Ignore events after this date (inclusive). Accepts YYYY-MM-DD, YY-MM-DD, or MM-DD.",
    )
    parser.add_argument(
        "--html",
        metavar="FILE",
        nargs="?",
        const="profitview-report.html",
        help=(
            "Also write an interactive HTML report (dark/light theme, sortable table, "
            "live name and date filtering). Defaults to profitview-report.html if no path given."
        ),
    )
    parser.add_argument(
        "--sort",
        metavar="COL",
        choices=CONSOLE_SORT_COLUMNS,
        default=CONSOLE_DEFAULT_SORT,
        help=f"Column the console summary table is sorted by: "
             f"{', '.join(CONSOLE_SORT_COLUMNS)}. Default: {CONSOLE_DEFAULT_SORT}.",
    )
    parser.add_argument(
        "--html-sort",
        metavar="COL",
        choices=HTML_SORT_COLUMNS,
        default=HTML_DEFAULT_SORT,
        help=f"Column the --html report's table is sorted by when it opens: "
             f"{', '.join(HTML_SORT_COLUMNS)}. Default: {HTML_DEFAULT_SORT}.",
    )
    parser.add_argument(
        "--html-from",
        metavar="DATE|PRESET",
        default=HTML_DEFAULT_FROM_DATE,
        help=f"Date pre-selected in the --html report's 'From' filter (YYYY-MM-DD, "
             f"YY-MM-DD, or MM-DD), or a quick-range preset to open on: "
             f"{', '.join(HTML_FROM_PRESETS)}. Pass \"\" for the full range. "
             f"Default: {HTML_DEFAULT_FROM_DATE or 'full range'}.",
    )
    parser.add_argument(
        "--csv",
        metavar="FILE",
        nargs="?",
        const="profitview-trades.csv",
        help=(
            "Also export every parsed trade (one row each, after all filters) to CSV. "
            "Defaults to profitview-trades.csv if no path given."
        ),
    )
    parser.add_argument(
        "--open",
        dest="open_html",
        action="store_true",
        help="Open the generated --html report in the default browser.",
    )
    parser.add_argument(
        "--no-pager",
        action="store_true",
        help=(
            "Print the report straight to stdout instead of paging it. Paging is "
            "skipped automatically when output is redirected. Set $PAGER to choose "
            "a different pager (default: less -RFX)."
        ),
    )
    flips = parser.add_mutually_exclusive_group()
    flips.add_argument(
        "--allow-flips",
        dest="allow_flips",
        action="store_true",
        default=ALLOW_FLIPS,
        help=f"Let a repeated entry signal close the open trade as a FLIP"
             f"{' (default)' if ALLOW_FLIPS else ''}.",
    )
    flips.add_argument(
        "--no-flips",
        dest="allow_flips",
        action="store_false",
        help=f"Filter the open trade when a flip occurs (see --filter-action)"
             f"{' (default)' if not ALLOW_FLIPS else ''}.",
    )
    parser.add_argument(
        "--max-trade-hours",
        type=float,
        metavar="H",
        default=MAX_TRADE_HOURS,
        help=f"Filter trades open longer than H hours (see --filter-action). "
             f"0 disables the check. Default: {MAX_TRADE_HOURS}.",
    )
    parser.add_argument(
        "--max-trade-pnl",
        type=float,
        metavar="PCT",
        default=MAX_TRADE_PNL_PCT,
        help=f"Remove trades whose closed account P/L exceeds PCT percent as a "
             f"profit or a loss, as faulty data. Such trades are always removed; "
             f"--filter-action does not apply. 0 disables the check. "
             f"Default: {MAX_TRADE_PNL_PCT:g}.",
    )
    parser.add_argument(
        "--filter-action",
        choices=FILTER_ACTIONS,
        default=FILTER_ACTION,
        help=f"What to do with a trade caught by the flip or max-hours filter: "
             f"'remove' drops it, 'sl' closes it at its entry stop-loss (removed "
             f"if it has no SL data). Does not affect --max-trade-pnl, which always "
             f"removes. Default: {FILTER_ACTION}.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Do not draw the parsing progress bar.",
    )

    args = parser.parse_args()

    # Flexible date parser: YYYY-MM-DD, YY-MM-DD, or MM-DD (assumes current year)
    def parse_date(raw: str, label: str) -> datetime:
        now = datetime.now()
        # MM-DD has no year, so prepend the current year and parse as a full
        # date — this avoids strptime's ambiguous-year DeprecationWarning.
        candidates = (
            ("%Y-%m-%d", raw),
            ("%y-%m-%d", raw),
            ("%Y-%m-%d", f"{now.year}-{raw}"),
        )
        for fmt, value in candidates:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        print(f"Invalid date for {label}: '{raw}' — accepted formats: YYYY-MM-DD, YY-MM-DD, MM-DD",
              file=sys.stderr)
        sys.exit(1)

    date_from  = parse_date(args.date_from,  "--from")  if args.date_from  else None
    date_until = parse_date(args.date_until, "--until") if args.date_until else None
    if date_until:
        date_until = date_until.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Only a value typed on the command line is normalised: the config default
    # is left as-is so write_html_report() can warn about a malformed one.
    html_from = args.html_from.strip()
    if html_from and html_from != HTML_DEFAULT_FROM_DATE and html_from not in HTML_FROM_PRESETS:
        html_from = parse_date(html_from, "--html-from").strftime("%Y-%m-%d")

    files = collect_log_files(args.paths)
    if not files:
        print("No log files found. Check your paths.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {len(files)} log file(s)…")
    for f in files:
        print(f"  {os.path.basename(f)}")

    events = extract_events(files, show_progress=not args.no_progress)
    if not events:
        print("No 'Received Alert' events found in the log files.", file=sys.stderr)
        sys.exit(0)

    print(f"\nFound {len(events)} alert event(s).")

    if date_from or date_until:
        before = len(events)
        events = [e for e in events
                  if (date_from  is None or e[0] >= date_from)
                  and (date_until is None or e[0] <= date_until)]
        print(f"Date filter applied: {before - len(events)} event(s) excluded, "
              f"{len(events)} remaining.")

    if FILTER_ACTION not in FILTER_ACTIONS:
        print(f"Invalid FILTER_ACTION = {FILTER_ACTION!r} in the configuration; "
              f"valid: {', '.join(FILTER_ACTIONS)}", file=sys.stderr)
        sys.exit(1)

    counts = FilterCounts()
    all_stats = build_trades(events, allow_flips=args.allow_flips,
                             max_trade_hours=args.max_trade_hours,
                             max_trade_pnl_pct=args.max_trade_pnl,
                             filter_action=args.filter_action, counts=counts)
    rules = [f"flips {'allowed' if args.allow_flips else 'filtered'}",
             f"max trade time {f'{args.max_trade_hours:g}h' if args.max_trade_hours else 'off'}",
             f"max trade P/L {f'{args.max_trade_pnl:g}%' if args.max_trade_pnl else 'off'}",
             f"filter action: {args.filter_action}"]
    print(f"Trade filters: {', '.join(rules)}.")
    if counts.total:
        print(f"  {counts.total} trade(s) filtered — {counts.describe()}.")

    if args.strat:
        pattern = args.strat.upper()
        all_stats = {k: v for k, v in all_stats.items() if fnmatch.fnmatch(k.upper(), pattern)}
        if not all_stats:
            print(f"No strategies matched '{args.strat}'.", file=sys.stderr)
            sys.exit(1)

    # The tables are captured rather than printed straight out so they can be
    # handed to a pager. Only the report itself is paged; the parsing chatter
    # above it has already been shown, and the --html line below belongs after
    # the pager exits.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_report(all_stats, verbose=args.verbose,
                     sort_key=args.sort, sort_desc=CONSOLE_DEFAULT_SORT_DESC)
    report = buf.getvalue()

    if args.no_pager or not sys.stdout.isatty():
        sys.stdout.write(report)
    else:
        page_text(report)

    if args.csv:
        csv_path = os.path.abspath(args.csv)
        n_rows = write_csv_report(all_stats, csv_path)
        print(f"CSV export ({n_rows} trades) written to {csv_path}")

    if args.html:
        out_path = os.path.abspath(args.html)
        write_html_report(
            all_stats, out_path, files,
            date_from=date_from, date_until=date_until, strat=args.strat,
            filtered=counts, default_sort=args.html_sort, default_from=html_from,
        )
        print(f"HTML report written to {out_path}")
        if args.open_html:
            import webbrowser
            webbrowser.open(f"file:///{out_path.replace(os.sep, '/')}")


if __name__ == "__main__":
    main()
