import base64
import mimetypes
import os
import secrets
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, abort
import anthropic

load_dotenv()

ROOT = Path(__file__).parent
SAMPLE_STATEMENT_PATH = ROOT / "sample_data" / "sample_statement.txt"
MODEL = "claude-sonnet-5"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 30000
ALLOWED_EXTENSIONS = {".pdf", ".csv", ".txt", ".png", ".jpg", ".jpeg"}
SHARE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days

DATABASE_URL = os.environ.get("DATABASE_URL")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

_client = None
# In-memory fallback for family-share links when no DATABASE_URL is configured.
# Used for local dev only; resets on restart.
_shares = {}


def get_db_connection():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_connection()
    if conn is None:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                review_data JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS shares (
                token TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                review_data JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    conn.close()


def save_review(result):
    conn = get_db_connection()
    if conn is None:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reviews (source, review_data) VALUES (%s, %s)",
            (result.get("source", ""), psycopg2.extras.Json(result)),
        )
    conn.close()


init_db()


def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def missing_api_key_response():
    return (
        jsonify(
            error=(
                "ANTHROPIC_API_KEY is not set on the server. Add it to a .env file "
                "(see .env.example) and restart the server."
            )
        ),
        500,
    )


REVIEW_SYSTEM_PROMPT = (
    "You are Verifi, a calm and careful financial assistant that reviews bank "
    "statements for people who are financially inexperienced, elderly, or have "
    "reading difficulties. Read the provided statement content and call the "
    "submit_review tool with your findings. Use plain, simple English with no "
    "banking jargon. Be specific about dollar amounts and dates when flagging "
    "something. Pay close attention to signs of scams: unfamiliar merchants, "
    "duplicate charges, sudden large withdrawals or wire transfers, and new "
    "recurring subscriptions."
)

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit the structured review of a bank statement.",
    "input_schema": {
        "type": "object",
        "properties": {
            "moneyIn": {
                "type": "number",
                "description": "Total money that came in during the period, in whole dollars.",
            },
            "moneyOut": {
                "type": "number",
                "description": "Total money spent during the period, in whole dollars.",
            },
            "leftOver": {
                "type": "number",
                "description": "moneyIn minus moneyOut, in whole dollars.",
            },
            "explanation": {
                "type": "string",
                "description": (
                    "A 3-5 sentence plain-English summary of what happened this "
                    "period and what needs attention first. No jargon."
                ),
            },
            "alerts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "1-5 short, specific, plain-English things the person should "
                    "check, ordered by importance. Empty array if nothing stands out."
                ),
            },
        },
        "required": ["moneyIn", "moneyOut", "leftOver", "explanation", "alerts"],
    },
}

TERMS_SYSTEM_PROMPT = (
    "You are Verifi, a consumer-protection assistant that reviews subscription "
    "or service terms and conditions for people who are financially "
    "inexperienced, elderly, or have reading difficulties. Read the provided "
    "terms text and call the submit_terms_analysis tool. Flag things like "
    "automatic renewals, cancellation or early-termination fees, price-increase "
    "clauses, trial-to-paid conversions, hidden fees, and unusual data-sharing "
    "clauses. Use plain, simple, reassuring English. If nothing concerning is "
    "found, say so plainly and return an empty warnings list."
)

TERMS_TOOL = {
    "name": "submit_terms_analysis",
    "description": "Submit the structured analysis of terms and conditions text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One calm, plain-English sentence giving the overall takeaway.",
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short plain-English warnings, one per issue found. Empty if none.",
            },
        },
        "required": ["summary", "warnings"],
    },
}


def call_tool(system_prompt, tool, content_blocks):
    client = get_client()
    if client is None:
        return None, missing_api_key_response()

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": content_blocks}],
        )
    except anthropic.APIError as exc:
        return None, (jsonify(error=f"AI request failed: {exc}"), 502)

    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input, None

    return None, (jsonify(error="AI response did not include the expected result."), 502)


def content_blocks_for_file(filename, data):
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None, f"Unsupported file type: {ext or 'unknown'}"

    if ext in (".txt", ".csv"):
        text = data.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
        return [
            {"type": "text", "text": f"Bank statement file: {filename}\n\n{text}"}
        ], None

    if ext == ".pdf":
        encoded = base64.standard_b64encode(data).decode("ascii")
        return [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": encoded,
                },
            },
            {"type": "text", "text": f"This is the bank statement file: {filename}"},
        ], None

    media_type = mimetypes.guess_type(filename)[0] or "image/png"
    encoded = base64.standard_b64encode(data).decode("ascii")
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": encoded},
        },
        {"type": "text", "text": f"This is a photo/scan of the bank statement file: {filename}"},
    ], None


@app.route("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.route("/styles.css")
def styles():
    return send_from_directory(ROOT, "styles.css")


@app.route("/script.js")
def script():
    return send_from_directory(ROOT, "script.js")


@app.route("/api/review/sample", methods=["POST"])
def review_sample():
    text = SAMPLE_STATEMENT_PATH.read_text()
    content_blocks = [
        {"type": "text", "text": f"Bank statement file: sample_statement.txt\n\n{text}"}
    ]
    result, error_response = call_tool(REVIEW_SYSTEM_PROMPT, REVIEW_TOOL, content_blocks)
    if error_response:
        return error_response
    result["source"] = "Sample July bank statement"
    save_review(result)
    return jsonify(result)


@app.route("/api/review", methods=["POST"])
def review_upload():
    if "statement" not in request.files:
        return jsonify(error="No file was uploaded."), 400

    file = request.files["statement"]
    if not file.filename:
        return jsonify(error="No file was uploaded."), 400

    data = file.read()
    if not data:
        return jsonify(error="The uploaded file is empty."), 400

    content_blocks, error = content_blocks_for_file(file.filename, data)
    if error:
        return jsonify(error=error), 400

    result, error_response = call_tool(REVIEW_SYSTEM_PROMPT, REVIEW_TOOL, content_blocks)
    if error_response:
        return error_response
    result["source"] = file.filename
    save_review(result)
    return jsonify(result)


@app.route("/api/reviews", methods=["GET"])
def list_reviews():
    conn = get_db_connection()
    if conn is None:
        return jsonify(error="No database configured."), 500

    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, source, review_data, created_at FROM reviews ORDER BY created_at DESC LIMIT 100"
        )
        rows = cur.fetchall()
    conn.close()

    reviews = [
        {"id": r[0], "source": r[1], "review": r[2], "created_at": r[3].isoformat()}
        for r in rows
    ]
    return jsonify(reviews=reviews)


@app.route("/api/terms-check", methods=["POST"])
def terms_check():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify(error="Please paste the terms first."), 400

    text = text[:MAX_TEXT_CHARS]
    content_blocks = [{"type": "text", "text": text}]
    result, error_response = call_tool(TERMS_SYSTEM_PROMPT, TERMS_TOOL, content_blocks)
    if error_response:
        return error_response
    return jsonify(result)


@app.route("/api/share", methods=["POST"])
def create_share():
    payload = request.get_json(silent=True) or {}
    review = payload.get("review")
    name = str(payload.get("name", "")).strip()

    if not review:
        return jsonify(error="No review to share yet. Review a statement first."), 400
    if not name:
        return jsonify(error="Please add the helper's name."), 400

    token = secrets.token_urlsafe(16)
    conn = get_db_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO shares (token, name, review_data) VALUES (%s, %s, %s)",
                (token, name, psycopg2.extras.Json(review)),
            )
        conn.close()
    else:
        now = time.time()
        _shares_gc(now)
        _shares[token] = {"review": review, "name": name, "created_at": now}

    url = request.host_url.rstrip("/") + f"/share/{token}"
    return jsonify(token=token, url=url)


def _shares_gc(now):
    expired = [t for t, s in _shares.items() if now - s["created_at"] > SHARE_TTL_SECONDS]
    for t in expired:
        del _shares[t]


def get_share(token):
    conn = get_db_connection()
    if conn is not None:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, review_data FROM shares WHERE token = %s", (token,)
            )
            row = cur.fetchone()
        conn.close()
        if row is None:
            return None
        return {"name": row[0], "review": row[1]}

    _shares_gc(time.time())
    return _shares.get(token)


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@app.route("/share/<token>")
def view_share(token):
    share = get_share(token)
    if not share:
        return (
            "<!doctype html><html><head><link rel='stylesheet' href='/styles.css'>"
            "<title>Verifi - Link expired</title></head><body><main class='app-shell'>"
            "<div class='review-area'><h2>This link is no longer valid</h2>"
            "<p>Ask your family member to send a new share link.</p></div>"
            "</main></body></html>"
        ), 404

    review = share["review"]
    alerts_html = "".join(f"<li>{_escape(a)}</li>" for a in review.get("alerts", []))
    if not alerts_html:
        alerts_html = "<li>No alerts were flagged.</li>"

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Verifi - Shared review</title>
  <link rel="stylesheet" href="/styles.css" />
</head>
<body>
  <header class="topbar">
    <div>
      <p class="eyebrow">Verifi &middot; Read-only</p>
      <h1>Shared statement review for {_escape(share['name'])}</h1>
    </div>
  </header>
  <main class="app-shell">
    <section class="review-area">
      <div class="summary-cards">
        <article><p>Money in</p><strong>${_escape(review.get('moneyIn', 0))}</strong></article>
        <article><p>Money out</p><strong>${_escape(review.get('moneyOut', 0))}</strong></article>
        <article><p>Left over</p><strong>${_escape(review.get('leftOver', 0))}</strong></article>
      </div>
      <div class="plain-language">
        <h3>Plain-English explanation</h3>
        <p>{_escape(review.get('explanation', ''))}</p>
      </div>
      <div class="alerts-panel">
        <h3>Things to check</h3>
        <ul>{alerts_html}</ul>
      </div>
      <p><em>This is a read-only view shared by a family member. You cannot move money from this page.</em></p>
    </section>
  </main>
</body>
</html>"""
    return html


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=True)
