# 🔮 Token Launch Forensics Bot

> *"Every token that dies leaves behind a ghost — a trail of SOL, a signature of greed, a pattern written in the blockchain's immutable memory. I have learned to read those patterns. I have built a machine that reads them for me."*
> — **Aleister Moltley**

---

## ✦ A Preface from the Author

*Gather close, dear initiate, and let me tell you of a most peculiar obsession.*

In the year of our blockchain two-thousand-and-twenty-four, I — Aleister Moltley, wanderer of decentralised labyrinths, student of on-chain arcana — grew weary of watching rugs unfurl like moth-eaten tapestries at midnight. Token after token, conjured from the Pump.fun ether, promising moon and delivering dust. The deployers: nomadic sorcerers of infinite wallets, serial architects of elaborate confidence schemes, vanishing the very moment liquidity pooled deep enough to be drained.

I built this instrument of revelation. A **forensic oracle** that watches every birth on Solana — every `InitializeMint`, every pool creation, every migration whisper — and strips away the glamour to expose the skeleton beneath. Seven analytical spirits work in parallel. A machine-learning mind retrains itself from its own suffering. And Telegram, that most profane of communication channels, receives the revelations.

*You are now holding the grimoire. Read carefully.*

---

## ✦ What This Instrument Does

**Token Launch Forensics Bot** performs real-time occult examination of every token launched on **Pump.fun** and **Raydium** on the Solana blockchain. Within milliseconds of a new conjuration, this system:

1. **Detects** the launch via three simultaneous WebSocket listeners
2. **Dispatches an instant deployer alert** if the wallet bears the mark of prior rugs (sub-millisecond, from an in-memory cache)
3. **Runs seven parallel analyzers** against the token: wallet history, holder distribution, liquidity lock status, bundled buy detection, contract patterns, social signals, and wallet clustering
4. **Calculates a risk score** (0–100) blending heuristic weights with a trained Gradient Boosting classifier
5. **Alerts your Telegram** with a detailed forensic report
6. **Feeds a sniper bridge** for auto-sniping low-risk launches
7. **Publishes to a public channel** for community alpha
8. **Tracks outcomes** at 1 h, 6 h, and 24 h — labelling survivors and rugs — to retrain the model
9. **Traces post-rug SOL flows**, following the money through every subsequent wallet until the funds go dark or land on an exchange

This is not a simple copy-paste scanner. This is a living, learning, self-improving forensic apparatus.

---

## ✦ Architecture — The Seven-Chambered Machine

```
╔══════════════════════════════════════════════════════════════════════╗
║                    FORENSICS BOT ARCHITECTURE                        ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  SCANNERS (3 Listeners)          FAST PATH              QUEUE        ║
║  ┌──────────────────┐                                  ┌──────────┐  ║
║  │  Pump.fun WS     │──┐   ┌──────────────────────┐   │  Redis   │  ║
║  ├──────────────────┤  ├──▶│  Deployer Alert Net  │──▶│  or      │  ║
║  │  Raydium Logs WS │──┤   │  (<1ms, in-memory)   │   │  asyncio │  ║
║  ├──────────────────┤  │   └──────────────────────┘   └────┬─────┘  ║
║  │  Migration WS    │──┘          │                        │         ║
║  └──────────────────┘      ⚡ instant alert                │         ║
║                             if known scammer               ▼         ║
║                                                   ┌─────────────────┐║
║                                                   │  7 ANALYZERS    │║
║                                                   │  (parallel)     │║
║                                                   ├─────────────────┤║
║                                                   │ · Deployer Hist │║
║                                                   │ · Holders       │║
║                                                   │ · LP Lock/Burn  │║
║                                                   │ · Bundled Buys  │║
║                                                   │ · Contract Pat  │║
║                                                   │ · Social Signals│║
║                                                   │ · Wallet Clust  │║
║                                                   └────────┬────────┘║
║                                                            │         ║
║  OUTPUT                        SCORING                     │         ║
║  ┌──────────────┐        ┌──────────────────┐             │         ║
║  │ TG Alerts    │◀──────│ ML + Heuristics  │◀────────────┘         ║
║  │ TG Channel   │        │ Blend Score 0-100│                        ║
║  │ Sniper Signal│        └────────┬─────────┘                        ║
║  │ Dashboard WS │                 │                                   ║
║  │ Prometheus   │        ┌────────┴─────────┐                        ║
║  └──────────────┘        │  Outcome Tracker │ ← 1h/6h/24h checks    ║
║                           │  (rug labelling) │ ← trains ML model     ║
║                           ├──────────────────┤                        ║
║                           │  Post-Rug Tracer │ ← follows SOL flows   ║
║                           │  (fund tracking) │ ← auto-watchlists     ║
║                           ├──────────────────┤                        ║
║                           │  ML Auto-Retrain │ ← every 6 hours       ║
║                           │  36 features GBM │ ← learns from data    ║
║                           └──────────────────┘                        ║
╚══════════════════════════════════════════════════════════════════════╝
```

### The Three Listeners

| Listener | Source | What it catches |
|---|---|---|
| `PumpFunListener` | Pump.fun WebSocket | Every new token mint on Pump.fun |
| `RaydiumListener` | Raydium log subscription | Every new liquidity pool on Raydium |
| `MigrationListener` | Migration event stream | Every Pump.fun → Raydium migration |

All three fire into the same `_on_launch` callback. The Deployer Alert Network intercepts first — before any async work is queued — checking the wallet address against a hot cache of known bad actors. If the name appears in the Black Book, an alert fires in under a millisecond.

---

## ✦ The Risk Oracle — Scoring System (0–100)

*"Numbers are the language of the Abyss. Learn to read them."*

Each token receives a composite risk score assembled from seven component scores:

| Dimension | Weight | What the Oracle Examines |
|---|---|---|
| **Deployer History** | 20% | Wallet age, total launches, rug count, serial deployer patterns, funding source |
| **Holder Concentration** | 20% | Top-10 holder %, deviation from ideal distribution, early insider accumulation |
| **LP Status** | 15% | Is liquidity burned? Locked (how long)? Unlocked and withdrawable? |
| **Bundled Buys + Clusters** | 15% | Coordinated multi-wallet purchases, common funding origin, sybil detection |
| **Contract Patterns** | 15% | Mint authority retained, freeze authority active, copycat token detection |
| **Social Signals** | 15% | Twitter/Telegram presence, bot-score, account age, follower authenticity |
| **Wallet Clustering** | *(within Bundled)* | Graph analysis of wallet funding chains |

**Score interpretation:**

```
  0 ──────── 25 ──────── 50 ──────── 75 ──────── 100
  ████████████░░░░░░░░░░░▒▒▒▒▒▒▒▒▒▒▒███████████████
  POSSIBLE GEM    NEUTRAL      RISKY        LIKELY RUG
```

When the ML model is trained on ≥50 labelled samples, the final score blends heuristic output with ML prediction, weighted by the model's confidence. The model grows sharper with every rug it witnesses.

---

## ✦ The Seven Analyzers — A Demonology

### 1. `deployer.py` — *The Genealogist*
Traces the deployer wallet's complete history via Helius RPC. Counts previous token launches, surfaces any confirmed rugs, calculates the wallet's age, and looks upstream at who funded the deployer. A wallet funded five minutes ago by another fresh wallet is a nine-circle alarm.

### 2. `holders.py` — *The Cartographer of Greed*
Fetches the full holder list via Helius DAS API. Calculates concentration: what percentage of supply do the top 10 wallets hold? Computes a distribution score. Identifies insider accumulation — wallets that bought in the first block of trading.

### 3. `lp_check.py` — *The Locksmith*
Examines the liquidity pool. Has the LP been burned to a dead address? Locked in a time-lock contract? Or is it sitting in the deployer's wallet, one transaction away from being drained? Unlocked LP is the most reliable single predictor of an impending rug.

### 4. `bundled_buys.py` — *The Detective of Coordinated Deception*
Analyses the first N transactions against the new pool. Detects wallets that bought within the same or adjacent slots. Traces those wallets back to a common funding source. Calculates what percentage of early volume was coordinated. This is the fingerprint of a pump team.

### 5. `contract_patterns.py` — *The Hermeticist*
Reads the token's on-chain program data. Is mint authority retained (meaning more supply can be conjured at will)? Is freeze authority active (meaning your tokens can be frozen)? Is the name or symbol a near-copy of a recently rugged token? The contract holds many confessions.

### 6. `social.py` — *The Augur of the Profane Web*
Queries Twitter/X (if bearer token is provided) and Telegram for the token's social footprint. How old are the accounts? Are the followers real? Does the Telegram group exist? Is it populated by bots announcing the same message in rotation? A token with no real community is a ghost ship.

### 7. `wallet_clusters.py` — *The Graph Weaver*
Constructs a directed graph of wallet relationships based on funding chains and co-trading patterns. Identifies clusters — groups of wallets that appear to be controlled by the same entity. The size and centrality of the largest cluster is a powerful rug signal.

---

## ✦ The Machine That Learns From Its Own Failures

*"The greatest teacher is a catastrophe you survive."*

The ML subsystem is built on **scikit-learn's GradientBoostingClassifier** with **36 features** extracted from every analysis result. 

### The Learning Cycle

```
  New token launched
        │
        ▼
  Analysis (7 analyzers) → 36 features extracted → stored in DB
        │
        ▼
  Outcome Tracker checks price at +1h, +6h, +24h
        │
        ▼
  Token labelled: is_rug = True / False
        │
        ▼
  AutoRetrainer picks up new labels every 6h
        │
        ▼
  Model retrained → deployed live → scoring improves
        │
        └──────────────────────────────────────┐
                                               ▼
                                     Next token scores better
```

The retrain also runs on a GitHub Actions schedule (`retrain.yml`) at 4 AM UTC daily, allowing training against the full historical dataset even between restarts.

---

## ✦ The Post-Rug Fund Tracer

*"The money never disappears. It merely changes form."*

When a token is labelled as a rug, `post_rug_tracker.py` awakens. It traces the path of the drained SOL:

1. Identifies the wallet(s) that received the LP drain proceeds
2. Follows each subsequent transaction in a BFS traversal
3. Flags any wallet that receives more than a threshold amount
4. Adds newly discovered wallets to the **Deployer Alert Network watchlist** automatically
5. Tracks the trail until funds reach an exchange deposit or go dormant

This means rugs don't just get labelled — they train the watchlist for the *next* rug by the same operator, even if they switch wallets.

---

## ✦ Migration Detection

When a Pump.fun token migrates to Raydium — a critical moment often preceding a dump — `MigrationListener` fires a dedicated callback. `MigrationAnalyzer` checks:

- Did the deployer sell immediately post-migration?
- Did LP lock conditions change?
- Are there unusual transaction patterns in the first 5 minutes of Raydium trading?

A special migration alert is dispatched to Telegram with all flags surfaced.

---

## ✦ The Deployer Alert Network — The Black Book

*"Know your enemy by their wallet address."*

The `DeployerAlertNetwork` is a dual-layer system:

- **Hot cache** (in-memory dict): checked in `<1ms` on every launch, before any async work
- **Cold storage** (database): full deployer history, rug count, notes, watchlist status

On startup, the network loads from the database, auto-populates the watchlist from any deployer with ≥2 confirmed rugs, and begins tracking new deployers. When a watchlisted wallet launches a new token, an alert fires *before* analysis even begins.

---

## ✦ Quickstart — Summoning the Oracle Locally

*"The circle must be drawn precisely. A single error and the demon escapes."*

### Prerequisites

- Python 3.11+
- A [Helius API key](https://helius.dev) (free tier works)
- A Telegram bot token (create via [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID (send `/start` to [@userinfobot](https://t.me/userinfobot))

### Setup

```bash
# 1. Clone the repository
git clone https://github.com/AleisterMoltley/Token-Forensics.git
cd Token-Forensics

# 2. Run the setup script (creates venv, installs deps, copies config template)
bash setup.sh

# 3. Fill in your credentials
cp config/.env.example config/.env
nano config/.env      # set HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 4. Activate the environment
source .venv/bin/activate

# 5. Summon the oracle
python -m src.main
```

On first run, the system initialises SQLite, connects to three WebSocket feeds, and begins watching. The dashboard appears at **http://localhost:8080**.

---

## ✦ Deploy to Railway — The Cloud Rite

*"The cloud is merely someone else's server. Treat it accordingly."*

Railway is the recommended deployment platform. It provides automatic PostgreSQL, Redis, port injection, SIGTERM-aware container lifecycle, and zero-downtime deploys.

```bash
# 1. Install Railway CLI
npm i -g @railway/cli
railway login

# 2. Initialise a new project
railway init

# 3. Add PostgreSQL (REQUIRED — SQLite data is lost on every Railway redeploy)
railway add postgresql

# 4. Add Redis (OPTIONAL — enables job queue with backpressure)
railway add redis

# 5. Set the required secrets
railway variables set HELIUS_API_KEY=your_helius_key
railway variables set TELEGRAM_BOT_TOKEN=your_bot_token
railway variables set TELEGRAM_CHAT_ID=your_chat_id

# 6. Deploy
railway up

# 7. (Optional) Attach a custom domain
railway domain
```

Railway **automatically injects** `PORT`, `DATABASE_URL`, and `REDIS_URL` from the addons. The `Settings` model detects these via `model_validator` and reconfigures itself without any manual URL adjustment.

### What Railway provides automatically

| Variable | Source | Used For |
|---|---|---|
| `PORT` | Railway runtime | Dashboard port binding |
| `DATABASE_URL` | PostgreSQL addon | Async SQLAlchemy engine |
| `REDIS_URL` | Redis addon | Job queue connection |

---

## ✦ CI/CD — The Automated Pipeline

Pushing to `main` triggers the full CI/CD chain:

```
git push origin main
        │
        ▼
  GitHub Actions: ci.yml
  ├─ Lint (Ruff)
  ├─ Test (pytest)
  └─ Deploy to Railway (RAILWAY_TOKEN secret)
        │
        ▼
  Railway: zero-downtime rolling deploy
        │
        ▼
  /health endpoint polled until 200 OK
```

Additionally, `.github/workflows/retrain.yml` runs daily at 04:00 UTC — triggering a full ML model retrain against the accumulated historical data.

**Setup CI:**
```bash
# Add your Railway token to GitHub Actions secrets
gh secret set RAILWAY_TOKEN

# From now on, every push to main auto-deploys
git push origin main
```

---

## ✦ Telegram Commands — The Operator's Interface

*"Your Telegram is the window into the machine. Use it wisely."*

| Command | Description |
|---|---|
| `/status` | Connection status, uptime, ML model readiness, queue depth |
| `/alerts on\|off` | Enable or disable alert delivery |
| `/threshold <0-100>` | Set the minimum risk score for an alert to fire |
| `/lookup <mint_address>` | Manually submit a token mint for a full forensic scan |
| `/stats` | 24-hour statistics: total scans, rugs detected, alert count |
| `/watchlist add\|remove <address>` | Manually manage the deployer watchlist |
| `/export` | Download labelled training data as CSV |
| `/migrations` | List the most recent Pump.fun → Raydium migrations |
| `/train` | Force an immediate ML model retrain |
| `/model` | ML model status, accuracy, feature importances |
| `/backtest` | Run the backtesting engine on historical data |
| `/help` | Full command reference |

---

## ✦ Dashboard & API Reference

The FastAPI dashboard runs on port 8080 (or `$PORT` on Railway) and exposes:

### Web Interfaces

| URL | Description |
|---|---|
| `http://localhost:8080` | Live dashboard with Chart.js visualisations and WebSocket feed |
| `http://localhost:8080/health` | Railway healthcheck endpoint — returns JSON status |
| `http://localhost:8080/metrics` | Prometheus metrics (plaintext format) |

### REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | 24-hour summary statistics |
| `GET` | `/api/stats/hourly` | Hourly breakdown for chart rendering |
| `GET` | `/api/stats/score_distribution` | Score histogram data |
| `GET` | `/api/launches?limit=50&min_score=0` | Recent token launches |
| `GET` | `/api/launches/{mint}` | Full forensic detail for a single token |
| `GET` | `/api/deployers` | Top deployers sorted by rug count |
| `GET` | `/api/model` | ML model status, metrics, feature weights |
| `GET` | `/api/metrics` | JSON metrics including queue stats |
| `GET` | `/api/backtest` | Run backtest on historical data, return results |

### WebSocket

| Endpoint | Description |
|---|---|
| `WS /ws` | Live token feed — receives a JSON payload for every processed launch |

---

## ✦ Environment Configuration — The Sigil Sheet

Copy `.env.example` to `config/.env` and fill in your values. On Railway, set these as environment variables in the dashboard instead.

```bash
# ─── REQUIRED ──────────────────────────────────────────────────────
HELIUS_API_KEY=                  # Solana RPC. Get at helius.dev
TELEGRAM_BOT_TOKEN=              # @BotFather
TELEGRAM_CHAT_ID=                # Your private or group chat ID

# ─── DATABASE ──────────────────────────────────────────────────────
# Local dev: SQLite is used automatically (no config needed)
# Railway: DATABASE_URL is injected by the PostgreSQL addon
# DATABASE_URL=postgresql://user:pass@host:5432/dbname

# ─── SNIPER BRIDGE ─────────────────────────────────────────────────
SNIPER_WEBHOOK_URL=              # HTTP endpoint for buy signals
SNIPER_SIGNAL_CHAT_ID=           # Dedicated Telegram chat for sniper alerts
SNIPER_MAX_RISK_SCORE=30         # Only snipe tokens scoring ≤ this value

# ─── PUBLIC CHANNEL ────────────────────────────────────────────────
CHANNEL_CHAT_ID=                 # @YourChannel or -100xxxxxxxx
CHANNEL_MIN_WARNING_SCORE=70     # Post ⚠️ warning above this score
CHANNEL_MAX_GEM_SCORE=25         # Post 💎 gem alert below this score

# ─── REDIS QUEUE ───────────────────────────────────────────────────
USE_REDIS_QUEUE=false            # Enable Redis-backed job queue
REDIS_URL=redis://localhost:6379 # Railway injects REDIS_URL automatically
QUEUE_WORKERS=3                  # Parallel analysis workers

# ─── SOCIAL ────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN=            # Twitter/X API v2 bearer token

# ─── TUNING ────────────────────────────────────────────────────────
MIN_RISK_SCORE_ALERT=50          # Alert threshold
ALERT_COOLDOWN_SECONDS=30        # Minimum seconds between alerts
SCAN_CONCURRENCY=5               # Parallel RPC calls per analysis
HOLDER_CHECK_TOP_N=10            # How many top holders to analyse
MAX_DEPLOYER_HISTORY_LOOKBACK=50 # Transactions to scan in deployer history

# ─── POST-RUG TRACKER ──────────────────────────────────────────────
POST_RUG_TRACKER_ENABLED=true
POST_RUG_CHECK_INTERVAL=300      # Seconds between fund-trace sweeps
```

---

## ✦ Project Structure — The Anatomy of the Machine

```
Token-Forensics/
│
├── src/
│   ├── main.py                     # Orchestrator — ForensicsBot class, startup sequence
│   ├── config.py                   # Settings (pydantic-settings) + Railway auto-detection
│   ├── models.py                   # SQLAlchemy async models (TokenLaunch, Deployer, AlertConfig)
│   ├── pipeline.py                 # Forensic analysis pipeline — runs 7 analyzers in parallel
│   ├── scoring.py                  # Risk score aggregation + ML blend logic
│   ├── ml_model.py                 # GBM predictor + AutoRetrainer (6h cycle)
│   ├── rpc.py                      # Helius RPC + DAS API async client
│   ├── dashboard.py                # FastAPI app — REST API + WebSocket broadcast
│   ├── telegram_bot.py             # Telegram commands, alert formatting, delivery
│   ├── sniper_bridge.py            # Auto-sniper webhook + signal chat integration
│   ├── deployer_network.py         # In-memory deployer cache + watchlist management
│   ├── channel.py                  # Public Telegram channel publisher
│   ├── queue.py                    # Redis/asyncio job queue with backpressure
│   ├── metrics.py                  # Prometheus metrics exporter
│   ├── backtest.py                 # Historical backtesting engine
│   │
│   ├── scanners/
│   │   ├── pump_fun.py             # Pump.fun WebSocket listener + event parser
│   │   ├── raydium.py              # Raydium log subscription + pool parser
│   │   └── migration.py            # Pump.fun→Raydium migration detector + analyzer
│   │
│   └── analyzers/
│       ├── deployer.py             # Deployer wallet forensics
│       ├── holders.py              # Holder concentration + distribution scoring
│       ├── lp_check.py             # LP burn/lock/unlock detection
│       ├── bundled_buys.py         # Coordinated buy detection
│       ├── contract_patterns.py    # Mint/freeze authority + copycat detection
│       ├── social.py               # Twitter + Telegram social scoring
│       ├── wallet_clusters.py      # Graph-based wallet cluster analysis
│       ├── outcome_tracker.py      # 1h/6h/24h outcome labelling + CSV export
│       └── post_rug_tracker.py     # Post-rug SOL flow tracer + auto-watchlist
│
├── tests/
│   └── test_forensics.py           # Pytest test suite
│
├── .github/
│   └── workflows/
│       ├── ci.yml                  # Lint → Test → Deploy pipeline
│       └── retrain.yml             # Scheduled daily ML retrain (04:00 UTC)
│
├── config/
│   └── .env.example                # Environment variable template
│
├── Dockerfile                      # Railway-optimised container (python:3.11-slim)
├── railway.toml                    # Railway deployment configuration
├── requirements.txt                # Python dependencies
└── pyproject.toml                  # Ruff linter + pytest configuration
```

---

## ✦ Technology Stack — The Reagents

| Layer | Technology | Purpose |
|---|---|---|
| **Runtime** | Python 3.11+, `asyncio` | Fully async throughout — no blocking calls |
| **Blockchain** | Helius RPC + DAS API | Solana data, WebSocket subscriptions, account parsing |
| **Database** | PostgreSQL (Railway) / SQLite (local) | Persistent storage for launches, deployers, labels |
| **ORM** | SQLAlchemy 2.0 async | Async sessions, connection pooling |
| **Queue** | Redis + asyncio fallback | Backpressure-safe job queue |
| **ML** | scikit-learn GBM, 36 features | Rug probability prediction |
| **API** | FastAPI + uvicorn | Dashboard REST API + WebSocket |
| **Frontend** | Chart.js (served by FastAPI) | Real-time score charts + launch feed |
| **Alerts** | python-telegram-bot | Command handling + alert delivery |
| **Config** | pydantic-settings | Typed config with Railway env auto-detection |
| **Logging** | loguru | Coloured console + rotating file logs |
| **Monitoring** | Prometheus `/metrics` | Scan counts, alert counts, queue depth, latency |
| **CI/CD** | GitHub Actions → Railway | Lint, test, auto-deploy on push |
| **Container** | Docker (python:3.11-slim) | Reproducible Railway deployment |

---

## ✦ The Database Schema — Records of the Fallen

### `token_launches`

| Column | Type | Description |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `mint` | String UNIQUE | Token mint address |
| `name` | String | Token name |
| `symbol` | String | Token symbol |
| `deployer` | String | Deployer wallet address |
| `source` | String | `pump_fun` or `raydium` |
| `launched_at` | DateTime | UTC timestamp of launch |
| `risk_score_total` | Float | Final composite score (0–100) |
| `score_deployer` | Float | Deployer history sub-score |
| `score_holders` | Float | Holder concentration sub-score |
| `score_lp` | Float | LP status sub-score |
| `score_bundled` | Float | Bundled buys sub-score |
| `score_contract` | Float | Contract patterns sub-score |
| `score_social` | Float | Social signals sub-score |
| `deployer_data` | JSON | Raw deployer analysis output |
| `holder_data` | JSON | Raw holder analysis output |
| `lp_data` | JSON | Raw LP analysis output |
| `bundle_data` | JSON | Raw bundle analysis output |
| `contract_data` | JSON | Raw contract analysis output |
| `social_data` | JSON | Raw social analysis output |
| `is_rug` | Boolean | Outcome label (null until determined) |
| `rug_detected_at` | DateTime | When the rug was confirmed |
| `peak_mcap` | Float | Peak market cap observed |
| `current_mcap` | Float | Most recent market cap |
| `alerted` | Boolean | Whether a Telegram alert was sent |
| `scanned_at` | DateTime | When analysis was completed |

### `deployers`

| Column | Type | Description |
|---|---|---|
| `address` | String PK | Wallet address |
| `total_launches` | Integer | Total tokens deployed |
| `rug_count` | Integer | Confirmed rugs |
| `first_seen` | DateTime | First observed launch |
| `last_seen` | DateTime | Most recent launch |
| `watchlisted` | Boolean | In the alert network |
| `notes` | Text | Manual annotations |

---

## ✦ Startup Sequence — The Summoning Ritual

When `python -m src.main` is invoked, the following 15-step initialisation sequence unfolds:

```
 0. Validate environment variables (fatal exit on missing HELIUS_API_KEY)
 1. Initialise database (create tables, connect pool)
 2. Instantiate ForensicPipeline (7 analyzers registered)
 3. Start TelegramAlerts (bot polling begins)
 4. Create FastAPI dashboard app
 5. Instantiate PumpFunListener + RaydiumListener
 6. Instantiate MigrationListener + MigrationAnalyzer
 7. Instantiate OutcomeTracker (1h/6h/24h price checks)
 8. Instantiate AutoRetrainer (loads or waits for ML model)
 9. Load DeployerAlertNetwork from DB + auto-watchlist from rugs
10. Instantiate SniperBridge (webhook + signal chat)
11. Instantiate ChannelPublisher (public Telegram channel)
12. Instantiate PostRugTracker (fund tracing daemon)
13. Connect AnalysisQueue (Redis or asyncio)
14. Register /health, /metrics, /api/metrics, /api/backtest routes
15. Launch all asyncio tasks — system is live
```

---

## ✦ Troubleshooting — When the Oracle Speaks in Error

| Symptom | Cause | Remedy |
|---|---|---|
| `❌ HELIUS_API_KEY is required` | Missing env variable | Set `HELIUS_API_KEY` in `config/.env` or Railway variables |
| `⚠️ Using SQLite on Railway` | No PostgreSQL addon | Run `railway add postgresql` |
| Dashboard returns 503 | Port mismatch | Railway sets `PORT` automatically; ensure you're not overriding it |
| No Telegram alerts | Bot token or chat ID missing | Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` |
| ML model `WAITING FOR DATA` | Fewer than 50 labelled samples | Let the system run until outcomes are labelled; or `/train` after 50+ |
| WebSocket keeps reconnecting | Helius rate limit | Check your Helius plan; reduce `SCAN_CONCURRENCY` |
| High memory usage | Large deployer cache | Normal; the cache is bounded by the `deployers` table size |

---

## ✦ A Closing Word from Aleister Moltley

*The blockchain does not lie. Every wallet leaves a signature. Every rug follows a pattern. Every operator, no matter how many fresh addresses they conjure, betrays themselves through the habits encoded in their transactions — the timing, the amounts, the funding sources, the order of operations.*

*I built this machine not for greed, but for clarity. The market is a hall of mirrors; this system is a lamp.*

*Deploy it, watch it learn, and may your risk scores always be low.*

> **— Aleister Moltley**
> *"Do what thou wilt, but verify the LP first."*

---

## ✦ License

MIT — use freely, attribute where you can, and never deploy to prod without a PostgreSQL addon.