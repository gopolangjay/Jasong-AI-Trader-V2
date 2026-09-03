# V6.11 active XAUUSD liquidity/structure strategy

Gold retains its dedicated `XAUUSD_LIQUIDITY_STRUCTURE_V1` logic alongside the
new forex strategy. Other category strategies remain retired. Existing
positions opened by an old strategy are not force-closed; IG protection, the
server watchdog, and their scheduled exit continue to manage them.

## Execution sequence

Every new entry must pass all of these checks on completed candles:

1. H4 structure is bullish (HH/HL) for a buy or bearish (LH/LL) for a sell.
2. Price is in the appropriate H4 discount/premium area.
3. M15 sweeps rolling liquidity or the previous New York calendar-day high/low
   and closes back through the swept level.
4. A closed M15 candle confirms BOS or CHoCH after the sweep.
5. An order block or three-candle fair-value gap exists in the displacement leg.
6. Price retests that zone within the final three completed M15 candles.
7. The latest completed M15 candle confirms with engulfing, rejection, or
   displacement anatomy.
8. The structural invalidation leaves at least 2R before opposing liquidity.

The still-forming M15 candle is never used. H1 and H4 aggregates are built only
from completed M15 candles; incomplete higher-timeframe aggregates are omitted.

After the setup passes, a fresh IG DEMO Spot Gold quote must be tradeable and
within the configured 22-basis-point spread ceiling. Public Gold futures candles
provide structure; only the resulting price *distance* is transferred to the
fresh IG Spot Gold entry quote, avoiding the direct transfer of futures price
levels across the basis difference.

## Sessions and geography

Entries are weekdays only and require at least one of these local sessions to be
open:

- London: 08:00-17:00 `Europe/London`
- New York: 08:00-17:00 `America/New_York`

IANA timezones are evaluated for every signal, so UK and US daylight-saving
changes—including the weeks when their changeover dates differ—are automatic.

South African time (`Africa/Johannesburg`) is recorded with every candidate:

| Session | Daylight period | South African time |
|---|---|---|
| London | BST | 09:00-18:00 SAST |
| London | GMT | 10:00-19:00 SAST |
| New York | EDT | 14:00-23:00 SAST |
| New York | EST | 15:00-00:00 SAST |

The London/New York overlap is labelled but is not required when all other
conditions pass. A position is due to close by the earlier of the next New York
17:00 close or 12 hours after entry.

## Risk and duplicate controls

- IG DEMO only; there is no live-money route.
- Risk budget defaults to 1% of current account balance per trade.
- Size is valued from IG instrument metadata and converted to account currency.
- Size is rounded down to the broker increment. If IG minimum size exceeds the
  risk budget, the trade is skipped.
- Broker automatic upward size retries are disabled for these risk-sized orders.
- The stop sits beyond sweep/zone invalidation plus a 0.15 M15 ATR buffer.
- Valid stop distance must be between 0.35 and 3.0 M15 ATR.
- The take-profit target is at least 2R.
- At most one account-wide Gold position may be open.
- At most two XAUUSD entries may open per South African calendar day.
- A sweep/structure event has a stable setup ID and can be entered only once.
- IG-native stop/limit protection is attached where dealing rules allow; the
  server watchdog retains the original 1R stop and 2R target economics.

Configuration is deliberately capped at the strategy safety limits:

```text
XAU_RISK_PER_TRADE_PCT=1.0    # accepted range 0.10-1.00
XAU_MAX_DAILY_ENTRIES=2       # accepted range 1-2
JASONG_ACTIVE_EXECUTION_MARKETS=GOLD,FOREX_ALL
CATEGORY_AUTOTRADE=true
```

## Runtime checks

```bash
python -m unittest discover -s tests -p 'test*.py' -v
python backend/clean_core_smoke.py
```

Useful deployed diagnostics:

```text
GET /health
GET /market-categories/status
GET /market-categories/METALS
GET /category-portfolio/status
GET /trade-excursions/status
GET /forward-validation/status
```
