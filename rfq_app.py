"""
rfq_app.py — Flask REST API + static file server for the RFQ Bid Manager.

Run with:  python rfq_app.py
Then open: http://localhost:5050
"""

import os
import re
import json
import traceback
from pathlib import Path

print("[startup] 1 stdlib imports OK", flush=True)

from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for
from dotenv import load_dotenv

print("[startup] 2 flask/dotenv imports OK", flush=True)

import rfq_db
import rfq_parser

print("[startup] 3 local module imports OK", flush=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR   = Path(__file__).parent           # folder containing this file
load_dotenv(BASE_DIR / ".env")

DB_PATH    = os.environ.get("DB_PATH", str(BASE_DIR / "rfq_database.db"))
UPLOAD_DIR = BASE_DIR / "uploads"
try:
    UPLOAD_DIR.mkdir(exist_ok=True)
except Exception as _e:
    print(f"[startup] WARNING: could not create uploads dir: {_e}", flush=True)

print(f"[startup] 4 config OK — DB_PATH={DB_PATH}", flush=True)

# Claude API key
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Google OAuth
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SECRET_KEY           = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "")

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.secret_key = SECRET_KEY

print("[startup] 5 Flask app created OK", flush=True)

# Trust Render's (and any reverse proxy's) X-Forwarded-Proto / X-Forwarded-Host
# headers so url_for(_external=True) produces https:// URLs correctly.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ---------------------------------------------------------------------------
# Google OAuth setup
# ---------------------------------------------------------------------------

from authlib.integrations.flask_client import OAuth

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

print("[startup] 6 OAuth registered OK", flush=True)

# Public paths that don't require authentication
_PUBLIC_PATHS = {"/auth/login", "/auth/callback", "/auth/logout", "/health"}

@app.before_request
def require_login():
    if request.path in _PUBLIC_PATHS:
        return None
    if "user" not in session:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized", "login_url": "/auth/login"}), 401
        return redirect("/auth/login")

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return "OK", 200


@app.route("/auth/login")
def auth_login():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    try:
        token     = oauth.google.authorize_access_token()
    except Exception as e:
        traceback.print_exc()
        return (
            f"<h2>OAuth Error</h2><pre>{e}</pre>"
            f'<p><a href="/auth/login">Try again</a></p>'
        ), 500
    user_info = token.get("userinfo")
    if not user_info:
        return "Login failed: no user info returned.", 400
    email = user_info.get("email", "")
    if ALLOWED_EMAIL_DOMAIN and not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        return (
            f"<h2>Access Denied</h2>"
            f"<p>Only <strong>@{ALLOWED_EMAIL_DOMAIN}</strong> accounts are allowed.</p>"
            f"<p>You signed in as <code>{email}</code>. "
            f'<a href="/auth/logout">Try a different account</a></p>'
        ), 403
    session["user"] = {
        "email":   email,
        "name":    user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    return redirect("/")


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect("/auth/login")


# ---------------------------------------------------------------------------
# /api/me  — current logged-in user
# ---------------------------------------------------------------------------

@app.route("/api/me")
def api_me():
    return jsonify(session.get("user", {}))


# Initialise DB on startup — wrapped so a bad DB_PATH doesn't crash gunicorn
try:
    rfq_db.init_db(DB_PATH)
    print(f"[startup] 7 DB initialised at {DB_PATH}", flush=True)
except Exception as _db_err:
    print(f"[startup] 7 WARNING: DB init failed ({_db_err}) — app will start but DB calls will fail", flush=True)

print("[startup] 8 ALL DONE — routes loading", flush=True)


# ---------------------------------------------------------------------------
# Filename metadata extractor
# ---------------------------------------------------------------------------

def _parse_filename_metadata(filename):
    """
    Extract RFQ metadata from filenames matching the pattern:
        St{station}_{rfq_id}_{creator}_{MM-D-YYYY}.xlsx

    Example: St105_ME0003_AUDUBON_11-10-2025.xlsx
      → station="St105", rfq_id="ME0003", creator="AUDUBON",
        rfq_date="2025-11-10"

    Returns a dict on success, or None if the filename doesn't match.
    """
    name = os.path.splitext(filename)[0]
    m = re.match(r'^[Ss][Tt](\w+)\s*_\s*(\w+)\s*_\s*(\w+)\s*_\s*(\d{1,2}-\d{1,2}-\d{4})$', name)
    if not m:
        return None
    station_raw, rfq_id, creator, date_raw = m.groups()
    try:
        month, day, year = (int(p) for p in date_raw.split('-'))
        rfq_date = f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return None
    return {
        "station":  f"St{station_raw}",
        "project":  rfq_id,
        "creator":  creator,
        "rfq_date": rfq_date,
        "rfq_id":   rfq_id,
    }


# ---------------------------------------------------------------------------
# Static / SPA
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


# ---------------------------------------------------------------------------
# /api/parse-preview  — parse a file and return preview (no DB write yet)
# ---------------------------------------------------------------------------

@app.route("/api/parse-preview", methods=["POST"])
def parse_preview():
    """
    Accepts multipart form OR JSON with a 'filepath' field.
    Returns parsed structure for user confirmation.
    """
    try:
        filepath = None

        if request.files.get("file"):
            f = request.files["file"]
            filepath = str(UPLOAD_DIR / f.filename)
            f.save(filepath)
        elif request.is_json:
            data = request.get_json()
            filepath = data.get("filepath")
        else:
            form_path = request.form.get("filepath")
            if form_path:
                filepath = form_path

        if not filepath or not os.path.exists(filepath):
            return jsonify({"error": f"File not found: {filepath}"}), 400

        sheet_name = (request.get_json() or {}).get("sheet_name") if request.is_json else request.form.get("sheet_name")

        sheets = rfq_parser.list_sheets(filepath)
        result = rfq_parser.parse_excel(filepath, sheet_name)

        if "error" in result:
            return jsonify({"error": result["error"]}), 422

        # Build a concise preview for the UI
        filename = os.path.basename(filepath)
        preview = {
            "filepath":    filepath,
            "filename":    filename,
            "sheets":      sheets,
            "sheet_used":  result["sheet"],
            "format":      result["format"],
            "bidders":     result["bidders"],
            "item_count":  len(result["items"]),
            "ambiguities": result["ambiguities"],
            "sample_items": result["items"][:6],   # first 6 rows for display
            "meta_hint":   _parse_filename_metadata(filename),  # None if pattern not matched
        }
        return jsonify(preview)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/load-rfq  — confirm and write to DB
# ---------------------------------------------------------------------------

@app.route("/api/load-rfq", methods=["POST"])
def load_rfq():
    """
    Body (JSON):
      rfq_id, creator, station, project, rfq_date, filepath, sheet_name,
      is_potential (bool), notes (optional)
    """
    try:
        data = request.get_json()
        rfq_id     = data.get("rfq_id", "").strip()
        creator    = data.get("creator", "").strip()
        station    = data.get("station", "").strip()
        project    = data.get("project", "").strip() or None
        rfq_date   = data.get("rfq_date", "").strip()
        filepath   = data.get("filepath", "").strip()
        sheet_name = data.get("sheet_name") or None
        is_pot     = bool(data.get("is_potential", False))
        notes      = data.get("notes", "")

        if not rfq_id:
            return jsonify({"error": "rfq_id is required"}), 400
        if not os.path.exists(filepath):
            return jsonify({"error": f"File not found: {filepath}"}), 400

        parsed = rfq_parser.parse_excel(filepath, sheet_name)
        if "error" in parsed:
            return jsonify({"error": parsed["error"]}), 422

        rfq_db.load_parsed_rfq(
            rfq_id, creator, station, project, rfq_date,
            os.path.basename(filepath), parsed["sheet"],
            parsed, is_potential=is_pot, notes=notes,
            db_path=DB_PATH
        )

        return jsonify({
            "status":  "loaded",
            "rfq_id":  rfq_id,
            "items":   len(parsed["items"]),
            "bidders": parsed["bidders"],
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/rfqs  — list all RFQs
# ---------------------------------------------------------------------------

@app.route("/api/rfqs", methods=["GET"])
def list_rfqs():
    try:
        rows = rfq_db.get_all_rfqs(DB_PATH)
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/rfq/<rfq_id>  — detail + DELETE
# ---------------------------------------------------------------------------

@app.route("/api/rfq/<rfq_id>", methods=["GET"])
def rfq_detail(rfq_id):
    try:
        detail = rfq_db.get_rfq_detail(rfq_id, DB_PATH)
        if not detail:
            return jsonify({"error": "RFQ not found"}), 404
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rfq/<rfq_id>", methods=["DELETE"])
def delete_rfq(rfq_id):
    try:
        if not rfq_db.rfq_exists(rfq_id, DB_PATH):
            return jsonify({"error": "RFQ not found"}), 404
        rfq_db.delete_rfq(rfq_id, DB_PATH)
        return jsonify({"status": "deleted", "rfq_id": rfq_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/bidders  — list all known bidders
# ---------------------------------------------------------------------------

@app.route("/api/bidders", methods=["GET"])
def list_bidders():
    try:
        return jsonify(rfq_db.get_all_bidders(DB_PATH))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/query  — natural-language AI query
# ---------------------------------------------------------------------------

@app.route("/api/query", methods=["POST"])
def ai_query():
    """
    Body: { "question": "...", "rfq_id": "..." (optional) }
    Uses Claude to interpret the question, generate SQL, execute it,
    and return both the SQL and the formatted answer.
    """
    try:
        data      = request.get_json()
        question  = data.get("question", "").strip()
        rfq_id    = data.get("rfq_id")

        if not question:
            return jsonify({"error": "question is required"}), 400

        api_key = ANTHROPIC_API_KEY or data.get("api_key", "")
        if not api_key:
            return jsonify({"error": "No Anthropic API key configured. Set ANTHROPIC_API_KEY environment variable or pass api_key in the request."}), 400

        import anthropic

        context = rfq_db.get_context_for_ai(DB_PATH)
        schema  = rfq_db.get_schema_summary(DB_PATH)

        rfq_filter = ""
        if rfq_id:
            rfq_filter = f"\nThe user is focusing on RFQ '{rfq_id}'."

        system_prompt = f"""You are an expert data analyst for a Pipes, Valves and Fittings procurement database.
{schema}

Current data context:
- RFQs loaded: {json.dumps(context['rfqs'], indent=2)}
- Known bidders: {context['bidders']}
- Item types: {context['item_types']}
{rfq_filter}

Your job:
1. Interpret the user's question.
2. Write a SQLite SELECT query that answers it.
3. Return ONLY valid JSON in this exact format:
{{
  "sql": "<the SELECT statement>",
  "explanation": "<one sentence describing what the query does>"
}}

Rules:
- Use only SELECT statements (no INSERT/UPDATE/DELETE).
- Always join bidders table via bids.bidder_id = bidders.id to get bidder names.
- For award scenarios: find minimum unit_price or ext_price per item.
- For coefficient of variance: STDDEV is not in SQLite; use a subquery approach or note the limitation.
- If the question cannot be answered with SQL, set sql to "" and explain why.
- Monetary values are in USD.
"""

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": question}]
        )

        raw = msg.content[0].text.strip()

        # Parse Claude's JSON response
        try:
            # Strip any markdown code fences
            clean = raw
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:])
            if clean.endswith("```"):
                clean = "\n".join(clean.split("\n")[:-1])
            ai_resp = json.loads(clean.strip())
        except json.JSONDecodeError:
            return jsonify({"error": "AI returned non-JSON", "raw": raw}), 500

        sql  = ai_resp.get("sql", "")
        expl = ai_resp.get("explanation", "")

        rows = []
        error = None
        if sql:
            try:
                rows = rfq_db.run_query(sql, DB_PATH)
            except Exception as qe:
                error = str(qe)

        return jsonify({
            "question":    question,
            "sql":         sql,
            "explanation": expl,
            "rows":        rows,
            "row_count":   len(rows),
            "error":       error,
        })

    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        # Surface Anthropic auth errors clearly
        if "401" in msg or "authentication" in msg.lower() or "api_key" in msg.lower():
            return jsonify({"error": "Anthropic API key is missing or invalid. Add ANTHROPIC_API_KEY to Render's Environment settings."}), 400
        return jsonify({"error": msg}), 500


# ---------------------------------------------------------------------------
# /api/analysis/<type>  — pre-built analyses
# ---------------------------------------------------------------------------

@app.route("/api/analysis/award-scenarios/<rfq_id>", methods=["GET"])
def award_scenarios(rfq_id):
    """
    Returns for each item the lowest unit price across all bidders,
    and identifies which bidder wins.
    """
    try:
        sql = """
            SELECT
                i.item_number,
                i.item_type,
                i.specification,
                i.size,
                i.unit,
                i.quantity,
                b.unit_price,
                b.ext_price,
                d.name  AS bidder,
                b.unit_price = MIN(b.unit_price) OVER (PARTITION BY i.id) AS is_lowest
            FROM rfq_items i
            JOIN bids b    ON b.item_id  = i.id
            JOIN bidders d ON d.id       = b.bidder_id
            WHERE i.rfq_id = ?
            ORDER BY CAST(i.item_number AS REAL), i.item_number, b.unit_price
        """
        rows = rfq_db.run_query(sql.replace("?", f"'{rfq_id}'"), DB_PATH)

        # Lowest complete bid (bidder with lowest total across all items they bid)
        sql2 = """
            SELECT d.name AS bidder,
                   SUM(b.ext_price) AS total_ext,
                   COUNT(b.id)      AS items_bid
            FROM bids b
            JOIN bidders d ON d.id = b.bidder_id
            JOIN rfq_items i ON i.id = b.item_id
            WHERE i.rfq_id = ? AND b.ext_price IS NOT NULL
            GROUP BY d.name
            ORDER BY total_ext
        """.replace("?", f"'{rfq_id}'")
        totals = rfq_db.run_query(sql2, DB_PATH)

        # Lowest unit price per item (best possible award)
        sql3 = """
            SELECT i.item_number, i.item_type, i.specification,
                   MIN(b.unit_price) AS best_unit_price,
                   d.name            AS best_bidder,
                   i.quantity,
                   MIN(b.unit_price) * i.quantity AS best_ext_price
            FROM rfq_items i
            JOIN bids b    ON b.item_id = i.id
            JOIN bidders d ON d.id      = b.bidder_id
            WHERE i.rfq_id = ? AND b.unit_price IS NOT NULL
            GROUP BY i.id
            ORDER BY CAST(i.item_number AS REAL), i.item_number
        """.replace("?", f"'{rfq_id}'")
        best_items = rfq_db.run_query(sql3, DB_PATH)

        return jsonify({
            "rfq_id":       rfq_id,
            "all_bids":     rows,
            "bidder_totals": totals,
            "best_by_item": best_items,
            "best_total":   sum(r.get("best_ext_price") or 0 for r in best_items),
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/analysis/cv/<rfq_id>", methods=["GET"])
def coefficient_variance(rfq_id):
    """
    Returns price spread stats per item across all bidders.
    SQLite doesn't have STDDEV, so we compute via a Python post-process.
    """
    try:
        sql = """
            SELECT i.id, i.item_number, i.item_type, i.specification, i.size,
                   d.name AS bidder, b.unit_price
            FROM rfq_items i
            JOIN bids b    ON b.item_id = i.id
            JOIN bidders d ON d.id      = b.bidder_id
            WHERE i.rfq_id = ? AND b.unit_price IS NOT NULL AND b.unit_price > 0
            ORDER BY i.id
        """.replace("?", f"'{rfq_id}'")
        rows = rfq_db.run_query(sql, DB_PATH)

        import statistics

        # Group by item
        items_map = {}
        for r in rows:
            key = r["id"]
            if key not in items_map:
                items_map[key] = {
                    "item_number":   r["item_number"],
                    "item_type":     r["item_type"],
                    "specification": r["specification"],
                    "size":          r["size"],
                    "prices":        [],
                    "bidders":       [],
                }
            items_map[key]["prices"].append(r["unit_price"])
            items_map[key]["bidders"].append(r["bidder"])

        result = []
        for item in items_map.values():
            prices = item["prices"]
            n = len(prices)
            if n < 2:
                cv = None
                stdev = None
            else:
                mean  = statistics.mean(prices)
                stdev = statistics.stdev(prices)
                cv    = (stdev / mean * 100) if mean else None
            result.append({
                "item_number":   item["item_number"],
                "item_type":     item["item_type"],
                "specification": item["specification"],
                "size":          item["size"],
                "bid_count":     n,
                "min_price":     min(prices),
                "max_price":     max(prices),
                "mean_price":    round(statistics.mean(prices), 4) if prices else None,
                "stdev":         round(stdev, 4) if stdev is not None else None,
                "cv_pct":        round(cv, 1) if cv is not None else None,
                "bidders":       item["bidders"],
                "prices":        prices,
            })

        result.sort(key=lambda x: (x["cv_pct"] or 0), reverse=True)
        return jsonify({"rfq_id": rfq_id, "items": result})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/analysis/price-trends", methods=["GET"])
def price_trends():
    """
    GET params: item_type (optional), description_like (optional)
    Returns average unit_price per item_type per RFQ over time.
    """
    try:
        item_type = request.args.get("item_type", "")
        desc_like = request.args.get("description_like", "")

        where_clauses = ["b.unit_price IS NOT NULL", "b.unit_price > 0", "r.is_potential = 0"]
        if item_type:
            where_clauses.append(f"i.item_type = '{item_type.upper()}'")
        if desc_like:
            where_clauses.append(f"(i.specification LIKE '%{desc_like}%' OR i.item_type LIKE '%{desc_like}%')")

        where = " AND ".join(where_clauses)
        sql = f"""
            SELECT r.rfq_id, r.station, r.rfq_date,
                   i.item_type, i.specification, i.size, i.unit,
                   d.name        AS bidder,
                   b.unit_price,
                   b.ext_price,
                   i.quantity
            FROM rfq_items i
            JOIN rfqs    r ON r.rfq_id  = i.rfq_id
            JOIN bids    b ON b.item_id = i.id
            JOIN bidders d ON d.id      = b.bidder_id
            WHERE {where}
            ORDER BY r.rfq_date, i.item_type, i.specification
        """
        rows = rfq_db.run_query(sql, DB_PATH)
        return jsonify({"rows": rows, "count": len(rows)})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/analysis/bidder-patterns", methods=["GET"])
def bidder_patterns():
    """
    Returns how often each bidder is the lowest price, by item_type.
    """
    try:
        sql2 = """
            SELECT
                i.item_type,
                d.name AS bidder,
                COUNT(*) AS bid_count,
                AVG(b.unit_price) AS avg_unit_price,
                MIN(b.unit_price) AS min_unit_price
            FROM rfq_items i
            JOIN bids b    ON b.item_id = i.id
            JOIN bidders d ON d.id      = b.bidder_id
            WHERE b.unit_price IS NOT NULL AND b.unit_price > 0
            GROUP BY i.item_type, d.name
            ORDER BY i.item_type, avg_unit_price
        """
        rows = rfq_db.run_query(sql2, DB_PATH)
        return jsonify({"rows": rows})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/analysis/subset-enum/<rfq_id>  — split-award subset optimisation
# ---------------------------------------------------------------------------

@app.route("/api/analysis/subset-enum/<rfq_id>", methods=["GET"])
def subset_enum(rfq_id):
    """
    For each subset size k = 1 .. n_bidders, find the combination of k bidders
    that minimises total cost when every line item is awarded to the cheapest
    bidder in the subset.  Returns the best subset and cost for every k,
    plus a savings-vs-k=1 column so you can see the diminishing-returns curve.
    """
    try:
        from itertools import combinations

        # ── 1. Load all items for this RFQ ──────────────────────────────────
        items_sql = """
            SELECT id, item_number, item_type, specification, size, unit, quantity
            FROM rfq_items WHERE rfq_id = ?
            ORDER BY CAST(item_number AS REAL), item_number
        """.replace("?", f"'{rfq_id}'")
        items = rfq_db.run_query(items_sql, DB_PATH)
        if not items:
            return jsonify({"error": f"No items found for RFQ '{rfq_id}'"}), 404

        # ── 2. Load all bids ─────────────────────────────────────────────────
        bids_sql = """
            SELECT b.item_id, d.name AS bidder, b.unit_price, b.ext_price,
                   i.quantity
            FROM bids b
            JOIN bidders  d ON d.id      = b.bidder_id
            JOIN rfq_items i ON i.id     = b.item_id
            WHERE i.rfq_id = ?
              AND (b.unit_price IS NOT NULL OR b.ext_price IS NOT NULL)
        """.replace("?", f"'{rfq_id}'")
        bids = rfq_db.run_query(bids_sql, DB_PATH)

        # ── 3. Build lookup: item_id → {bidder: effective_cost} ─────────────
        #       effective_cost = ext_price if available, else unit_price * qty
        item_costs = {}   # {item_id: {bidder: cost}}
        for b in bids:
            iid = b["item_id"]
            if iid not in item_costs:
                item_costs[iid] = {}
            cost = b["ext_price"]
            if cost is None and b["unit_price"] is not None:
                qty = b["quantity"] or 1
                cost = b["unit_price"] * qty
            if cost is not None and cost >= 0:
                item_costs[iid][b["bidder"]] = cost

        all_bidders = sorted({b["bidder"] for b in bids})
        n = len(all_bidders)
        item_ids = [it["id"] for it in items]

        # ── 4. Enumerate every subset size k ─────────────────────────────────
        def eval_subset(bidder_set):
            """Return (total_cost, covered_count, uncovered_ids) for a bidder subset."""
            total = 0.0
            covered = 0
            uncovered = []
            for iid in item_ids:
                costs_here = {bd: item_costs[iid][bd]
                              for bd in bidder_set if iid in item_costs and bd in item_costs[iid]}
                if costs_here:
                    total += min(costs_here.values())
                    covered += 1
                else:
                    uncovered.append(iid)
            return total, covered, uncovered

        results = []
        baseline_cost = None   # cost for k=1 best single bidder

        for k in range(1, n + 1):
            best_cost     = None
            best_subset   = None
            best_covered  = 0
            best_uncovered = []

            for combo in combinations(all_bidders, k):
                cost, covered, uncovered = eval_subset(set(combo))
                if best_cost is None or cost < best_cost:
                    best_cost      = cost
                    best_subset    = list(combo)
                    best_covered   = covered
                    best_uncovered = uncovered

            if k == 1:
                baseline_cost = best_cost

            savings_vs_single = None
            if baseline_cost and baseline_cost > 0 and k > 1:
                savings_vs_single = round((baseline_cost - best_cost) / baseline_cost * 100, 2)
            savings_vs_prev = None
            if results and results[-1]["total_cost"] and best_cost is not None:
                savings_vs_prev = round(
                    (results[-1]["total_cost"] - best_cost) / results[-1]["total_cost"] * 100, 2
                )

            # Per-item breakdown for this best subset
            per_item = []
            for it in items:
                iid = it["id"]
                costs_here = {bd: item_costs[iid][bd]
                              for bd in best_subset if iid in item_costs and bd in item_costs[iid]}
                if costs_here:
                    winner     = min(costs_here, key=costs_here.get)
                    win_cost   = costs_here[winner]
                    all_quoted = {bd: round(costs_here[bd], 4) for bd in costs_here}
                else:
                    winner     = None
                    win_cost   = None
                    all_quoted = {}
                per_item.append({
                    "item_number":   it["item_number"],
                    "item_type":     it["item_type"],
                    "specification": it["specification"],
                    "size":          it["size"],
                    "unit":          it["unit"],
                    "quantity":      it["quantity"],
                    "awarded_to":    winner,
                    "awarded_cost":  round(win_cost, 4) if win_cost is not None else None,
                    "all_quotes":    all_quoted,
                    "covered":       winner is not None,
                })

            results.append({
                "k":                  k,
                "best_subset":        best_subset,
                "total_cost":         round(best_cost, 2) if best_cost is not None else None,
                "items_covered":      best_covered,
                "items_total":        len(item_ids),
                "uncovered_count":    len(best_uncovered),
                "savings_vs_k1_pct":  savings_vs_single,
                "savings_vs_prev_pct": savings_vs_prev,
                "per_item":           per_item,
            })

        return jsonify({
            "rfq_id":      rfq_id,
            "all_bidders": all_bidders,
            "item_count":  len(item_ids),
            "results":     results,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/analysis/estimate/<rfq_id>  — historical price estimation
# ---------------------------------------------------------------------------

def _tokenise_spec(spec):
    """
    Split a specification string into a normalised set of meaningful tokens.
    e.g. "SMLS, NPS 2, SCH XS/80 (0.218 WT), BARE, ASTM A106 B"
      -> {"SMLS","NPS","2","SCH","XS","80","0.218","WT","BARE","ASTM","A106","B"}
    """
    if not spec:
        return set()
    import re
    tokens = re.split(r"[\s,/\(\)\-]+", str(spec).upper())
    # Drop tokens that are too short or purely punctuation
    skip = {"AND", "OR", "THE", "TO", "OF", "IN", "WITH"}
    return {t for t in tokens if len(t) >= 2 and t not in skip}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@app.route("/api/analysis/estimate/<rfq_id>", methods=["GET"])
def estimate_rfq(rfq_id):
    """
    For each item in a potential RFQ, find historically-bid items with the same
    item_type and similar specification, then estimate likely pricing.

    Matching logic:
      1. Exact item_type match (required)
      2. Jaccard similarity on spec tokens
      3. Bonus +0.20 if sizes match
    Confidence: HIGH >= 0.55, MEDIUM >= 0.30, LOW < 0.30 (shown but flagged)

    Returns per-item estimates plus an overall RFQ cost estimate.
    """
    try:
        # ── 1. Verify this RFQ exists (ideally is_potential=1, but allow either) ──
        rfq_rows = rfq_db.run_query(
            f"SELECT * FROM rfqs WHERE rfq_id = '{rfq_id}'", DB_PATH
        )
        if not rfq_rows:
            return jsonify({"error": f"RFQ '{rfq_id}' not found"}), 404

        # ── 2. Load items for the target RFQ ─────────────────────────────────
        target_items = rfq_db.run_query(
            f"""SELECT id, item_number, item_type, specification, size, unit, quantity
                FROM rfq_items WHERE rfq_id = '{rfq_id}'
                ORDER BY CAST(item_number AS REAL), item_number""",
            DB_PATH
        )
        if not target_items:
            return jsonify({"error": f"No items found for RFQ '{rfq_id}'"}), 404

        # ── 3. Load all historical bid data (exclude this RFQ, exclude potential) ──
        hist_bids = rfq_db.run_query(
            f"""SELECT i.id        AS item_id,
                       i.item_type,
                       i.specification,
                       i.size,
                       i.unit,
                       i.quantity,
                       r.rfq_id,
                       r.rfq_date,
                       r.station,
                       d.name      AS bidder,
                       b.unit_price,
                       b.ext_price
                FROM rfq_items  i
                JOIN rfqs        r ON r.rfq_id     = i.rfq_id
                JOIN bids        b ON b.item_id    = i.id
                JOIN bidders     d ON d.id          = b.bidder_id
                WHERE r.rfq_id      != '{rfq_id}'
                  AND r.is_potential = 0
                  AND b.unit_price  IS NOT NULL
                  AND b.unit_price   > 0""",
            DB_PATH
        )

        # Group historical bids by item_id for fast lookup
        hist_by_type = {}   # {item_type: [bid_rows…]}
        for row in hist_bids:
            hist_by_type.setdefault(row["item_type"], []).append(row)

        # ── 4. Match and estimate each target item ───────────────────────────
        estimates = []
        total_low = total_mean = total_high = 0.0
        uncovered = 0

        for titem in target_items:
            ttype  = titem["item_type"]
            tspec  = titem["specification"] or ""
            tsize  = (titem["size"] or "").strip().upper()
            ttoks  = _tokenise_spec(tspec)

            candidates = hist_by_type.get(ttype, [])
            if not candidates:
                estimates.append({
                    "item_number":   titem["item_number"],
                    "item_type":     ttype,
                    "specification": tspec,
                    "size":          titem["size"],
                    "unit":          titem["unit"],
                    "quantity":      titem["quantity"],
                    "confidence":    "NONE",
                    "match_score":   0,
                    "matches":       [],
                    "est_unit_min":  None,
                    "est_unit_mean": None,
                    "est_unit_max":  None,
                    "est_ext_mean":  None,
                    "bidders_seen":  [],
                    "source_rfqs":   [],
                })
                uncovered += 1
                continue

            # Score every candidate bid row
            scored = []
            for c in candidates:
                ctoks = _tokenise_spec(c["specification"] or "")
                score = _jaccard(ttoks, ctoks)
                # Size match bonus
                csize = (c["size"] or "").strip().upper()
                if tsize and csize and tsize == csize:
                    score = min(1.0, score + 0.20)
                if score >= 0.25:
                    scored.append((score, c))

            if not scored:
                estimates.append({
                    "item_number":   titem["item_number"],
                    "item_type":     ttype,
                    "specification": tspec,
                    "size":          titem["size"],
                    "unit":          titem["unit"],
                    "quantity":      titem["quantity"],
                    "confidence":    "NONE",
                    "match_score":   0,
                    "matches":       [],
                    "est_unit_min":  None,
                    "est_unit_mean": None,
                    "est_unit_max":  None,
                    "est_ext_mean":  None,
                    "bidders_seen":  [],
                    "source_rfqs":   [],
                })
                uncovered += 1
                continue

            # Weight prices by match score, collect stats
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score  = scored[0][0]
            confidence  = "HIGH" if best_score >= 0.55 else \
                          "MEDIUM" if best_score >= 0.30 else "LOW"

            # Gather unit prices from all scored matches (weighted by score)
            prices_weighted = []  # [(unit_price, weight)]
            prices_raw      = []
            bidders_seen    = set()
            source_rfqs     = set()
            match_detail    = []

            seen_keys = set()  # deduplicate (rfq_id, bidder, spec) combos
            for score, c in scored:
                key = (c["rfq_id"], c["bidder"], c["specification"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                up = c["unit_price"]
                prices_weighted.append((up, score))
                prices_raw.append(up)
                bidders_seen.add(c["bidder"])
                source_rfqs.add(c["rfq_id"])
                match_detail.append({
                    "rfq_id":        c["rfq_id"],
                    "rfq_date":      c["rfq_date"],
                    "bidder":        c["bidder"],
                    "specification": c["specification"],
                    "size":          c["size"],
                    "unit_price":    round(up, 4),
                    "match_score":   round(score, 3),
                })

            if not prices_raw:
                uncovered += 1
                continue

            # Weighted mean price
            total_w  = sum(w for _, w in prices_weighted)
            wmean    = sum(p * w for p, w in prices_weighted) / total_w
            qty      = titem["quantity"] or 0

            est_min  = round(min(prices_raw), 4)
            est_mean = round(wmean, 4)
            est_max  = round(max(prices_raw), 4)
            est_ext  = round(wmean * qty, 2) if qty else None

            total_low  += est_min  * qty
            total_mean += wmean    * qty
            total_high += est_max  * qty

            estimates.append({
                "item_number":   titem["item_number"],
                "item_type":     ttype,
                "specification": tspec,
                "size":          titem["size"],
                "unit":          titem["unit"],
                "quantity":      qty,
                "confidence":    confidence,
                "match_score":   round(best_score, 3),
                "matches":       match_detail[:8],  # top 8 for display
                "est_unit_min":  est_min,
                "est_unit_mean": est_mean,
                "est_unit_max":  est_max,
                "est_ext_mean":  est_ext,
                "bidders_seen":  sorted(bidders_seen),
                "source_rfqs":   sorted(source_rfqs),
            })

        covered   = len(estimates) - uncovered
        conf_dist = {
            "HIGH":   sum(1 for e in estimates if e["confidence"] == "HIGH"),
            "MEDIUM": sum(1 for e in estimates if e["confidence"] == "MEDIUM"),
            "LOW":    sum(1 for e in estimates if e["confidence"] == "LOW"),
            "NONE":   uncovered,
        }

        return jsonify({
            "rfq_id":        rfq_id,
            "item_count":    len(estimates),
            "covered":       covered,
            "uncovered":     uncovered,
            "confidence_dist": conf_dist,
            "total_est_low":  round(total_low,  2),
            "total_est_mean": round(total_mean, 2),
            "total_est_high": round(total_high, 2),
            "estimates":     estimates,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/files  — list xlsx files in the base directory for the file picker
# ---------------------------------------------------------------------------

@app.route("/api/files", methods=["GET"])
def list_files():
    try:
        files = []
        for p in BASE_DIR.glob("*.xlsx"):
            files.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
        for p in BASE_DIR.glob("*.xls"):
            files.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
        files.sort(key=lambda f: f["name"])
        return jsonify(files)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# /api/config  — read/write API key
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
def get_config():
    has_key = bool(ANTHROPIC_API_KEY)
    return jsonify({"has_api_key": has_key})


@app.route("/api/config", methods=["POST"])
def set_config():
    global ANTHROPIC_API_KEY
    data = request.get_json()
    key  = data.get("api_key", "").strip()
    if key:
        ANTHROPIC_API_KEY = key
        return jsonify({"status": "ok", "has_api_key": True})
    return jsonify({"error": "api_key required"}), 400


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import webbrowser
    import threading

    port = 5050
    url  = f"http://localhost:{port}"

    def open_browser():
        import time; time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"\n  RFQ Bid Manager running at {url}\n  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=port, debug=False)
