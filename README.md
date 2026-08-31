# Loaf Volume-Maximizing Trading Bot (70% Portfolio Utilization)

A production-ready, high-frequency **Python Trading Bot & SDK** for the [Loaf](https://loafmarkets.com) trading platform.

This bot is configured to:
1. **Utilize 70% of Portfolio Equity**: Safely allocates up to 70% of total portfolio value into active market making while preserving a 30% reserve buffer.
2. **Maximize Trading Volume**: Deploys tight two-sided limit quoting, spread capturing, and price improvement to maximize fill frequency and trade turnover.
3. **Continuous Inventory Recirculation**: Automatically balances inventory ($USDL \leftrightarrow Tokens$) to maintain infinite circular churn without getting stranded.
4. **Enforce Exchange & Risk Protocols**: Strictly adheres to the 10 USDL minimum notional, 1-decimal token quantity, 2-decimal limit price, single-use nonces, wash trading self-crossing prevention, and graceful platform halt handling.

---

## Architecture Overview

```
                        ┌──────────────────────────────────────────────┐
                        │              Loaf Markets Engine             │
                        └──────────────┬────────────────┬──────────────┘
               Public Market Feeds     │                │  Private Fills & Balances
        (orderbook, markprice, trades) │                │  (portfolio:userId channel)
                                       ▼                ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                Volume Maximizer Bot                                    │
│                                                                                        │
│  ┌─────────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────┐ │
│  │ Portfolio Sizer (70%)   │   │ Quote Engine             │   │ Inventory Rebalancer │ │
│  │ • 70% Active Budget     │──▶│ • Spread Tightening      │──▶│ • USDL <-> Token     │ │
│  │ • 30% Risk Reserve      │   │ • Price Improvement      │   │ • Circular Turnover  │ │
│  └─────────────────────────┘   └──────────────────────────┘   └──────────────────────┘ │
│                                              │                                         │
│                                              ▼                                         │
│                               ┌───────────────────────────┐                            │
│                               │ Order Reconciler & Exec   │                            │
│                               │ • Nonce mint & submit     │                            │
│                               │ • Stale order replacement │                            │
│                               │ • Invariant P_buy < P_sell│                            │
│                               └───────────────────────────┘                            │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Installation

```bash
git clone https://github.com/Loaf-Markets/loaf-python-api-bot-template.git
cd loaf-python-api-bot-template
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dotenv,dev]"
```

### 2. Configuration

Copy `.env.example` to `.env` and set your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# Your Loaf API Key from https://beta.loafmarkets.com/api
LOAF_API_KEY="your-api-key"

# REST base URL (must include /api)
LOAF_API_BASE_URL="https://api.loafmarkets.com/api"

# Target property token (e.g. opera)
LOAF_TARGET_TOKEN="opera"

# Portfolio allocation fraction (0.70 = 70%)
LOAF_PORTFOLIO_PCT="0.70"

# Optional: Loaf user ID for private fill websocket stream
LOAF_USER_ID="123"
```

### 3. Run the Bot

**Live Trading Mode (Default: 70% portfolio allocation):**
```bash
python bot.py
```

**Simulation / Dry-Run Mode:**
```bash
python bot.py --dry-run
```

**Inspect Market & Portfolio Snapshot:**
```bash
python bot.py --info
```

**Emergency Flatten (Cancel all active orders and exit):**
```bash
python bot.py --panic
```

---

## Strategy Details

### 1. 70% Portfolio Capital Model
- **Total Equity ($E$)**: $\text{Cash} + \text{Frozen Cash} + (\text{Token Position} \times \text{Reference Price})$.
- **Active Budget ($C_{active}$)**: $0.70 \times E$.
- **Risk Reserve**: $0.30 \times E$ kept untouched in free USDL.
- **Available Buy Power**: $\min(\text{Cash}, C_{active} - \text{Committed Capital})$.

### 2. Volume Maximization Mechanics
- **Price Improvement**:
  - When the book spread exceeds 2 ticks ($> \$0.02$), the bot places Limit Bids at $\text{Best Bid} + \$0.01$ and Limit Asks at $\text{Best Ask} - \$0.01$. This places the bot at the very top of the queue on both sides.
  - When the spread is tight ($\le \$0.02$), the bot joins top of book.
  - When the book is empty/one-sided, quotes are anchored around the official Mark Price.
- **Inventory Balancing**:
  - Targets a 50/50 split of the active 70% budget between USDL and Token inventory.
  - Skews orders when imbalance $> 15\%$ to liquidate excess tokens into USDL or acquire tokens to flip.
- **Capital Recycling on Fills**:
  - As soon as a BUY fill occurs, the acquired tokens are immediately queued for selling at a profitable or competitive ask.
  - As soon as a SELL fill occurs, the released USDL is immediately reused to submit a new buy quote.

### 3. Strict Safety Invariants
- **No Self-Crossing**: $P_{buy} < P_{sell}$ strictly enforced under all conditions.
- **Precision Validation**: Quantities pre-validated to $\le 1$ decimal place, prices to $\le 2$ decimal places.
- **Minimum Notional**: $\ge 10.0$ USDL per order verified locally before submitting.

---

## CLI Options

| Flag | Default | Description |
| --- | --- | --- |
| `--token` | `LOAF_TARGET_TOKEN` | Token name (e.g. `opera`) |
| `--portfolio-pct` | `0.70` | Fraction of portfolio to utilize (70%) |
| `--interval` | `2.0` | Strategy tick interval (seconds) |
| `--spread-bps` | `20.0` | Basis point offset for empty/one-sided books |
| `--tranches` | `1` | Number of order slices per side |
| `--stale-timeout` | `20.0` | Seconds before refreshing resting orders |
| `--dry-run` | `False` | Run in simulation without exchange orders |
| `--panic` | `False` | Cancel all active orders and exit |
| `--info` | `False` | Print portfolio & market snapshot and exit |

---

## Testing

Run the full offline test suite:

```bash
pytest tests/
```
# Trading-Bot
# Trading-Bot
