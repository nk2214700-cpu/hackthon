import io
import re
from collections import defaultdict
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

REQUIRED_COLUMNS = {"date", "description", "amount"}
UPI_DETECT_COLUMNS = {"sender", "receiver"}
UNSUPPORTED_FILE_COLUMNS_MSG = (
    "Uploaded file is missing required columns. Expected either "
    "(Date, Description, Amount) or a supported UPI statement format."
)
PAREN_NAME_RE = re.compile(r"\(([^)]*)\)")

CATEGORY_KEYWORDS = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "food", "dominos", "starbucks"],
    "Bills": ["electricity", "bescom", "water", "gas", "internet", "airtel", "jio", "rent", "wifi"],
    "Entertainment": ["netflix", "movie", "pvr", "bookmyshow", "spotify", "prime", "hotstar", "sony liv"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "nykaa"],
}

SAMPLE_TRANSACTIONS = [
    {"date": "2026-07-03", "description": "UPI-SWIGGY-BANGALORE", "amount": 412.50},
    {"date": "2026-07-08", "description": "UPI-BESCOM-ELECTRICITY", "amount": 1850.00},
    {"date": "2026-07-12", "description": "UPI-AMAZON-SHOPPING", "amount": 1299.00},
    {"date": "2026-07-18", "description": "UPI-NETFLIX-SUBSCRIPTION", "amount": 649.00},
    {"date": "2026-07-22", "description": "UPI-ZOMATO-ORDER", "amount": 278.00},
    {"date": "2026-07-28", "description": "UPI-FLIPKART-ELECTRONICS", "amount": 4599.00},
    {"date": "2026-08-02", "description": "UPI-SWIGGY-BANGALORE", "amount": 365.00},
    {"date": "2026-08-06", "description": "UPI-AIRTEL-INTERNET", "amount": 999.00},
    {"date": "2026-08-11", "description": "UPI-PVR-MOVIE-TICKETS", "amount": 780.00},
    {"date": "2026-08-15", "description": "UPI-AMAZON-SHOPPING", "amount": 2140.00},
    {"date": "2026-08-19", "description": "UPI-ZOMATO-ORDER", "amount": 520.00},
    {"date": "2026-08-25", "description": "UPI-BESCOM-ELECTRICITY", "amount": 1720.00},
    {"date": "2026-09-01", "description": "UPI-NETFLIX-SUBSCRIPTION", "amount": 649.00},
    {"date": "2026-09-05", "description": "UPI-SWIGGY-BANGALORE", "amount": 890.00},
    {"date": "2026-09-09", "description": "UPI-FLIPKART-FASHION", "amount": 1890.00},
    {"date": "2026-09-14", "description": "UPI-STARBUCKS-CAFE", "amount": 340.00},
    {"date": "2026-09-18", "description": "UPI-RENT-APARTMENT", "amount": 18000.00},
    {"date": "2026-09-21", "description": "UPI-BOOKMYSHOW-MOVIE", "amount": 450.00},
    {"date": "2026-09-24", "description": "NEFT-SALARY-CREDIT-REFUND", "amount": 250.00},
    {"date": "2026-09-25", "description": "UPI-AMAZON-SHOPPING", "amount": 8750.00},
]


def categorize(description):
    text = str(description or "").lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


def normalize_date(value, dayfirst=False):
    if pd.isna(value):
        raise ValueError("date is missing in one or more rows")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=dayfirst)
    if pd.isna(parsed):
        raise ValueError(f"Could not parse date: {value!r}")
    return parsed.strftime("%Y-%m-%d")


def normalize_amount(value):
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Could not parse amount: {value!r}") from exc
    if pd.isna(amount):
        raise ValueError("amount is missing in one or more rows")
    return round(amount, 2)


def build_transaction(row, dayfirst=False):
    return {
        "date": normalize_date(row["date"], dayfirst=dayfirst),
        "description": str(row["description"]).strip(),
        "amount": normalize_amount(row["amount"]),
        "category": categorize(row["description"]),
    }


def _normalize_headers(df):
    df = df.copy()
    df.columns = [" ".join(str(col).strip().lower().split()) for col in df.columns]
    return df


def is_upi_statement(columns):
    cols = {str(col) for col in columns}
    compact = {col.replace(" ", "") for col in cols}
    return UPI_DETECT_COLUMNS.issubset(cols) and "dr/cr" in compact


def _column_named(columns, *candidates):
    compact_map = {col.replace(" ", ""): col for col in columns}
    for name in candidates:
        key = name.replace(" ", "")
        if key in compact_map:
            return compact_map[key]
    return None


def extract_merchant_name(receiver):
    if receiver is None or (isinstance(receiver, float) and pd.isna(receiver)):
        return "UPI Transaction"
    text = str(receiver).strip()
    if not text or text.lower() in {"nan", "none"}:
        return "UPI Transaction"
    matches = PAREN_NAME_RE.findall(text)
    if not matches:
        return "UPI Transaction"
    name = matches[-1].strip()
    if not name or not re.search(r"[A-Za-z0-9]", name):
        return "UPI Transaction"
    return name


def _cell_text(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def map_upi_statement(df):
    date_col = _column_named(df.columns, "date")
    receiver_col = _column_named(df.columns, "receiver")
    dr_col = _column_named(df.columns, "dr/cr")
    status_col = _column_named(df.columns, "status")
    amount_col = _column_named(df.columns, "amount (in rs.)", "amount (in rs)")
    if amount_col is None:
        for col in df.columns:
            compact = col.replace(" ", "")
            if compact.startswith("amount") and "rs" in compact:
                amount_col = col
                break
    if not all([date_col, receiver_col, dr_col, status_col, amount_col]):
        raise ValueError(UNSUPPORTED_FILE_COLUMNS_MSG)

    mapped = []
    for _, row in df.iterrows():
        if _cell_text(row[dr_col]).upper() != "DR":
            continue
        if _cell_text(row[status_col]).upper() != "SUCCESS":
            continue
        mapped.append(
            {
                "date": row[date_col],
                "description": extract_merchant_name(row[receiver_col]),
                "amount": row[amount_col],
            }
        )
    return mapped


def load_sample_transactions():
    return [build_transaction(row) for row in SAMPLE_TRANSACTIONS]


transactions = load_sample_transactions()
budget = {}
BUDGET_CATEGORIES = ("Bills", "Food", "Shopping", "Entertainment", "Other")


def parse_upload(file_storage):
    filename = (file_storage.filename or "").strip().lower()
    if not filename:
        raise ValueError("No file selected. Please upload a CSV or Excel (.xlsx) file.")

    raw = file_storage.read()
    if not raw:
        raise ValueError("Uploaded file is empty.")

    buffer = io.BytesIO(raw)
    if filename.endswith(".csv"):
        df = pd.read_csv(buffer)
    elif filename.endswith(".xlsx"):
        df = pd.read_excel(buffer, engine="openpyxl")
    else:
        raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")

    df = _normalize_headers(df)
    upi_format = is_upi_statement(df.columns)
    if upi_format:
        source_rows = map_upi_statement(df)
        dayfirst = True
    else:
        missing = REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(UNSUPPORTED_FILE_COLUMNS_MSG)
        source_rows = df.to_dict(orient="records")
        dayfirst = False

    parsed = []
    for index, row in enumerate(source_rows):
        try:
            parsed.append(build_transaction(row, dayfirst=dayfirst))
        except ValueError as exc:
            raise ValueError(f"Row {index + 2}: {exc}") from exc
    return parsed


def filter_by_month(rows, month):
    if not month:
        return list(rows)
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise ValueError("Invalid month. Use YYYY-MM, for example 2026-09.") from exc
    return [row for row in rows if row["date"].startswith(month)]


def month_key(date_str):
    return date_str[:7]


def spend_by_category(rows):
    totals = defaultdict(float)
    for row in rows:
        totals[row["category"]] += row["amount"]
    return {category: round(total, 2) for category, total in sorted(totals.items())}


def monthly_trend(rows):
    totals = defaultdict(float)
    for row in rows:
        totals[month_key(row["date"])] += row["amount"]
    return [
        {"month": month, "total": round(totals[month], 2)}
        for month in sorted(totals)
    ]


def predicted_next_month(rows):
    monthly_by_category = defaultdict(lambda: defaultdict(float))
    months = set()
    for row in rows:
        month = month_key(row["date"])
        months.add(month)
        monthly_by_category[row["category"]][month] += row["amount"]

    month_count = len(months) or 1
    categories = set(CATEGORY_KEYWORDS) | {"Other"} | set(monthly_by_category)
    return {
        category: round(sum(monthly_by_category[category].values()) / month_count, 2)
        for category in sorted(categories)
    }


def find_anomalies(filtered_rows, all_rows):
    category_amounts = defaultdict(list)
    for row in all_rows:
        category_amounts[row["category"]].append(row["amount"])

    averages = {
        category: (sum(amounts) / len(amounts))
        for category, amounts in category_amounts.items()
        if amounts
    }

    anomalies = []
    for row in filtered_rows:
        average = averages.get(row["category"], 0)
        if average and row["amount"] > 1.5 * average:
            anomalies.append(
                {
                    **row,
                    "category_average": round(average, 2),
                    "threshold": round(1.5 * average, 2),
                }
            )
    return anomalies


def budget_vs_actual(actual_by_category):
    comparison = {}
    for category, budget_amount in budget.items():
        actual = round(float(actual_by_category.get(category, 0)), 2)
        comparison[category] = {
            "budget": budget_amount,
            "actual": actual,
            "status": "over" if actual > budget_amount else "under",
        }
    return comparison


@app.route("/")
def homepage():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part named 'file' in the request."}), 400

    try:
        parsed = parse_upload(request.files["file"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not parse uploaded file: {exc}"}), 400

    global transactions
    transactions = parsed
    return jsonify(transactions)


@app.route("/api/budget", methods=["GET", "POST"])
def budget_endpoint():
    global budget
    if request.method == "GET":
        return jsonify(budget)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be JSON object of category budgets."}), 400

    parsed = {}
    for category in BUDGET_CATEGORIES:
        if category not in payload:
            continue
        try:
            amount = float(payload[category])
        except (TypeError, ValueError):
            return jsonify({"error": f"Budget for {category} must be a number."}), 400
        if amount < 0:
            return jsonify({"error": f"Budget for {category} cannot be negative."}), 400
        parsed[category] = round(amount, 2)

    budget = parsed
    return jsonify(budget)


@app.route("/api/summary", methods=["GET"])
def summary():
    month = request.args.get("month")
    try:
        filtered = filter_by_month(transactions, month)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    actual = spend_by_category(filtered)
    return jsonify(
        {
            "month": month,
            "transaction_count": len(filtered),
            "total_spend": round(sum(row["amount"] for row in filtered), 2),
            "spend_by_category": actual,
            "monthly_trend": monthly_trend(transactions),
            "predicted_next_month": predicted_next_month(transactions),
            "anomalies": find_anomalies(filtered, transactions),
            "budget_vs_actual": budget_vs_actual(actual),
        }
    )


if __name__ == "__main__":
    app.run(debug=True)
