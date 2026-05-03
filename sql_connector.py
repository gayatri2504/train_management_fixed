import getpass
import os
from datetime import datetime

import mysql.connector
from mysql.connector import Error


DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "database": "railway_management_system",
}
WAITING_LIST_LIMIT = 2


def connect_db():
    password = os.getenv("MYSQL_PASSWORD")
    if password is None:
        password = getpass.getpass("MySQL root password: ")
    try:
        connection = mysql.connector.connect(password=password, **DB_CONFIG)
        print("Connected successfully.")
        return connection
    except Error as exc:
        print(f"Database connection failed: {exc}")
        return None


def fetch_all(connection, query, params=None):
    cursor = connection.cursor()
    try:
        cursor.execute(query, params or ())
        return cursor.fetchall()
    finally:
        cursor.close()


def fetch_one(connection, query, params=None):
    rows = fetch_all(connection, query, params)
    return rows[0] if rows else None


def execute_write(connection, query, params=None):
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


def list_trains(connection):
    trains = fetch_all(
        connection,
        """
        SELECT trainNumber, trainName, train_source, train_destination
        FROM trainList
        ORDER BY trainNumber
        """,
    )
    print("Trains Available")
    for train_number, name, source, destination in trains:
        print(f"{train_number} {name.strip()} ({source.strip()} -> {destination.strip()})")
    return {str(train[0]) for train in trains}


def list_available_dates(connection, train_number):
    dates = fetch_all(
        connection,
        """
        SELECT day_available, date_available
        FROM available_days
        WHERE trainNumber = %s
        ORDER BY date_available
        """,
        (train_number,),
    )
    if not dates:
        print("No travel dates found for this train.")
        return []

    print("Available Dates")
    valid_dates = []
    for day_name, travel_date in dates:
        date_text = travel_date.strftime("%Y-%m-%d")
        print(f"{train_number} {day_name} {date_text}")
        valid_dates.append(date_text)
    return valid_dates


def get_valid_train_number(valid_train_numbers):
    while True:
        train_number = input("Train Number: ").strip()
        if train_number in valid_train_numbers:
            return train_number
        print("Invalid train number. Please choose one from the list above.")


def get_valid_booking_date(valid_dates):
    while True:
        booking_date = input("Please enter the booking date exactly as shown (YYYY-MM-DD): ").strip()
        try:
            normalized = datetime.strptime(booking_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print("Invalid date format. Use YYYY-MM-DD.")
            continue

        if normalized in valid_dates:
            return normalized
        print("That date is not available for the selected train.")


def get_valid_category():
    while True:
        category = input("AC or GEN: ").strip().upper()
        if category in {"AC", "GEN"}:
            return category
        print("Invalid category. Enter AC or GEN.")


def get_passenger_details():
    name = input("Name: ").strip()
    while not name:
        print("Name cannot be empty.")
        name = input("Name: ").strip()

    while True:
        age_text = input("Age: ").strip()
        try:
            age = int(age_text)
        except ValueError:
            print("Age must be a number.")
            continue
        if age <= 0:
            print("Age must be greater than 0.")
            continue
        break

    gender = input("Gender: ").strip()
    address = input("Address: ").strip()
    return name, age, gender, address


def get_seat_availability(connection, train_number, booking_date, category):
    row = fetch_one(
        connection,
        f"""
        SELECT {category}_seats_available
        FROM train_status
        WHERE trainNumber = %s AND train_date = %s
        """,
        (train_number, booking_date),
    )
    if row is None:
        return None
    seats = row[0]
    print(f"Seats Available for train {train_number} on {booking_date}: {seats}")
    return seats


def get_waiting_list_count(connection, train_number, booking_date, category):
    row = fetch_one(
        connection,
        """
        SELECT COUNT(ticket_id)
        FROM passenger
        WHERE trainNumber = %s
          AND booking_date = %s
          AND category = %s
          AND LOWER(ticket_status) = 'waiting list'
        """,
        (train_number, booking_date, category),
    )
    return row[0] if row else 0


def insert_passenger(connection, train_number, booking_date, category, ticket_status, details):
    name, age, gender, address = details
    return execute_write(
        connection,
        """
        INSERT INTO passenger
            (trainNumber, Booking_Date, passenger_name, age, sex, address, ticket_status, category)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (train_number, booking_date, name, age, gender, address, ticket_status, category),
    )


def update_train_status(connection, train_number, booking_date, category, available_delta, booked_delta):
    execute_write(
        connection,
        f"""
        UPDATE train_status
        SET {category}_seats_available = {category}_seats_available + %s,
            {category}_seats_booked = {category}_seats_booked + %s
        WHERE trainNumber = %s AND train_date = %s
        """,
        (available_delta, booked_delta, train_number, booking_date),
    )


def promote_waiting_ticket(connection, train_number, booking_date, category):
    waiting_ticket = fetch_one(
        connection,
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
        return

    execute_write(
        connection,
        """
        UPDATE passenger
        SET ticket_status = 'Confirmed'
        WHERE ticket_id = %s
        """,
        (waiting_ticket[0],),
    )
    update_train_status(connection, train_number, booking_date, category, -1, 1)
    print(f"Waiting list ticket {waiting_ticket[0]} moved to Confirmed.")


def book_ticket(connection):
    valid_train_numbers = list_trains(connection)
    train_number = get_valid_train_number(valid_train_numbers)
    valid_dates = list_available_dates(connection, train_number)
    if not valid_dates:
        return

    booking_date = get_valid_booking_date(valid_dates)
    category = get_valid_category()
    seats_available = get_seat_availability(connection, train_number, booking_date, category)
    if seats_available is None:
        print("Could not find seat information for that train and date.")
        return

    passenger_details = get_passenger_details()

    try:
        if seats_available > 0:
            ticket_id = insert_passenger(
                connection,
                train_number,
                booking_date,
                category,
                "Confirmed",
                passenger_details,
            )
            update_train_status(connection, train_number, booking_date, category, -1, 1)
            print(f"Ticket booked successfully. Ticket ID: {ticket_id}")
            return

        waiting_count = get_waiting_list_count(connection, train_number, booking_date, category)
        if waiting_count >= WAITING_LIST_LIMIT:
            print("Waiting list is full. Ticket cannot be booked.")
            return

        ticket_id = insert_passenger(
            connection,
            train_number,
            booking_date,
            category,
            "Waiting List",
            passenger_details,
        )
        print(f"No confirmed seats left. Added to waiting list with Ticket ID: {ticket_id}")
    except Error as exc:
        print(f"Booking failed: {exc}")


def cancel_ticket(connection):
    ticket_id = input("Enter Ticket Id: ").strip()
    if not ticket_id.isdigit():
        print("Ticket Id must be a number.")
        return

    ticket = fetch_one(
        connection,
        """
        SELECT ticket_id, trainNumber, Booking_Date, ticket_status, category
        FROM passenger
        WHERE ticket_id = %s
        """,
        (ticket_id,),
    )
    if ticket is None:
        print("Ticket not found.")
        return

    _, train_number, booking_date, ticket_status, category = ticket
    try:
        execute_write(connection, "DELETE FROM passenger WHERE ticket_id = %s", (ticket_id,))

        if ticket_status.strip().lower() == "confirmed":
            update_train_status(connection, train_number, booking_date, category, 1, -1)
            promote_waiting_ticket(connection, train_number, booking_date, category)

        print("Ticket cancelled successfully.")
    except Error as exc:
        print(f"Cancellation failed: {exc}")


def main():
    connection = connect_db()
    if connection is None:
        return

    try:
        print("1: Book a ticket")
        print("2: Cancel Ticket")
        choice = input("Choice: ").strip()
        if choice == "1":
            book_ticket(connection)
        elif choice == "2":
            cancel_ticket(connection)
        else:
            print("Invalid choice.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
