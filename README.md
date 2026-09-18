# parse_logs_strats.py

Turn your [ProfitView](https://profitview.app) log exports into a performance report for each of your trading strategies. The report comes as a table in the console and, if you want, as an interactive HTML page.

The script reads the alerts (signals) your strategies sent to ProfitView and pairs each entry with its exit. For every strategy it then shows:

- the number of trades, wins and losses, and how each trade was closed (take-profit, spike, stop-loss, timeout, flip)
- the win rate and the planned reward-to-risk ratio
- the **compounded account P/L**, plus the average return per trade and per month
- time in trade (min, average, max), split into winners and losers
- in the HTML report: an equity curve and a list of every trade, with filters for name and date range

It is a single file that uses only the Python standard library.

## Requirements

- Python 3.9 or newer
- No third-party packages

## Quick start

1. Put `parse_logs_strats.py` in the folder where you keep your logs.
2. **Adapt the configuration block at the top of the script to your own setup.** See [Configuration](#configuration). This step is not optional.
3. Export your logs from ProfitView and drop the files into that folder.
4. Run:

   ```
   python parse_logs_strats.py
   ```

   Or run this to also create an HTML report and open it in your browser:

   ```
   python parse_logs_strats.py --html --open
   ```

With no arguments, the script reads every `profitview-*.log` file in the current directory.

### Recommended: merge the exports first

ProfitView exports logs as several ~10 MB chunks, and each new export repeats part of the previous one. The companion tool [`merge_logs.py`](merge_logs.md) combines all exports into a single `profitview-merged.log` without duplicates and keeps it up to date:

```
python merge_logs.py
python parse_logs_strats.py profitview-merged.log --html --open
```

> **Warning: don't count trades twice.** A folder scan picks up *every* `profitview-*.log`, and that includes `profitview-merged.log`. If the export chunks are still in the folder next to the merged file, every trade is counted twice. After merging, either pass the merged file explicitly (as shown above) or delete the chunks.
>
> Don't feed the script overlapping exports without merging them first, for the same reason.

## How your alerts must be formatted

The script only understands alerts in the format below, so this section matters most. It looks for ProfitView's `Received Alert` log entries and reads the two command lines that follow each one:

```
2026-03-02 19:59:30.857 NOTICE [47162080552] Received Alert (S) for BYBIT:BTCUSDT 5m @ ... => Commands to run:
1: XPX4
2: #82674.6,82740.6,82674.6,82730.1,512.3,side:1,q:6.5,l:45,tp:84120.5,sl:81990.2
```

| Line | Content |
| ---- | ------- |
| header | Timestamp of the alert. `for BYBIT:BTCUSDT 5m` is optional; if present, it gives the strategy's market and timeframe. |
| `1:` | The **signal name**: the strategy name, plus a suffix if the signal is an exit. |
| `2:` | `#open,high,low,close,volume` of the signal candle, optionally followed by the trade parameters. The **close** price is used as the entry or exit price. |

### Signals sent through Telegram

Commands sent to the Telegram bot are read too:

```
2026-09-16 23:26:46.590 NOTICE [TelegramBot:110492452] Received commands from @user (...):
1: EDF3B(e=bybit,s=dogeusdt,res=5m)
2: #0.08025,0.08044,0.08023,0.08034,3468389,side:1,q:13.1,l:20,tp:0.08215,sl:0.07947
```

- These headers carry no market, so the market comes from the `s=` parameter. `dogeusdt.p` is read as `DOGEUSDT`. If `s=` is missing, `TELEGRAM_DEFAULT_MARKET` is used. The timeframe comes from `res=` and stays empty if that's missing.
- Inline parameters may use `key=value` or `key:value`, in any case (`Side=-1`).
- An **entry** only counts if it has a `2: #...` data line. That keeps chat commands like `Logvars` or `/pos` out of the report. Exits don't need one.
- A manually sent **entry** that only repeats an alert already in the log is ignored, so it doesn't open a second trade. See `TELEGRAM_DEDUPE_SECONDS`.
- A manually sent **exit** usually has no data line and therefore no exit price. In that case the script looks ahead in the log for the `markPrice` of the position query that ProfitView runs right after, and uses it as the exit price. See `TELEGRAM_MARK_PRICE_*`.

### Entry and exit signals

- **Entry:** the bare strategy name, e.g. `1: XPX4`. Any signal without an exit suffix counts as an entry.
- **`!` / `!!` prefix:** tells ProfitView to skip its filtering. The script drops it, so `!XPX4` and `!!XPX4_TP1` count as `XPX4` and `XPX4_TP1`.
- **Exit:** the strategy name plus one of the suffixes in `EXIT_SUFFIXES`, e.g. `1: XPX4_TP1`, `1: XPX4_SL`. The exit closes the trade that `XPX4` currently has open. An exit with no open trade is ignored.
- **Flip:** if a strategy sends a new entry while a trade is still open, the open trade is closed as a `FLIP` at the new entry's price, and a new trade opens. See `--no-flips` to filter these instead.

A strategy has one open trade at a time. Wins and losses are decided by **price**: a long wins if the exit is above the entry, a short if it is below. The exit type (`TP1`, `SPIKE` = win; `SL`, `TIMEOUT`, `FLIP` = loss) is only used as a fallback when prices or direction are missing.

### Trade parameters

The parameters are `key:value` pairs, separated by commas:

| Parameter (default key) | Meaning | Needed for |
| ----------------------- | ------- | ---------- |
| `side` | Direction. Long: `1`, `+1`, `1:0`, `long`, `buy`. Short: `-1`, `0:1`, `short`, `sell`. | win/loss by price, P/L, R:R |
| `q` | Position size, as a **percentage of the account** | P/L |
| `l` | Leverage multiplier. **Optional:** if it is missing, `DEFAULT_LEVERAGE` (1×) is assumed. | P/L |
| `tp` | Take-profit price | R:R |
| `sl` | Stop-loss price | R:R, `--filter-action sl` |

Keys the script doesn't know are ignored, so your payload can carry any extra data. A missing parameter doesn't break anything; the figures that depend on it just show `—`. The exception is leverage: without it, P/L is still calculated as if no leverage were used.

The minimum for P/L figures is therefore `side` and `q`. Without leverage, the returns are unleveraged, so they are smaller than your real account results but still fine for comparing how strategies perform relative to each other.

Parameters are read **from the entry signal**. The values on an exit signal (other than its close price) are not used, because the trade was opened with the entry's size and leverage.

You can place the parameters in either of two ways:

**Option A: parameters on line 2, after the candle**

```
1: XPX4
2: #82674.6,82740.6,82674.6,82730.1,512.3,side:1,q:6.5,l:45,tp:84120.5,sl:81990.2
```

**Option B: parameters inline on line 1, candle only on line 2**

```
1: XPX4(side:1,q:6.5,l:45,tp:84120.5,sl:81990.2)
2: #82674.6,82740.6,82674.6,82730.1,512.3
```

You can also combine both. If the same key appears in both places, the inline value on line 1 wins. Line 2 with at least the five OHLCV values is always required, because that's where the price comes from.

### How P/L is calculated

For each closed trade:

```
account return = price move (in trade direction) × (q ÷ 100) × leverage
```

`leverage` is the entry's `l` value, or `DEFAULT_LEVERAGE` (1×) if the alert has none.

Trades **compound**; they are not added up. A +50% trade followed by a −50% trade leaves the account at −25%, not break-even. So a strategy's *Account P/L* is ∏(1 + r) − 1 over its trades.

- **Avg/trade** is the geometric mean: the constant per-trade return that compounds to the same total.
- **Avg/month** is the same idea per month. It is measured over the report period (first to last entry day, or the selected date range in the HTML report; minimum one month), and every strategy uses that same period.
- **Sortino** is the annualised Sortino ratio with a 0% target. Each trade's P/L is booked on its exit day, and every day of the report period with no exits counts as 0%. The result is mean daily return ÷ downside deviation × √`TRADING_DAYS_PER_YEAR` (365 by default; set in the configuration block). It shows "—" when no day lost money, because the ratio has no upper bound then. In the CSV it is the `strategy_sortino` column, repeated on each of the strategy's trades.

Each strategy's P/L is calculated as if it had the account to itself. The TOTAL row compounds every trade of every strategy together.

## Configuration

Everything you are meant to change is in the `CONFIGURATION` block at the top of `parse_logs_strats.py`. **The defaults reflect the author's own setup. Check each setting against your own alert payloads before trusting the numbers.**

### Parameter keys: `FIELD_*`

```python
FIELD_SIDE = "side"
FIELD_QTY  = "q"
FIELD_LEV  = "l"
FIELD_TP   = "tp"
FIELD_SL   = "sl"

DEFAULT_LEVERAGE = 1.0
```

Set these to the keys *your* alerts use. For example, if your payload says `size:5,lev:20`, set `FIELD_QTY = "size"` and `FIELD_LEV = "lev"`. If a key doesn't match, the script reads nothing for it, and P/L and R:R stay empty (`—`) without any error.

`DEFAULT_LEVERAGE` is the leverage assumed for entries that carry no leverage value. Leave it at `1.0` for unleveraged returns, or set it to the leverage you always trade with if your alerts don't include it. Be careful with the leverage key: if `FIELD_LEV` doesn't match your payload, no error shows up and the P/L figures look plausible, just unleveraged.

### Exit suffixes: `EXIT_SUFFIXES`

```python
EXIT_SUFFIXES = ("_TP1", "_SPIKE", "_SL", "_TIMEOUT")
```

These are the suffixes that turn a signal name into an exit. Change them to match your own exit signals. Any signal that doesn't end in one of them is treated as an **entry**, so a missing suffix here makes your exits count as new trades (flips).

The console and HTML tables have fixed *Spike* and *Timeout* columns. Exits with other suffixes are still counted correctly as wins or losses by price.

### Signals to skip: `IGNORED_SIGNALS`

```python
IGNORED_SIGNALS = ("bal", "*_DEBUG", "TEST_*", ...)
```

Signals that aren't trades (balance checks, debug alerts, retired test strategies) go here. Entries are case-insensitive glob patterns. The shipped list contains the author's own names; replace it with yours.

### Telegram signals: `TELEGRAM_*`

```python
TELEGRAM_FIELD_MARKET       = "s"
TELEGRAM_FIELD_TIMEFRAME    = "res"
TELEGRAM_DEFAULT_MARKET     = "BTCUSDT"
TELEGRAM_MARK_PRICE_LINES   = 150
TELEGRAM_MARK_PRICE_SECONDS = 300
```

The first three are the keys that give a Telegram signal its market and timeframe, and the market used when the key is missing (`None` leaves it unknown).

The last two control the exit price of a manual exit, which arrives without a candle. The script then searches the next `TELEGRAM_MARK_PRICE_LINES` log lines for a `markPrice`, and accepts it only if its `symbol` matches the signal's market and the line is stamped no more than `TELEGRAM_MARK_PRICE_SECONDS` after the command. Set the line count to `0` to switch the fallback off.

The window is meant to reach past the command's own output, because the position query sometimes belongs to the next command a few seconds later. The symbol and age checks are what keep the search honest: the log is full of mark prices for other instruments, and a quiet log can put the next query hours later.

### Repeated signals: `TELEGRAM_DEDUPE_SECONDS`, `DUPLICATE_ALERT_SECONDS`

```python
TELEGRAM_DEDUPE_SECONDS = 3600
DUPLICATE_ALERT_SECONDS = 5
```

The same signal sometimes reaches the log twice. Left alone, the second copy opens a new trade and closes the first as a `FLIP` at the same price, which shows up as a zero-length trade at 0% P/L. Two cases, each with its own test:

| Setting | Case | Matched on |
| ------- | ---- | ---------- |
| `TELEGRAM_DEDUPE_SECONDS` | An alert that was disabled in ProfitView fired anyway, and you re-sent it by hand through Telegram with the payload pasted in. | Name, side, price, size, TP **and** SL — the copy is identical, and the gap can be up to an hour. Only the Telegram copy is dropped. |
| `DUPLICATE_ALERT_SECONDS` | The same alert is configured twice in TradingView, so it fires twice for the same bar. | Name, side and price only, since the two copies are computed moments apart and their size and levels can differ slightly. |

Set either to `0` to switch that case off.

**Keep `DUPLICATE_ALERT_SECONDS` small.** It ignores most of the payload, so the tiny window is what stops it from swallowing genuine signals. Repeats arrive within a second of each other; widening it toward one bar's length would start eating real entries at an unchanged price.

### Trade filters

| Setting | Default | What it does | CLI override |
| ------- | ------- | ------------ | ------------ |
| `ALLOW_FLIPS` | `True` | Whether a new entry while a trade is open closes that trade as a `FLIP`. If `False`, the open trade is filtered instead. | `--allow-flips` / `--no-flips` |
| `MAX_TRADE_HOURS` | `168` | Trades open longer than this are treated as stale and filtered. `0` disables the check. | `--max-trade-hours` |
| `MAX_TRADE_PNL_PCT` | `10.0` | Trades with an account P/L beyond ±this percentage are considered faulty data and always removed. `0` disables the check. | `--max-trade-pnl` |
| `FILTER_ACTION` | `"remove"` | What to do with trades caught by the flip or max-hours filter: `"remove"` drops them, `"sl"` closes them at their `sl` price. | `--filter-action` |

Pick `MAX_TRADE_PNL_PCT` to suit your position sizes and leverage. With high leverage, a legitimate trade can exceed 10%.

### Report defaults

| Setting | Meaning |
| ------- | ------- |
| `CONSOLE_DEFAULT_SORT`, `CONSOLE_DEFAULT_SORT_DESC` | Sort column and direction of the console table |
| `HTML_DEFAULT_SORT`, `HTML_DEFAULT_SORT_DESC` | Initial sort of the HTML table |
| `HTML_DEFAULT_FROM_DATE` | Initial "From" date filter of the HTML report: a date, a preset (`7d`, `31d`, `3m`, `6m`, `month`, `quarter`, `year`), or `""` for everything |
| `HTML_EXPAND_TOP_ROW` | Open the top strategy's details when the page loads |
| `HTML_EQUITY_START` | Starting balance of the equity curve |
| `HTML_LOG_SCALE_DURATION` | Start with log-scaled time-in-trade bars |
| `HTML_LOGO_*` | Logo shown in the page heading. Set to `""` for plain text. |
| `HTML_TRADE_CHART` | Whether the report's *Trade charts* box starts ticked: a candle chart of the trade when hovering a row in the trade list (needs a network connection) |
| `HTML_CHART_HOVER_MS` | How long the pointer must rest on a trade row before its chart opens, in milliseconds (default 400) |
| `LOG_UTC_OFFSET_HOURS` | Timezone of the log timestamps, used to fetch the right candles. `None` = this computer's local time, including daylight saving |

## Usage

```
python parse_logs_strats.py [PATH ...] [options]
```

`PATH` can be log files, glob patterns, or directories. Directories are searched for `profitview-*.log`. The default is the current directory.

### Selecting data

| Flag | Description |
| ---- | ----------- |
| `--strat NAME` | Only report strategies matching `NAME`. Case-insensitive, and wildcards work, e.g. `--strat "*XP*"`. |
| `--from DATE` | Ignore alerts before this date (inclusive). |
| `--until DATE` | Ignore alerts after this date (inclusive). |

Dates can be written as `YYYY-MM-DD`, `YY-MM-DD` or `MM-DD` (current year).

### Output

| Flag | Description |
| ---- | ----------- |
| `-v`, `--verbose` | Also print every individual trade after the summary tables. |
| `--sort COL` | Sort column of the console table: `name`, `market`, `entries`, `profit`, `loss`, `spike`, `timeout`, `winrate`, `rr`, `pnl`, `avgPnl`, `avgMonth`, `sortino`. |
| `--html [FILE]` | Also write the interactive HTML report. Default file: `profitview-report.html`. |
| `--html-sort COL` | Initial sort column of the HTML table: `name`, `entries`, `profit`, `loss`, `spike`, `timeout`, `winrate`, `pnl`, `avgPnl`, `avgMonth`, `sortino`, `dur`. |
| `--html-from DATE\|PRESET` | Initial "From" filter of the HTML report: a date or a preset (`7d`, `31d`, `3m`, `6m`, `month`, `quarter`, `year`). Pass `""` for the full range. |
| `--open` | Open the HTML report in your default browser. |
| `--csv [FILE]` | Also export every trade (after all filters) to CSV. Default file: `profitview-trades.csv`. |
| `--no-pager` | Print the report directly instead of through a pager. Paging is skipped automatically when output is redirected. Set `$PAGER` to choose a pager; the default is `less -RFX` if available. |
| `--no-progress` | Don't draw the parsing progress bar. |

### Trade filters

| Flag | Description |
| ---- | ----------- |
| `--allow-flips` / `--no-flips` | Let a new entry close the open trade as a `FLIP`, or filter that trade instead. |
| `--max-trade-hours H` | Filter trades open longer than `H` hours. `0` disables the check. |
| `--max-trade-pnl PCT` | Remove trades whose P/L exceeds ±`PCT`%. `0` disables the check. |
| `--filter-action {remove,sl}` | What happens to trades caught by the flip or max-hours filter. |

The defaults of all these flags come from the configuration block.

### Examples

Report on everything in the current folder:

```
python parse_logs_strats.py
```

The merged log, as an HTML report opened in the browser:

```
python parse_logs_strats.py profitview-merged.log --html --open
```

Every strategy with "XP" in its name, since March 1st, sorted by monthly return:

```
python parse_logs_strats.py profitview-merged.log --strat "*XP*" --from 2026-03-01 --sort avgMonth
```

Closing stale or flipped trades at their stop-loss instead of dropping them, which gives a more pessimistic view:

```
python parse_logs_strats.py --filter-action sl --no-flips
```

Exporting trades to CSV for your own analysis in a spreadsheet:

```
python parse_logs_strats.py profitview-merged.log --csv trades.csv --no-pager
```

## Example output

```
  STRATEGY SUMMARY  (sorted by Account P/L, descending)
===========================================================================================================
Strategy       Market          Entries  Profit   Loss  Spike  Timeout    Win%    R:R  Account P/L   Avg/trade   Avg/month
-----------------------------------------------------------------------------------------------------------
EXF5B          BTC 5m           96 (1)      48     47     19       10   50.5%   1.87      +72.75%      +0.58%      +6.12%
EXF3B          BTC 5m               72      37     35     11        2   51.4%   1.56      +57.08%      +0.63%      +5.03%
EDF3B          DOGE 5m              82      39     43     10        0   47.6%   1.66      +21.36%      +0.24%      +2.13%
EEV9x          ETH 15m              49      22     27      1        3   44.9%   1.40       -1.71%      -0.04%      -0.19%
```

- **Entries** `96 (1)`: 96 trades in total, 1 of them still open.
- **Profit / Loss**: closed trades won and lost.
- **Spike / Timeout**: how many trades closed through those exit signals.
- **R:R**: the planned reward-to-risk ratio, from the entry's `tp` and `sl`.

The console also prints a table of time in trade, split into winners and losers, and, above the tables, a summary of how many trades each filter caught.

## The HTML report

`--html` writes a single, self-contained HTML file. It needs no server; just open it in a browser. It offers:

- summary tiles (trades, win rate, account P/L, average per trade and per month, time in trade)
- a sortable strategy table; click a row to see that strategy's equity curve, statistics and trade list
- a strategy name filter, a date range with quick presets, and a date slider. All figures recalculate for the current selection.
  The name filter takes a comma-separated list of exact names (case-insensitive) with `*` and `?` wildcards: `xf4` shows only XF4, `*xf4` also shows EXF4, and `exf*, xpx*` shows both families.
- a price chart for each trade: rest the pointer on a row in the trade list to see the market's candles around the trade, with entry, exit, take-profit and stop-loss marked. Click the row to pin the chart, and press Esc to close it. Untick *Trade charts* next to the filters to turn this off. The candles come live from Bybit's public API, so this part needs a network connection. Strategies on timeframes below 1 minute are shown on 1m candles, and long trades on coarser ones.
- light and dark themes

The browser remembers your filters and sort order for the next time you open the report.

## Things to know

- **No match, no numbers.** If a strategy shows trades but `—` for P/L, its alerts are probably missing `side` or `q`, or they use different keys than the `FIELD_*` settings.
- **Market and timeframe** come from the first entry signal of a strategy that names them. If a strategy later signals on a different market or timeframe, the script prints a warning. A signal that doesn't name one (e.g. a Telegram command without `res=`) doesn't trigger it.
- **Open trades** at the end of the log are listed in *Entries* but don't count toward win rate or P/L.
- **Alerts and Telegram commands are read.** Commands sent in any other way are not counted.

## License

MIT. See [LICENSE](LICENSE).
