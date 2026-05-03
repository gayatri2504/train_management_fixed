# Train Management System (Python + MySQL)

This is my mini railway reservation project built using Python and MySQL, then extended into a web version for demo and deployment practice.

I first started with a terminal-based booking flow, and later converted it into a browser app with login, booking, cancellation, and printable tickets.

## What this project does

- Login-based access for booking operations
- View train routes and seat availability
- Book AC/GEN tickets
- Waiting list support when seats are full
- Cancel tickets and auto-promote from waiting list
- Separate **Booking Counter** page (modular UI idea)
- Printable ticket page
- Status highlighting:
  - Confirmed -> Green
  - Waiting List -> Yellow
  - Cancelled -> Red

## Tech stack

- Python 3
- MySQL
- `mysql-connector-python`
- Built-in `http.server` for web handling
- HTML/CSS/JS rendered from Python

## Project structure

- `web_app.py` - Main web application
- `main.py` - Entry point used for deployment
- `sql_connector.py` - Terminal/CLI version
- `Railway Mnagement.sql` - Database schema + seed data
- `requirements.txt` - Python dependencies
- `Procfile`, `railway.toml` - Railway deployment config

## Run locally

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Make sure MySQL is running.
3. Import SQL file into your MySQL database.
4. Set environment variables if needed:
   - `MYSQL_HOST`
   - `MYSQL_PORT`
   - `MYSQL_USER`
   - `MYSQL_PASSWORD`
   - `MYSQL_DATABASE`
5. Run:
   ```bash
   python web_app.py
   ```
6. Open:
   - `http://127.0.0.1:4000/login`

## Deployment notes

This project is configured for Railway.

- Start command:
  - `python main.py`
- Health check path:
  - `/health`

The app supports both Railway-style DB variable names:
- `MYSQLHOST`, `MYSQLPORT`, `MYSQLUSER`, `MYSQLPASSWORD`, `MYSQLDATABASE`

and underscore-style names:
- `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`

## What I learned

- Handling real MySQL connection issues during setup
- Difference between local DB and cloud DB environment variables
- Debugging deployment logs and fixing startup/build issues
- Building a basic modular web flow from a terminal-first project

## Future improvements

- Admin panel for train/day management
- Better validation and user error messages
- Search and filter in ticket history
- Role-based login (admin/counter staff)
- Move HTML templates into separate files

---

This project was built as a practical learning demo to understand full flow:
**database setup -> backend logic -> UI -> deployment**.

## Author

**TAMMISETTI SRI VENKATA SAI GAYATRI** developed this project.
