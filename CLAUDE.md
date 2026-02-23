# RFQ Bid Manager — PVF Procurement
## Claude Code Project Brief

---

## What This App Does

A local web application for managing competitive bids on **Pipes, Valves & Fittings (PVF)**
procurement. Field engineers issue RFQs (Requests for Quotation) to a pool of supply-house
bidders (SMP, EDGEN, WHITCO, IPS, DNOW, MRC, FLOW-ZONE, etc.). Each RFQ covers dozens to
hundreds of line items. Bidders return spreadsheets with unit prices and extended prices.

The app loads those spreadsheets into a SQLite database and provides:
- Award analysis (lowest complete bid, lowest possible per-item award)
- Price variance analysis (coefficient of variation per item)
- Subset optimisation (best k-bidder combination for k=1..n)
- Price trends across historical RFQs
- Bidder pattern analysis by item type
- **Potential RFQ estimation** — load an RFQ without bids, estimate costs from history
- AI-powered natural language queries via Claude API

**Run with:** `python rfq_app.py` → opens at `http://localhost:5050`

---

## File Structure

```
RFQ DATABASE/
├── rfq_app.py          # Flask app + all REST API endpoints
├── rfq_db.py           # SQLite schema, CRUD, analytics helpers
├── rfq_parser.py       # Auto-detecting Excel parser (handles 3 formats)
├── index.html          # Single-page frontend (vanilla JS, no framework)
├── rfq_database.db     # SQLite database (created on first run)
├── requirements.txt    # flask, openpyxl, anthropic
├── launch.bat          # Windows one-click launcher
├── launch.sh           # Mac/Linux launcher
├── CLAUDE.md           # This file
└── uploads/            # Temp folder for browser-uploaded files
```

---

## Database Schema

```sql
rfqs(
  rfq_id TEXT PK,       -- user-assigned, e.g. "HGA-2025-OCT"
  creator TEXT,         -- person who issued the RFQ
  station TEXT,         -- project/station name, e.g. "Station 155"
  rfq_date TEXT,        -- ISO date string
  source_file TEXT,     -- original xlsx filename
  sheet_name TEXT,      -- which sheet was parsed
  is_potential INTEGER, -- 1 = potential RFQ (no bids loaded), 0 = full RFQ
  notes TEXT,
  loaded_at TEXT        -- auto timestamp
)

rfq_items(
  id INTEGER PK AUTOINCREMENT,
  rfq_id TEXT FK → rfqs,
  item_number TEXT,     -- as it appears in the spreadsheet (may be "1", "1A", etc.)
  item_type TEXT,       -- first word of description: PIPE, ELL, TEE, GASKET, VALVE, FLANGE…
  specification TEXT,   -- rest of description after item_type, e.g. "SMLS, NPS 2, SCH 40"
  size TEXT,            -- pipe size if in a separate column, e.g. '10"'
  unit TEXT,            -- LF, EA, SETS, FEET…
  quantity REAL
)

bidders(
  id INTEGER PK AUTOINCREMENT,
  name TEXT UNIQUE      -- UPPER-CASED company name
)

bids(
  id INTEGER PK AUTOINCREMENT,
  rfq_id TEXT FK → rfqs,
  item_id INTEGER FK → rfq_items,
  bidder_id INTEGER FK → bidders,
  unit_price REAL,      -- USD per unit
  ext_price REAL        -- USD total (unit_price × quantity, or as quoted)
)
```

Foreign keys are enforced. All cascading deletes are on. SQLite WAL mode is enabled.

---

## Excel Format Variations

The parser (`rfq_parser.py`) auto-detects and handles three formats seen in the wild.
New formats from new RFQs will likely be variations of these.

### Format A — Stacked header, separate columns
- Rows 1–4: bidder info (company name, contact, phone, email)
- Row 5: column headers
- RFQ columns: ITEM #, PREFAB, SIZE, DESCRIPTION, UNIT, QTY QUOTED, UNITS QUOTED
- Per-bidder columns (6 each): DELIVERY, WEEKS, UNIT PRICE, TOTAL PRICE, VENDOR COMMENTS, MANUFACTURER
- Example: `St 155 HGA 10-3-2025.xlsx` → Sheet `COMPLETE` (was `WO Bolts Gaskets`)

### Format B — Flat headers with bidder prefix
- Row 1: all headers in one row, bidder name as column prefix
- Pattern: `DNOW_UNIT_PRICE`, `DNOW_TOTAL_PRICE`, `EDGEN_UNIT_PRICE`, etc.
- RFQ columns: ITEM #, SIZE, DESCRIPTION, UNIT, QTY QUOTED
- Per-bidder cols (6 each): DELIVERY, DELIVERY_DATE, UNIT_PRICE, TOTAL_PRICE, MFR, COMMENTS
- Example: `St 155 HGA 2-20-2026.xlsx` → Sheet `Bid Comparison`

### Format C — Two-row header, no separate SIZE column
- Row 1: "RFQ" label then bidder names at column start positions
- Row 2: column headers (ITEM NO, QTY, UNITS, DESCRIPTION, then per-bidder)
- Per-bidder cols (5 each): UNIT COST, EXT. COST, DELIVERY ARO, COMMENTS, DELIVERY DATE
- SIZE is embedded in the DESCRIPTION text
- Example: `St150 Audubon 2-12-2026.xlsx` → Sheet `Complete Bids`

**Key parser detail:** The bidder-name detection uses a scoring heuristic that prefers
shorter company abbreviations (SMP, EDGEN) over full contact names (Liana Biondolilo)
which appear in rows below the company name row.

---

## Item Type / Specification Extraction

The DESCRIPTION field in RFQs is structured as:
`ITEM_TYPE, rest-of-specification`

Examples:
- `PIPE, SMLS, NPS 2, SCH XS/80 (0.218 WT), BARE, ASTM A106 B`
  → type=`PIPE`, spec=`SMLS, NPS 2, SCH XS/80 (0.218 WT), BARE, ASTM A106 B`
- `GASKET, SPL WND, NPS 2, CL 600, 1/8 THK, CARBON STEEL`
  → type=`GASKET`, spec=`SPL WND, NPS 2, CL 600, 1/8 THK, CARBON STEEL`
- `ELL, 90 DEG, NPS 2, SCH 80, BW, A234 WPB`
  → type=`ELL`, spec=`90 DEG, NPS 2, SCH 80, BW, A234 WPB`

Split is on the first comma. If no comma, split on first space.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve index.html |
| POST | `/api/parse-preview` | Parse xlsx, return preview (no DB write) |
| POST | `/api/load-rfq` | Confirm load into DB |
| GET | `/api/rfqs` | List all RFQs with counts |
| GET | `/api/rfq/<id>` | Full detail with items and bids |
| DELETE | `/api/rfq/<id>` | Delete RFQ and all bids (cascades) |
| GET | `/api/bidders` | All known bidder names |
| GET | `/api/files` | List xlsx files in the app folder |
| POST | `/api/query` | AI natural-language query via Claude API |
| GET | `/api/analysis/award-scenarios/<id>` | Award scenario analysis |
| GET | `/api/analysis/cv/<id>` | Coefficient of variance per item |
| GET | `/api/analysis/price-trends` | Price trends across RFQs |
| GET | `/api/analysis/bidder-patterns` | Bidder pricing patterns by item type |
| GET | `/api/analysis/subset-enum/<id>` | Subset optimisation (k=1..n bidders) |
| GET | `/api/analysis/estimate/<id>` | Historical price estimation (potential RFQs) |
| GET/POST | `/api/config` | Read/set Anthropic API key |

---

## Subset Optimisation (key algorithm)

For each subset size k (1 to n_bidders), enumerate all C(n,k) combinations of bidders.
For each combination, compute total cost = Σ min(ext_price) per item across bidders in subset.
Return the best (lowest-cost) combination for each k, with savings vs k=1 and vs k-1.

Practical result example (Audubon RFQ, 7 bidders, 167 items):
- k=1 (SMP): $297,253
- k=2 (DNOW+SMP): -8.3%
- k=3 (EDGEN+MRC+SMP): -12.8%  ← typical "sweet spot"
- k=7 (all): -16.3% (marginal gain ~0% over k=6)

Complexity is fine up to ~15 bidders (2^15 = 32,768 subsets).

---

## Historical Price Estimation (Potential RFQs)

When a potential RFQ is loaded (no bids), the `/api/analysis/estimate/<id>` endpoint
matches each item against historical bid data using:

1. **Exact item_type match** (required)
2. **Specification keyword overlap** — tokenise both specs, score by Jaccard similarity
3. **Size match bonus** — if sizes match exactly, boost score
4. Returns per-item: matched historical items, min/mean/max unit prices, which bidders
   quoted, most recent date, confidence level (HIGH/MEDIUM/LOW based on match score)

---

## Known Bidders (as of Feb 2026)

SMP, EDGEN, WHITCO, IPS, DNOW, MRC, FLOW-ZONE

New bidders are added automatically to the `bidders` table when RFQs are loaded.

---

## Planned Enhancements (Priority Order)

1. **Authentication** — Google OAuth preferred (email domain restriction).
   Libraries: `authlib`, `flask-session`. Needs Google Cloud Console OAuth 2.0 credentials.
   Alternative: named-user table in SQLite with bcrypt passwords.

2. **`.env` file** — Move `ANTHROPIC_API_KEY` and future OAuth secrets out of code/memory.
   Use `python-dotenv`. Never commit `.env` to git.

3. **Per-user attribution** — Add `loaded_by TEXT` to `rfqs`, `queried_by TEXT` + timestamp
   log table for AI queries.

4. **Role separation** — `viewer` (query/analysis only) vs `editor` (load/delete RFQs).

5. **HTTPS + deployment** — Render.com (easiest) or local network server.
   For Render: add `gunicorn` to requirements, `Procfile`, switch DB to PostgreSQL if
   concurrent writes become an issue.

6. **Nightly backup** — One-line scheduled task: copy `rfq_database.db` to timestamped file.

7. **Power BI** — Connect directly to `rfq_database.db` via SQLite ODBC driver.
   All 4 tables are normalised and indexed. No changes needed to the DB for this.

---

## Development Notes for Claude Code

- **Python 3.9+** required (uses walrus operator and f-strings throughout)
- **No ORM** — raw SQLite via `sqlite3` stdlib. Keep it that way for simplicity.
- **No JS framework** — vanilla JS only. Keep it that way unless complexity demands otherwise.
- The frontend is one file (`index.html`) with embedded CSS and JS. For maintainability,
  consider splitting into `static/` once the file exceeds ~1500 lines.
- `rfq_app.py` uses `DB_PATH` as a module-level constant. Pass it explicitly to all
  `rfq_db` functions rather than using a global default.
- The AI query endpoint (`/api/query`) sends the full schema + current DB context to
  Claude on every request. This is intentionally stateless.
- SQLite parameter binding uses `.replace("?", f"'{rfq_id}'")`  in some places —
  this is a known shortcut that should be replaced with proper parameterised queries
  before any public deployment to prevent SQL injection.
- `launch.bat` / `launch.sh` auto-open the browser after a 1.2s delay.
