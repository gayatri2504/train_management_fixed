import getpass
import hashlib
import hmac
import os
import secrets
import time
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse

import mysql.connector
from mysql.connector import Error


DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "user": os.getenv("MYSQL_USER", "root"),
    "database": os.getenv("MYSQL_DATABASE", "railway_management_system"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
}
WAITING_LIST_LIMIT = 2
APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "")
APP_DEV_PASSWORD = os.getenv("APP_DEV_PASSWORD", "admin123")
DEMO_DB_PASSWORD = os.getenv("DEMO_DB_PASSWORD", "Bannu@123")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
BOOKING_COUNTER_ROUTE = "/booking-counter"
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", os.getenv("APP_PORT", "4000")))
SERVER_PASSWORD = None
SESSIONS = {}


def get_db_password():
    global SERVER_PASSWORD
    if SERVER_PASSWORD is None:
        SERVER_PASSWORD = os.getenv("MYSQL_PASSWORD", "").strip()
        if not SERVER_PASSWORD and DEMO_DB_PASSWORD:
            # Demo fallback to avoid background-server prompts that can hang requests.
            SERVER_PASSWORD = DEMO_DB_PASSWORD
        if not SERVER_PASSWORD:
            try:
                SERVER_PASSWORD = getpass.getpass("MySQL root password: ")
            except Exception:
                SERVER_PASSWORD = ""
        if not SERVER_PASSWORD:
            raise RuntimeError("MYSQL_PASSWORD is not set and no password was entered.")
    return SERVER_PASSWORD


def hash_password(password, salt=None, iterations=260000):
    safe_salt = salt or secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        safe_salt.encode("utf-8"),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${safe_salt}${pwd_hash}"


def verify_password(password, stored_hash):
    try:
        algorithm, iterations_text, salt, expected_hash = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
    except (ValueError, AttributeError):
        return False

    candidate_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(candidate_hash, expected_hash)


def get_password_hash():
    if APP_PASSWORD_HASH:
        return APP_PASSWORD_HASH
    # Local-development fallback: set APP_PASSWORD_HASH in production.
    return hash_password(APP_DEV_PASSWORD)


def connect_db():
    return mysql.connector.connect(password=get_db_password(), **DB_CONFIG)


def fetch_all(query, params=None):
    connection = connect_db()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(query, params or ())
        return cursor.fetchall()
    finally:
        cursor.close()
        connection.close()


def fetch_one(query, params=None):
    rows = fetch_all(query, params)
    return rows[0] if rows else None


def execute_write(query, params=None):
    connection = connect_db()
    cursor = connection.cursor()
    try:
        cursor.execute(query, params or ())
        connection.commit()
        return cursor.lastrowid
    except Error:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def update_train_status(train_number, booking_date, category, available_delta, booked_delta):
    execute_write(
        f"""
        UPDATE train_status
        SET {category}_seats_available = {category}_seats_available + %s,
            {category}_seats_booked = {category}_seats_booked + %s
        WHERE trainNumber = %s AND train_date = %s
        """,
        (available_delta, booked_delta, train_number, booking_date),
    )


def get_dashboard_data():
    trains = fetch_all(
        """
        SELECT
            tl.trainNumber,
            tl.trainName,
            tl.train_source,
            tl.train_destination,
            tl.AC_ticket_fair,
            tl.GEN_ticket_fair,
            ad.day_available,
            ad.date_available,
            ts.AC_seats_available,
            ts.GEN_seats_available
        FROM trainList tl
        LEFT JOIN available_days ad ON ad.trainNumber = tl.trainNumber
        LEFT JOIN train_status ts
            ON ts.trainNumber = ad.trainNumber
           AND ts.train_date = ad.date_available
        ORDER BY tl.trainNumber, ad.date_available
        """
    )
    recent_tickets = fetch_all(
        """
        SELECT
            ticket_id,
            trainNumber,
            Booking_Date,
            passenger_name,
            ticket_status,
            category
        FROM passenger
        ORDER BY ticket_id DESC
        LIMIT 10
        """
    )
    return trains, recent_tickets


def get_train_date_options():
    rows = fetch_all(
        """
        SELECT
            tl.trainNumber,
            tl.trainName,
            ad.day_available,
            ad.date_available
        FROM trainList tl
        JOIN available_days ad ON ad.trainNumber = tl.trainNumber
        ORDER BY tl.trainNumber, ad.date_available
        """
    )
    options = []
    for row in rows:
        date_text = row["date_available"].strftime("%Y-%m-%d")
        options.append(
            {
                "value": f'{row["trainNumber"]}|{date_text}',
                "label": (
                    f'{row["trainNumber"]} - {row["trainName"].strip()} - '
                    f'{row["day_available"]} {date_text}'
                ),
            }
        )
    return options


def get_ticket_details(ticket_id):
    return fetch_one(
        """
        SELECT
            p.ticket_id,
            p.trainNumber,
            p.Booking_Date,
            p.passenger_name,
            p.age,
            p.sex,
            p.address,
            p.ticket_status,
            p.category,
            tl.trainName,
            tl.train_source,
            tl.train_destination,
            tl.AC_ticket_fair,
            tl.GEN_ticket_fair
        FROM passenger p
        JOIN trainList tl ON tl.trainNumber = p.trainNumber
        WHERE p.ticket_id = %s
        """,
        (ticket_id,),
    )


def get_waiting_list_count(train_number, booking_date, category):
    row = fetch_one(
        """
        SELECT COUNT(ticket_id) AS total
        FROM passenger
        WHERE trainNumber = %s
          AND booking_date = %s
          AND category = %s
          AND LOWER(ticket_status) = 'waiting list'
        """,
        (train_number, booking_date, category),
    )
    return row["total"] if row else 0


def get_seat_availability(train_number, booking_date, category):
    row = fetch_one(
        f"""
        SELECT {category}_seats_available AS seats
        FROM train_status
        WHERE trainNumber = %s AND train_date = %s
        """,
        (train_number, booking_date),
    )
    return None if row is None else row["seats"]


def insert_passenger(train_number, booking_date, category, ticket_status, details):
    return execute_write(
        """
        INSERT INTO passenger
            (trainNumber, Booking_Date, passenger_name, age, sex, address, ticket_status, category)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            train_number,
            booking_date,
            details["name"],
            details["age"],
            details["gender"],
            details["address"],
            ticket_status,
            category,
        ),
    )


def promote_waiting_ticket(train_number, booking_date, category):
    waiting_ticket = fetch_one(
        """
        SELECT ticket_id
        FROM passenger
        WHERE trainNumber = %s
          AND booking_date = %s
          AND category = %s
          AND LOWER(ticket_status) = 'waiting list'
        ORDER BY ticket_id
        LIMIT 1
        """,
        (train_number, booking_date, category),
    )
    if waiting_ticket is None:
        return None

    execute_write(
        "UPDATE passenger SET ticket_status = 'Confirmed' WHERE ticket_id = %s",
        (waiting_ticket["ticket_id"],),
    )
    update_train_status(train_number, booking_date, category, -1, 1)
    return waiting_ticket["ticket_id"]


def book_ticket(form_data):
    errors = []
    selection = form_data.get("train_date", "").strip()
    category = form_data.get("category", "").strip().upper()
    name = form_data.get("name", "").strip()
    gender = form_data.get("gender", "").strip()
    address = form_data.get("address", "").strip()
    age_text = form_data.get("age", "").strip()

    if "|" not in selection:
        errors.append("Choose a train and travel date.")
        train_number = booking_date = None
    else:
        train_number, booking_date = selection.split("|", 1)

    if category not in {"AC", "GEN"}:
        errors.append("Choose AC or GEN.")

    if not name:
        errors.append("Passenger name is required.")

    if not age_text.isdigit() or int(age_text) <= 0:
        errors.append("Age must be a positive number.")
    else:
        age = int(age_text)

    valid_option = None
    if "|" in selection:
        valid_option = fetch_one(
            """
            SELECT 1 AS valid_option
            FROM available_days
            WHERE trainNumber = %s AND date_available = %s
            """,
            (train_number, booking_date),
        )
    if "|" in selection and valid_option is None:
        errors.append("The selected train/date combination is not available.")

    if errors:
        return False, " ".join(errors)

    details = {
        "name": name,
        "age": age,
        "gender": gender,
        "address": address,
    }

    try:
        seats_available = get_seat_availability(train_number, booking_date, category)
        if seats_available is None:
            return False, "Could not find seat details for that booking.", None

        if seats_available > 0:
            ticket_id = insert_passenger(train_number, booking_date, category, "Confirmed", details)
            update_train_status(train_number, booking_date, category, -1, 1)
            return True, f"Ticket booked successfully. Ticket ID: {ticket_id}", ticket_id

        waiting_count = get_waiting_list_count(train_number, booking_date, category)
        if waiting_count >= WAITING_LIST_LIMIT:
            return False, "Confirmed seats are full and the waiting list is also full.", None

        ticket_id = insert_passenger(train_number, booking_date, category, "Waiting List", details)
        return True, f"No confirmed seats left. Added to waiting list with Ticket ID: {ticket_id}", ticket_id
    except Error as exc:
        return False, f"Booking failed: {exc}", None


def cancel_ticket(form_data):
    ticket_id = form_data.get("ticket_id", "").strip()
    if not ticket_id.isdigit():
        return False, "Ticket ID must be a number."

    ticket = fetch_one(
        """
        SELECT ticket_id, trainNumber, Booking_Date, ticket_status, category
        FROM passenger
        WHERE ticket_id = %s
        """,
        (ticket_id,),
    )
    if ticket is None:
        return False, "Ticket not found."

    try:
        execute_write("DELETE FROM passenger WHERE ticket_id = %s", (ticket_id,))
        if ticket["ticket_status"].strip().lower() == "confirmed":
            update_train_status(ticket["trainNumber"], ticket["Booking_Date"], ticket["category"], 1, -1)
            promoted_ticket = promote_waiting_ticket(
                ticket["trainNumber"], ticket["Booking_Date"], ticket["category"]
            )
            if promoted_ticket is not None:
                return True, (
                    f"Ticket {ticket_id} cancelled. Waiting list ticket {promoted_ticket} is now confirmed."
                )
        return True, f"Ticket {ticket_id} cancelled successfully."
    except Error as exc:
        return False, f"Cancellation failed: {exc}"


def calculate_stats(trains, tickets):
    unique_trains = {}
    total_ac = 0
    total_gen = 0
    upcoming_slots = 0

    for row in trains:
        unique_trains[row["trainNumber"]] = row
        if row["date_available"] is not None:
            upcoming_slots += 1
        total_ac += int(row["AC_seats_available"] or 0)
        total_gen += int(row["GEN_seats_available"] or 0)

    return {
        "train_count": len(unique_trains),
        "slot_count": upcoming_slots,
        "recent_tickets": len(tickets),
        "available_seats": total_ac + total_gen,
    }


def format_message(message, level):
    if not message:
        return ""
    return f'<div class="notice notice-{level}">{escape(message)}</div>'


def render_nav(active_page):
    links = [
        ("/", "Dashboard", "dashboard"),
        (BOOKING_COUNTER_ROUTE, "Booking Counter", "booking"),
        ("/logout", "Log out", "logout"),
    ]
    link_markup = []
    for href, label, key in links:
        class_name = "nav-link"
        if active_page == key:
            class_name += " active"
        if key == "logout":
            class_name += " nav-logout"
        link_markup.append(f'<a class="{class_name}" href="{href}">{escape(label)}</a>')
    return f'<nav class="top-nav card">{"".join(link_markup)}</nav>'


def render_forwarding_page(target):
    safe_target = escape(target)
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="0; url={safe_target}">
    <title>Redirecting</title>
</head>
<body>
    <script>window.location.replace("{safe_target}");</script>
    <p>Redirecting to <a href="{safe_target}">{safe_target}</a>...</p>
</body>
</html>"""


def render_page(title, body, extra_head=""):
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
        :root {{
            --bg: #f5f7fb;
            --panel: #ffffff;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #dbe3ef;
            --accent: #2563eb;
            --accent-dark: #1d4ed8;
            --danger: #dc2626;
            --success-bg: #e8f7ee;
            --error-bg: #fdecec;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            color: var(--ink);
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: var(--bg);
        }}
        a {{ color: inherit; }}
        .shell {{
            width: min(1180px, calc(100% - 32px));
            margin: 24px auto 40px;
        }}
        .card {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 16px;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
        }}
        .notice {{
            margin: 16px 0 0;
            padding: 14px 16px;
            border-radius: 12px;
            font-weight: 600;
            border: 1px solid var(--line);
        }}
        .notice-success {{
            background: var(--success-bg);
            color: #166534;
        }}
        .notice-error {{
            background: var(--error-bg);
            color: #991b1b;
        }}
        .top-nav {{
            display: flex;
            gap: 10px;
            padding: 12px;
            margin-bottom: 16px;
            align-items: center;
        }}
        .nav-link {{
            text-decoration: none;
            color: var(--muted);
            padding: 9px 14px;
            border-radius: 10px;
            font-weight: 500;
        }}
        .nav-link.active {{
            background: #eff6ff;
            color: var(--accent);
        }}
        .nav-link.nav-logout {{
            margin-left: auto;
        }}
        .label-chip {{
            display: inline-flex;
            align-items: center;
            padding: 6px 12px;
            border-radius: 999px;
            border: 1px solid #dbeafe;
            background: #eff6ff;
            color: var(--accent);
            font-size: 0.86rem;
            font-weight: 600;
        }}
        input, select, button {{
            width: 100%;
            border-radius: 10px;
            border: 1px solid var(--line);
            padding: 11px 12px;
            font: inherit;
        }}
        input, select {{
            color: var(--muted);
            color: var(--ink);
            background: #fff;
        }}
        button {{
            border: none;
            cursor: pointer;
            color: white;
            background: var(--accent);
            font-weight: 600;
        }}
        button:hover {{
            background: var(--accent-dark);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: #fff;
        }}
        th, td {{
            text-align: left;
            padding: 12px 10px;
            border-bottom: 1px solid #eef2f7;
        }}
        th {{
            color: var(--muted);
            font-size: 0.84rem;
            background: #f8fafc;
        }}
        .loading-screen {{
            position: fixed;
            inset: 0;
            display: grid;
            place-items: center;
            background: rgba(245, 247, 251, 0.95);
            z-index: 9999;
            transition: opacity 0.35s ease;
        }}
        .loading-screen.hidden {{
            opacity: 0;
            pointer-events: none;
        }}
        .signal-shell {{
            display: grid;
            gap: 14px;
            justify-items: center;
        }}
        .signal-title {{
            color: #334155;
            font-weight: 700;
            letter-spacing: 0.02em;
            font-size: 0.95rem;
            text-transform: uppercase;
        }}
        .signal-body {{
            width: 74px;
            padding: 12px 10px;
            border-radius: 18px;
            background: #1f2937;
            border: 3px solid #111827;
            display: grid;
            gap: 10px;
            justify-items: center;
            box-shadow: 0 14px 28px rgba(15, 23, 42, 0.28);
        }}
        .signal-light {{
            width: 42px;
            height: 42px;
            border-radius: 50%;
            border: 2px solid rgba(255, 255, 255, 0.2);
            opacity: 0.25;
        }}
        .signal-red {{
            background: #ef4444;
            animation: redPulse 1.1s infinite;
        }}
        .signal-yellow {{
            background: #f59e0b;
            animation: yellowPulse 1.1s infinite;
        }}
        .signal-green {{
            background: #22c55e;
            animation: greenPulse 1.1s infinite;
        }}
        @keyframes redPulse {{
            0%, 60%, 100% {{ opacity: 0.2; box-shadow: none; }}
            15%, 45% {{ opacity: 1; box-shadow: 0 0 16px rgba(239, 68, 68, 0.7); }}
        }}
        @keyframes yellowPulse {{
            0%, 15%, 100% {{ opacity: 0.2; box-shadow: none; }}
            30%, 60% {{ opacity: 1; box-shadow: 0 0 16px rgba(245, 158, 11, 0.7); }}
        }}
        @keyframes greenPulse {{
            0%, 30%, 100% {{ opacity: 0.2; box-shadow: none; }}
            45%, 75% {{ opacity: 1; box-shadow: 0 0 16px rgba(34, 197, 94, 0.7); }}
        }}
        {extra_head}
    </style>
</head>
<body>
    <div id="loading-screen" class="loading-screen">
        <div class="signal-shell">
            <span class="signal-title">Loading</span>
            <div class="signal-body" aria-hidden="true">
                <span class="signal-light signal-red"></span>
                <span class="signal-light signal-yellow"></span>
                <span class="signal-light signal-green"></span>
            </div>
        </div>
    </div>
    {body}
    <script>
        window.addEventListener("load", function () {{
            var loader = document.getElementById("loading-screen");
            if (!loader) return;
            setTimeout(function () {{
                loader.classList.add("hidden");
                setTimeout(function () {{
                    loader.remove();
                }}, 420);
            }}, 700);
        }});
    </script>
</body>
</html>"""


def render_login(message="", level="error"):
    body = f"""
    <main class="shell login-shell">
        <section class="login-card card">
            <div class="login-card-inner">
                    <span class="label-chip">Railway Login</span>
                    <h1>Sign in</h1>
                    <p>Enter your app credentials to open the railway dashboard.</p>
                    {format_message(message, level)}
                    <form method="post" action="/login" class="login-form">
                        <label>Username
                            <input name="username" placeholder="admin" autocomplete="username" required>
                        </label>
                        <label>Password
                            <input name="password" type="password" placeholder="admin123" autocomplete="current-password" required>
                        </label>
                        <button type="submit">Enter Dashboard</button>
                    </form>
                    <p class="hint">Use your configured login credentials.</p>
            </div>
            </div>
        </section>
    </main>
    """
    extra_head = """
        .login-shell {
            display: grid;
            place-items: center;
            min-height: calc(100vh - 64px);
        }
        .login-card {
            width: min(420px, 100%);
            padding: 28px;
        }
        .login-card-inner {
            width: 100%;
        }
        .login-card h1 {
            margin: 16px 0 10px;
            font-size: 2rem;
        }
        .login-card p {
            color: var(--muted);
            line-height: 1.6;
        }
        .login-form {
            display: grid;
            gap: 14px;
            margin-top: 18px;
        }
        .login-form label {
            display: grid;
            gap: 8px;
            color: var(--muted);
        }
        .hint {
            margin-top: 16px;
            font-size: 0.92rem;
        }
    """
    return render_page("Railway Login", body, extra_head)


def render_dashboard(username, message="", level="success"):
    trains, recent_tickets = get_dashboard_data()
    stats = calculate_stats(trains, recent_tickets)

    grouped = {}
    for row in trains:
        grouped.setdefault(row["trainNumber"], []).append(row)

    train_cards = []
    for train_number, items in grouped.items():
        first = items[0]
        rows = []
        for item in items:
            if item["date_available"] is None:
                continue
            rows.append(
                """
                <tr>
                    <td>{day}</td>
                    <td>{date}</td>
                    <td><span class="seat seat-ac">{ac}</span></td>
                    <td><span class="seat seat-gen">{gen}</span></td>
                </tr>
                """.format(
                    day=escape(str(item["day_available"])),
                    date=escape(item["date_available"].strftime("%Y-%m-%d")),
                    ac=escape(str(item["AC_seats_available"])),
                    gen=escape(str(item["GEN_seats_available"])),
                )
            )
        train_cards.append(
            """
            <article class="route-card">
                <div class="route-top">
                    <div>
                        <span class="route-id">Train {number}</span>
                        <h3>{name}</h3>
                        <p>{source} to {destination}</p>
                    </div>
                    <div class="fare-stack">
                        <span>AC {ac_fare}</span>
                        <span>GEN {gen_fare}</span>
                    </div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Day</th>
                            <th>Date</th>
                            <th>AC</th>
                            <th>GEN</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>
            </article>
            """.format(
                number=escape(str(train_number)),
                name=escape(first["trainName"].strip()),
                source=escape(first["train_source"].strip()),
                destination=escape(first["train_destination"].strip()),
                ac_fare=escape(str(first["AC_ticket_fair"])),
                gen_fare=escape(str(first["GEN_ticket_fair"])),
                rows="".join(rows) or "<tr><td colspan='4'>No schedules found.</td></tr>",
            )
        )

    ticket_rows = []
    for ticket in recent_tickets:
        status_text = ticket["ticket_status"].strip()
        status_key = status_text.lower()
        status_class = "status-default"
        if "confirm" in status_key:
            status_class = "status-confirmed"
        elif "waiting" in status_key:
            status_class = "status-waiting"
        elif "cancel" in status_key or "canceld" in status_key:
            status_class = "status-cancelled"

        ticket_rows.append(
            """
            <tr>
                <td>#{ticket_id}</td>
                <td>{train}</td>
                <td>{date}</td>
                <td>{name}</td>
                <td>{category}</td>
                <td><span class="status-badge {status_class}">{status}</span></td>
                <td><a class="table-link" href="/ticket?ticket_id={ticket_id}">Print</a></td>
            </tr>
            """.format(
                ticket_id=escape(str(ticket["ticket_id"])),
                train=escape(str(ticket["trainNumber"])),
                date=escape(ticket["Booking_Date"].strftime("%Y-%m-%d")),
                name=escape(ticket["passenger_name"].strip()),
                category=escape(ticket["category"]),
                status=escape(status_text),
                status_class=status_class,
            )
        )

    body = f"""
    <main class="shell dashboard-shell">
        {render_nav("dashboard")}
        <section class="hero card">
            <div class="hero-copy">
                <span class="label-chip">Signed in as {escape(username)}</span>
                <h1>Railway Dashboard</h1>
                <p>Manage bookings, check seats, and cancel tickets from one simple page.</p>
            </div>
        </section>
        {format_message(message, level)}
        <section class="stats-row">
            <article class="stat-card card">
                <span class="stat-label">Active Trains</span>
                <strong>{stats["train_count"]}</strong>
            </article>
            <article class="stat-card card">
                <span class="stat-label">Travel Slots</span>
                <strong>{stats["slot_count"]}</strong>
            </article>
            <article class="stat-card card">
                <span class="stat-label">Recent Tickets</span>
                <strong>{stats["recent_tickets"]}</strong>
            </article>
            <article class="stat-card card">
                <span class="stat-label">Open Seats</span>
                <strong>{stats["available_seats"]}</strong>
            </article>
        </section>
        <section class="dashboard-grid">
            <section class="card main-panel">
                <div class="panel-head">
                    <div>
                        <h2>Train schedule</h2>
                    </div>
                    <p>Sample records still use the original 2021 dates from the project dump.</p>
                </div>
                <div class="route-grid">
                    {''.join(train_cards)}
                </div>
            </section>
            <aside class="side-stack">
                <section class="card form-panel">
                    <div class="panel-head">
                        <div>
                            <h2>Booking counter</h2>
                        </div>
                    </div>
                    <p class="panel-copy">Use the dedicated booking counter page for new reservations and a more expandable ticket workflow.</p>
                    <a class="primary-link" href="{BOOKING_COUNTER_ROUTE}">Open Booking Counter</a>
                </section>
                <section class="card form-panel">
                    <div class="panel-head">
                        <div>
                            <h2>Cancel ticket</h2>
                        </div>
                    </div>
                    <form method="post" action="/cancel" class="form-grid">
                        <label>Ticket ID
                            <input name="ticket_id" type="number" min="1" placeholder="3" required>
                        </label>
                        <button class="danger-button" type="submit">Cancel Ticket</button>
                    </form>
                </section>
                <section class="card ticket-panel">
                    <div class="panel-head">
                        <div>
                            <h2>Recent tickets</h2>
                        </div>
                    </div>
                    <div class="ticket-table">
                        <table>
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Train</th>
                                    <th>Date</th>
                                    <th>Name</th>
                                    <th>Class</th>
                                    <th>Status</th>
                                    <th>Ticket</th>
                                </tr>
                            </thead>
                            <tbody>
                                {''.join(ticket_rows) or "<tr><td colspan='7'>No tickets found yet.</td></tr>"}
                            </tbody>
                        </table>
                    </div>
                </section>
            </aside>
        </section>
    </main>
    """
    extra_head = """
        .dashboard-shell {
            display: grid;
            gap: 18px;
        }
        .hero {
            display: flex;
            justify-content: space-between;
            gap: 18px;
            align-items: center;
            padding: 24px;
        }
        .hero h1 {
            margin: 14px 0 8px;
            font-size: 2rem;
        }
        .hero p {
            margin: 0;
            max-width: 58ch;
            color: var(--muted);
            line-height: 1.6;
        }
        .ghost-link {
            display: inline-flex;
            padding: 10px 14px;
            border-radius: 999px;
            text-decoration: none;
            border: 1px solid var(--line);
            background: #fff;
            color: var(--muted);
        }
        .panel-copy {
            margin: 0 0 16px;
            color: var(--muted);
            line-height: 1.6;
        }
        .table-link {
            color: var(--accent);
            text-decoration: none;
            font-weight: 600;
        }
        .primary-link {
            display: inline-block;
            text-decoration: none;
            background: var(--accent);
            color: #fff;
            padding: 11px 14px;
            border-radius: 10px;
            font-weight: 600;
        }
        .stats-row {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
        }
        .stat-card {
            padding: 18px 20px;
        }
        .stat-label {
            display: block;
            color: var(--muted);
            margin-bottom: 10px;
            font-size: 0.78rem;
        }
        .stat-card strong {
            font-size: 1.8rem;
        }
        .dashboard-grid {
            display: grid;
            grid-template-columns: 1.15fr 0.85fr;
            gap: 18px;
        }
        .main-panel, .form-panel, .ticket-panel {
            padding: 24px;
        }
        .panel-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            margin-bottom: 18px;
        }
        .panel-head h2 {
            margin: 0;
            font-size: 1.35rem;
        }
        .panel-head p {
            margin: 0;
            max-width: 28ch;
            color: var(--muted);
            line-height: 1.55;
            text-align: right;
        }
        .route-grid {
            display: grid;
            gap: 14px;
        }
        .route-card {
            padding: 18px;
            border-radius: 12px;
            background: #f9fbfd;
            border: 1px solid #e5ebf3;
        }
        .route-top {
            display: flex;
            justify-content: space-between;
            gap: 18px;
            margin-bottom: 16px;
        }
        .route-id {
            display: inline-block;
            color: var(--accent);
            margin-bottom: 8px;
            font-size: 0.85rem;
            font-weight: 600;
        }
        .route-top h3 {
            margin: 0 0 8px;
            font-size: 1.15rem;
        }
        .route-top p {
            margin: 0;
            color: var(--muted);
        }
        .fare-stack {
            display: grid;
            gap: 8px;
            align-content: start;
            min-width: 110px;
        }
        .fare-stack span {
            text-align: center;
            border-radius: 999px;
            padding: 8px 12px;
            background: #eef4ff;
            color: var(--accent);
            font-weight: 600;
        }
        .side-stack {
            display: grid;
            gap: 18px;
        }
        .form-grid {
            display: grid;
            gap: 14px;
        }
        .form-grid label {
            display: grid;
            gap: 8px;
            color: var(--muted);
        }
        .split {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }
        .danger-button {
            background: var(--danger);
        }
        .ticket-table {
            overflow-x: auto;
        }
        .seat, .status-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 44px;
            padding: 7px 10px;
            border-radius: 999px;
            font-weight: 600;
        }
        .seat-ac {
            background: #e8efff;
            color: #1d4ed8;
        }
        .seat-gen {
            background: #e7f8f1;
            color: #0f766e;
        }
        .status-badge {
            background: #f3f4f6;
            color: #374151;
        }
        .status-confirmed {
            background: #dcfce7;
            color: #166534;
        }
        .status-waiting {
            background: #fef3c7;
            color: #92400e;
        }
        .status-cancelled {
            background: #fee2e2;
            color: #991b1b;
        }
        @media (max-width: 1080px) {
            .stats-row {
                grid-template-columns: repeat(2, 1fr);
            }
            .dashboard-grid {
                grid-template-columns: 1fr;
            }
        }
        @media (max-width: 720px) {
            .shell {
                width: min(100%, calc(100% - 18px));
            }
            .hero {
                flex-direction: column;
            }
            .stats-row {
                grid-template-columns: 1fr;
            }
            .split {
                grid-template-columns: 1fr;
            }
            .route-top, .panel-head {
                flex-direction: column;
                align-items: flex-start;
            }
            .panel-head p {
                text-align: left;
            }
        }
    """
    return render_page("Railway Control Center", body, extra_head)


def render_ticket_page(username, ticket, message="", level="success"):
    fare_value = ticket["AC_ticket_fair"] if ticket["category"] == "AC" else ticket["GEN_ticket_fair"]
    show_confirmation_train = level == "success" and "ticket booked successfully" in message.lower()
    train_animation_markup = """
            <div class="ticket-train-rail no-print" id="ticket-train-rail" aria-hidden="true">
                <div class="ticket-train-track"></div>
                <div class="ticket-train-runner">🚂🚃🚃🚃</div>
            </div>
    """ if show_confirmation_train else ""
    body = """
    <main class="shell dashboard-shell">
        {nav}
        {message_block}
        <section class="card ticket-print-shell">
                <div class="ticket-topbar">
                <div>
                    <span class="label-chip">Signed in as {username}</span>
                    <h1>Printable Ticket</h1>
                    <p>Open, review, and print this ticket from the browser.</p>
                </div>
                <div class="ticket-actions no-print">
                    <a class="secondary-link" href="{booking_route}">Back to Booking Counter</a>
                    <button type="button" onclick="window.print()">Print Ticket</button>
                </div>
                </div>
                {train_animation}
            <article class="ticket-sheet">
                <div class="ticket-sheet-head">
                    <div>
                        <span class="ticket-kicker">Railway Reservation Ticket</span>
                        <h2>{train_name}</h2>
                        <p>{source} to {destination}</p>
                    </div>
                    <div class="ticket-id-box">
                        <span>Ticket ID</span>
                        <strong>#{ticket_id}</strong>
                    </div>
                </div>
                <div class="ticket-grid">
                    <div class="ticket-cell">
                        <span>Train Number</span>
                        <strong>{train_number}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Travel Date</span>
                        <strong>{booking_date}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Passenger</span>
                        <strong>{passenger_name}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Age</span>
                        <strong>{age}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Gender</span>
                        <strong>{gender}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Class</span>
                        <strong>{category}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Status</span>
                        <strong>{ticket_status}</strong>
                    </div>
                    <div class="ticket-cell">
                        <span>Fare</span>
                        <strong>{fare}</strong>
                    </div>
                </div>
                <div class="ticket-address">
                    <span>Address</span>
                    <strong>{address}</strong>
                </div>
            </article>
        </section>
    </main>
    """.format(
        nav=render_nav("booking"),
        message_block=format_message(message, level),
        train_animation=train_animation_markup,
        username=escape(username),
        booking_route=BOOKING_COUNTER_ROUTE,
        train_name=escape(ticket["trainName"].strip()),
        source=escape(ticket["train_source"].strip()),
        destination=escape(ticket["train_destination"].strip()),
        ticket_id=escape(str(ticket["ticket_id"])),
        train_number=escape(str(ticket["trainNumber"])),
        booking_date=escape(ticket["Booking_Date"].strftime("%Y-%m-%d")),
        passenger_name=escape(ticket["passenger_name"].strip()),
        age=escape(str(ticket["age"])),
        gender=escape((ticket["sex"] or "").strip() or "-"),
        category=escape(ticket["category"]),
        ticket_status=escape(ticket["ticket_status"].strip()),
        fare=escape(str(fare_value)),
        address=escape((ticket["address"] or "").strip() or "-"),
    )
    extra_head = """
        .ticket-print-shell {
            padding: 24px;
            display: grid;
            gap: 20px;
        }
        .ticket-topbar {
            display: flex;
            justify-content: space-between;
            gap: 18px;
            align-items: flex-start;
        }
        .ticket-topbar h1 {
            margin: 12px 0 8px;
            font-size: 2rem;
        }
        .ticket-topbar p {
            margin: 0;
            color: var(--muted);
            line-height: 1.6;
        }
        .ticket-actions {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        .secondary-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 11px 14px;
            border-radius: 10px;
            border: 1px solid var(--line);
            background: #fff;
            color: var(--ink);
            text-decoration: none;
            font-weight: 600;
        }
        .ticket-sheet {
            border: 1px dashed #93c5fd;
            border-radius: 18px;
            padding: 24px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
        }
        .ticket-sheet-head {
            display: flex;
            justify-content: space-between;
            gap: 18px;
            padding-bottom: 18px;
            border-bottom: 1px solid #dbeafe;
            margin-bottom: 18px;
        }
        .ticket-kicker {
            display: inline-block;
            color: var(--accent);
            font-weight: 700;
            font-size: 0.84rem;
            letter-spacing: 0.02em;
            text-transform: uppercase;
        }
        .ticket-sheet-head h2 {
            margin: 10px 0 8px;
            font-size: 1.6rem;
        }
        .ticket-sheet-head p {
            margin: 0;
            color: var(--muted);
        }
        .ticket-id-box {
            min-width: 140px;
            padding: 14px;
            border-radius: 14px;
            background: #eff6ff;
            text-align: center;
        }
        .ticket-id-box span,
        .ticket-cell span,
        .ticket-address span {
            display: block;
            color: var(--muted);
            font-size: 0.85rem;
            margin-bottom: 6px;
        }
        .ticket-id-box strong {
            font-size: 1.4rem;
        }
        .ticket-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
        }
        .ticket-cell,
        .ticket-address {
            padding: 14px;
            border-radius: 14px;
            background: #fff;
            border: 1px solid #e5eefb;
        }
        .ticket-address {
            margin-top: 14px;
        }
        .ticket-train-rail {
            width: 100%;
            margin: 4px 0 10px;
            position: relative;
            height: 44px;
            overflow: hidden;
            border-radius: 12px;
            background: #f1f5f9;
            border: 1px solid #dbeafe;
        }
        .ticket-train-track {
            position: absolute;
            left: 10px;
            right: 10px;
            top: 25px;
            height: 6px;
            border-radius: 999px;
            background: linear-gradient(90deg, #94a3b8 0%, #64748b 100%);
        }
        .ticket-train-runner {
            position: absolute;
            right: -220px;
            top: 4px;
            font-size: 1.75rem;
            line-height: 1;
            animation: ticketTrainRide 6s linear 1;
            will-change: transform;
            white-space: nowrap;
        }
        @keyframes ticketTrainRide {
            0% { transform: translateX(0); }
            100% { transform: translateX(calc(-100vw - 420px)); }
        }
        @media (max-width: 900px) {
            .ticket-grid {
                grid-template-columns: repeat(2, 1fr);
            }
            .ticket-topbar,
            .ticket-sheet-head {
                flex-direction: column;
            }
        }
        @media (max-width: 640px) {
            .ticket-grid {
                grid-template-columns: 1fr;
            }
        }
        @media print {
            body {
                background: #fff;
            }
            .top-nav,
            .no-print,
            .notice {
                display: none !important;
            }
            .shell {
                width: 100%;
                margin: 0;
            }
            .ticket-print-shell {
                border: none;
                padding: 0;
            }
            .ticket-sheet {
                border: 1px solid #d1d5db;
                box-shadow: none;
            }
        }
    """
    if show_confirmation_train:
        body += """
        <script>
            window.addEventListener("load", function () {
                var rail = document.getElementById("ticket-train-rail");
                if (!rail) return;
                setTimeout(function () {
                    rail.style.transition = "opacity 0.4s ease";
                    rail.style.opacity = "0";
                    setTimeout(function () { rail.remove(); }, 420);
                }, 6200);
            });
        </script>
        """
    return render_page("Printable Ticket", body, extra_head)


def render_booking_counter(username, message="", level="success"):
    options = get_train_date_options()
    option_markup = "".join(
        f'<option value="{escape(option["value"])}">{escape(option["label"])}</option>'
        for option in options
    )

    body = f"""
    <main class="shell dashboard-shell">
        {render_nav("booking")}
        <section class="card simple-counter-header">
            <span class="label-chip">Signed in as {escape(username)}</span>
            <h1>Booking Counter</h1>
            <p>Create new reservations from this separate counter page as the application grows into multiple modules.</p>
        </section>
        {format_message(message, level)}
        <section class="card simple-counter-card">
            <form method="post" action="/book" class="simple-counter-form" id="simple-counter-form" autocomplete="off">
                <div class="simple-row">
                    <label for="train_date">Train and Date</label>
                    <select id="train_date" name="train_date" required>
                        <option value="">Choose a route and date</option>
                        {option_markup}
                    </select>
                </div>
                <div class="simple-row">
                    <label for="category">Category</label>
                    <select id="category" name="category" required>
                        <option value="">Select category</option>
                        <option value="AC">AC</option>
                        <option value="GEN">GEN</option>
                    </select>
                </div>
                <div class="simple-row">
                    <label for="name">Passenger Name</label>
                    <input id="name" name="name" value="" autocomplete="new-password" autocapitalize="words" spellcheck="false" required>
                </div>
                <div class="simple-inline">
                    <div class="simple-row">
                        <label for="age">Age</label>
                        <input id="age" name="age" type="number" min="1" value="" autocomplete="new-password" required>
                    </div>
                    <div class="simple-row">
                        <label for="gender">Gender</label>
                        <input id="gender" name="gender" value="" autocomplete="new-password">
                    </div>
                </div>
                <div class="simple-row">
                    <label for="address">Address</label>
                    <input id="address" name="address" value="" autocomplete="new-password">
                </div>
                <div class="simple-actions">
                    <button type="reset">Clear</button>
                    <button type="submit">Confirm Booking</button>
                </div>
            </form>
        </section>
    </main>
    """
    extra_head = """
        .simple-counter-header,
        .simple-counter-card {
            padding: 20px;
        }
        .simple-counter-header {
            max-width: 760px;
            display: grid;
            gap: 10px;
            align-content: start;
        }
        .simple-counter-header h1 {
            margin: 0;
            font-size: 1.8rem;
        }
        .simple-counter-header p {
            margin: 0;
            color: var(--muted);
            line-height: 1.6;
            max-width: 60ch;
        }
        .simple-counter-card {
            max-width: 760px;
        }
        .simple-counter-form {
            display: grid;
            gap: 16px;
        }
        .simple-row {
            display: grid;
            gap: 8px;
        }
        .simple-row label {
            font-weight: 600;
            color: var(--ink);
        }
        .simple-inline {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }
        .simple-actions {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
        }
        .simple-actions button[type="reset"] {
            background: #e5e7eb;
            color: #111827;
        }
        @media (max-width: 720px) {
            .simple-inline {
                grid-template-columns: 1fr;
            }
            .simple-actions {
                flex-direction: column;
            }
        }
    """
    extra_head += """
        .simple-counter-card input:-webkit-autofill,
        .simple-counter-card input:-webkit-autofill:hover,
        .simple-counter-card input:-webkit-autofill:focus {
            -webkit-text-fill-color: var(--ink);
            transition: background-color 99999s ease-in-out 0s;
        }
    """
    body += """
    <script>
        function wipeCounterFields() {
            const form = document.getElementById("simple-counter-form");
            if (!form) return;
            form.reset();
            ["name", "age", "gender", "address"].forEach((id) => {
                const field = document.getElementById(id);
                if (field) {
                    field.value = "";
                    field.defaultValue = "";
                    field.setAttribute("value", "");
                }
            });
        }
        window.addEventListener("load", () => {
            wipeCounterFields();
            setTimeout(wipeCounterFields, 50);
            setTimeout(wipeCounterFields, 250);
        });
        window.addEventListener("pageshow", wipeCounterFields);
    </script>
    """
    return render_page("Booking Counter", body, extra_head)


class RailwayHandler(BaseHTTPRequestHandler):
    def prune_expired_sessions(self):
        now = int(time.time())
        expired_ids = [
            sid
            for sid, session_data in SESSIONS.items()
            if not isinstance(session_data, dict) or session_data.get("expires_at", 0) <= now
        ]
        for sid in expired_ids:
            SESSIONS.pop(sid, None)

    def build_session_cookie(self, session_id, max_age):
        secure_flag = "; Secure" if COOKIE_SECURE else ""
        return (
            f"session_id={session_id}; HttpOnly; Path=/; SameSite=Strict; Max-Age={max_age}{secure_flag}"
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/health":
            self.respond_html("ok")
            return

        if route == "/logout":
            self.handle_logout()
            return

        if route == "/login":
            query = parse_qs(parsed.query)
            message = unquote_plus(query.get("message", [""])[0])
            level = query.get("level", ["error"])[0]
            self.respond_html(render_login(message, level))
            return

        if route == "/ticket":
            session_user = self.require_auth()
            if session_user is None:
                return
            query = parse_qs(parsed.query)
            ticket_id = query.get("ticket_id", [""])[0]
            message = unquote_plus(query.get("message", [""])[0])
            level = query.get("level", ["success"])[0]
            if not ticket_id.isdigit():
                self.redirect_with_message("Ticket ID must be a number.", "error")
                return
            ticket = get_ticket_details(ticket_id)
            if ticket is None:
                self.redirect_with_message("Ticket not found.", "error")
                return
            self.respond_html(render_ticket_page(session_user, ticket, message, level))
            return

        if route in {BOOKING_COUNTER_ROUTE, "/booking-counter-fresh", "/booking-desk"}:
            session_user = self.require_auth()
            if session_user is None:
                return
            query = parse_qs(parsed.query)
            message = unquote_plus(query.get("message", [""])[0])
            level = query.get("level", ["success"])[0]
            self.respond_html(render_booking_counter(session_user, message, level))
            return

        if route != "/":
            self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
            return

        session_user = self.require_auth()
        if session_user is None:
            return

        query = parse_qs(parsed.query)
        message = unquote_plus(query.get("message", [""])[0])
        level = query.get("level", ["success"])[0]
        self.respond_html(render_dashboard(session_user, message, level))

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = {key: values[0] for key, values in parse_qs(raw_body).items()}

        if route == "/login":
            self.handle_login(fields)
            return

        session_user = self.require_auth()
        if session_user is None:
            return

        if route == "/book":
            success, message, ticket_id = book_ticket(fields)
            if success and ticket_id is not None:
                self.redirect(
                    f"/ticket?ticket_id={quote_plus(str(ticket_id))}&message={quote_plus(message)}&level=success"
                )
                return
            self.redirect_with_message(message, "success" if success else "error", BOOKING_COUNTER_ROUTE)
            return

        if route == "/cancel":
            success, message = cancel_ticket(fields)
            self.redirect_with_message(message, "success" if success else "error")
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Page not found")

    def handle_login(self, fields):
        username = fields.get("username", "").strip()
        password = fields.get("password", "")
        self.prune_expired_sessions()
        if username == APP_USERNAME and verify_password(password, get_password_hash()):
            session_id = secrets.token_urlsafe(24)
            now = int(time.time())
            SESSIONS[session_id] = {
                "username": username,
                "created_at": now,
                "last_seen": now,
                "expires_at": now + SESSION_TTL_SECONDS,
            }
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", self.build_session_cookie(session_id, SESSION_TTL_SECONDS))
            self.end_headers()
            return
        self.redirect("/login?message=Invalid+username+or+password.&level=error")

    def handle_logout(self):
        cookies = self.get_cookies()
        session_id = cookies.get("session_id")
        if session_id:
            SESSIONS.pop(session_id.value, None)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login?message=Logged+out+successfully.&level=success")
        self.send_header("Set-Cookie", self.build_session_cookie("", 0))
        self.end_headers()

    def require_auth(self):
        self.prune_expired_sessions()
        cookies = self.get_cookies()
        session_cookie = cookies.get("session_id")
        if session_cookie is None:
            self.redirect("/login?message=Please+log+in+to+continue.&level=error")
            return None

        session_data = SESSIONS.get(session_cookie.value)
        if not isinstance(session_data, dict):
            self.redirect("/login?message=Your+session+expired.+Log+in+again.&level=error")
            return None

        now = int(time.time())
        if session_data.get("expires_at", 0) <= now:
            SESSIONS.pop(session_cookie.value, None)
            self.redirect("/login?message=Your+session+expired.+Log+in+again.&level=error")
            return None

        # Sliding expiry.
        session_data["last_seen"] = now
        session_data["expires_at"] = now + SESSION_TTL_SECONDS
        return session_data.get("username")

    def get_cookies(self):
        cookies = SimpleCookie()
        raw_cookie = self.headers.get("Cookie")
        if raw_cookie:
            cookies.load(raw_cookie)
        return cookies

    def redirect(self, location):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def redirect_with_message(self, message, level, target="/"):
        location = f"{target}?message={quote_plus(message)}&level={quote_plus(level)}"
        self.redirect(location)

    def respond_html(self, html_text):
        data = html_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Clear-Site-Data", "\"cache\", \"storage\"")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format_string, *args):
        return


def main():
    print(f"Starting Railway Control Center on http://{APP_HOST}:{APP_PORT}")
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), RailwayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
