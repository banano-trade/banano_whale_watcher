from flask import Flask, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
import threading
import websocket
import json
import time
from datetime import datetime, timedelta, timezone
from extensions import db
import os
from dotenv import load_dotenv
import logging
load_dotenv()
import requests
from threading import Thread
import time

# Global variable to store aliases
account_aliases = {}
websocket_connected = False
ws = None
retry_count = 0


def trim_account(account):
    if account and len(account) > 30:
        return f"{account[:10]}...{account[-6:]}"
    return account


def fetch_aliases():
    global account_aliases
    while True:
        try:
            response = requests.post("https://api.spyglass.pw/banano/v1/known/accounts")
            if response.status_code == 200:
                aliases = response.json()
                account_aliases = {item["address"]: item["alias"] for item in aliases}
        except Exception as e:
            logging.error(f"Failed to fetch aliases: {e}")

        # Wait for one day (86400 seconds) before fetching again
        time.sleep(86400)


# Start the alias fetching thread
alias_thread = Thread(target=fetch_aliases)
alias_thread.start()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///transactions.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
MINIMUM_DETECTABLE_BAN_AMOUNT = 10000
PER_PAGE = 20

db.init_app(app)

# Import models after initializing db with app
from models import Transaction


def create_tables():
    db.create_all()


with app.app_context():
    create_tables()  # Create tables before starting the application

# Global variable for the message timer
message_timer = None
MESSAGE_TIMEOUT = 60  # 60 seconds


def reset_message_timer():
    global message_timer
    if message_timer is not None:
        message_timer.cancel()  # Stop the previous timer
        message_timer = None  # Dereference the previous timer

    message_timer = threading.Timer(MESSAGE_TIMEOUT, reconnect_websocket)
    message_timer.start()


def reconnect_websocket():
    global ws, retry_count
    if retry_count >= MAX_RETRIES:
        logging.error("Max retries reached, will not attempt to reconnect.")
        return

    if ws is not None:
        ws.close()

    logging.info("Attempting to reconnect after a delay due to inactivity...")
    time.sleep(INITIAL_RETRY_DELAY)  # Delay before attempting to reconnect
    start_websocket_connection(websocket_url)


def on_message(ws, message):
    with app.app_context():
        global last_message_time
        data = json.loads(message)
        if data.get("topic") == "confirmation":
            last_message_time = datetime.utcnow()
            transaction_data = data["message"]
            transaction_time = datetime.fromtimestamp(
                int(data["time"]) / 1000, timezone.utc
            )

            # Check if the subtype is 'send' and the amount is greater than the limit
            if (
                transaction_data["block"]["subtype"] == "send"
                and float(transaction_data.get("amount_decimal", 0))
                > MINIMUM_DETECTABLE_BAN_AMOUNT
            ):
                sender = transaction_data["account"]
                receiver = transaction_data["block"].get("link_as_account")
                if sender != receiver:  # Ignore self transactions
                    existing_transaction = Transaction.query.filter_by(
                        hash=transaction_data["hash"]
                    ).first()
                    if not existing_transaction:
                        transaction = Transaction(
                            sender=sender,
                            receiver=receiver,
                            amount_decimal=float(
                                format(float(transaction_data["amount_decimal"]), ".2f")
                            ),
                            time=transaction_time,
                            hash=transaction_data["hash"],
                        )
                        db.session.add(transaction)
                        db.session.commit()


def on_error(ws, error):
    logging.error(f"WebSocket error: {error}")


def on_open(ws):
    global websocket_connected
    websocket_connected = True
    subscribe_message = {
        "action": "subscribe",
        "topic": "confirmation",
        "options": {"confirmation_type": "all"},
    }
    ws.send(json.dumps(subscribe_message))


MAX_RETRIES = 10  # Maximum number of retries
INITIAL_RETRY_DELAY = 1  # Initial delay in seconds before the first retry

run_websocket_thread = True
ws_lock = threading.Lock()


def start_websocket_connection(websocket_url):
    global ws, retry_count
    retry_delay = INITIAL_RETRY_DELAY

    while run_websocket_thread and retry_count < MAX_RETRIES:
        try:
            with ws_lock:
                if ws is not None:
                    ws.close()
                    ws = None

            ws = websocket.WebSocketApp(
                websocket_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever()
            logging.info("WebSocket connection established successfully.")
            retry_count = 0  # Reset retry count after a successful connection
            return  # Exit the function after successful connection
        except Exception as e:
            logging.error(f"WebSocket connection failed: {e}")
            retry_count += 1
            time.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

    logging.error("Reached maximum retry attempts, stopping reconnection attempts.")
    if message_timer:
        message_timer.cancel()

# Modify your existing on_close function to reset the timer
def on_close(ws, close_status_code, close_msg):
    global websocket_connected
    websocket_connected = False
    global message_timer
    if message_timer is not None:
        message_timer.cancel()  # Cancel the timer when the WebSocket is closed
    # Reconnect after 5 seconds
    time.sleep(5)
    logging.info("Reconnecting...")
    start_websocket_connection(websocket_url)


@app.route("/websocket-status")
def websocket_status():
    global last_message_time, websocket_connected
    try:
        if websocket_connected and last_message_time:
            last_message_str = last_message_time.strftime("%Y-%m-%d %H:%M:%S UTC")
            last_message_ago = int(
                (datetime.utcnow() - last_message_time).total_seconds()
            )
            if last_message_ago > 3600:
                return {
                    "status": False,
                    "last_message": "Last message is older than an hour ago",
                    "last_message_ago": last_message_ago,
                }
        else:
            last_message_str = "No message received"
        return {
            "status": websocket_connected,
            "last_message": last_message_str,
            "last_message_ago": last_message_ago,
        }
    except:
        return {"status": False, "last_message": "No message received"}


@app.route("/", methods=["GET"])
def index():
    page = request.args.get("page", 1, type=int)
    filtered = False
    date_range = None
    time_frame_display = None

    # Default values for filters
    min_amount = request.args.get(
        "min_amount", default=MINIMUM_DETECTABLE_BAN_AMOUNT, type=int
    )
    if min_amount > 100000000000000000:
        min_amount = 100000000000000000
    time_frame = request.args.get("time_frame", default="30d")

    # Calculate the start and end time based on the time frame
    end_time = datetime.now()
    if time_frame == "24h":
        start_time = end_time - timedelta(days=1)
        date_range = "Last 24 Hours"
    elif time_frame == "7d":
        start_time = end_time - timedelta(days=7)
        date_range = "Last 7 Days"
    elif time_frame == "30d":
        start_time = end_time - timedelta(days=30)
        date_range = "Last 30 Days"
    else:
        # Custom date range
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")
        if start_date_str and end_date_str:
            start_time = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_time = datetime.strptime(end_date_str, "%Y-%m-%d")
            date_range = (
                f"{start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}"
            )
            time_frame_display = "Custom Date Range"
        else:
            # Default to the last 30 days if no custom date range is provided
            start_time = end_time - timedelta(days=30)
            time_frame = "30d"
            time_frame_display = "Last 30 Days"

    filtered = True

    # Filtering and pagination logic
    transactions_query = Transaction.query.filter(
        Transaction.time >= start_time,
        Transaction.time <= end_time,
        Transaction.amount_decimal > min_amount,
    ).order_by(Transaction.time.desc())
    # Pagination URL Generation with Custom Date Range Support
    url_kwargs = {
        "min_amount": min_amount,
        "time_frame": time_frame,
        "start_date": request.args.get("start_date"),
        "end_date": request.args.get("end_date"),
    }
    transactions_paginated = transactions_query.paginate(
        page=page, per_page=PER_PAGE, error_out=False
    )

    if transactions_paginated.has_next:
        url_kwargs["page"] = transactions_paginated.next_num
        next_url = url_for("index", **url_kwargs)
    else:
        next_url = None

    if transactions_paginated.has_prev:
        url_kwargs["page"] = transactions_paginated.prev_num
        prev_url = url_for("index", **url_kwargs)
    else:
        prev_url = None

    transactions_with_alias = []
    for transaction in transactions_paginated.items:
        sender_alias = account_aliases.get(transaction.sender, transaction.sender)
        receiver_alias = account_aliases.get(transaction.receiver, transaction.receiver)
        transactions_with_alias.append(
            {
                "time": transaction.time.isoformat(),
                "sender": sender_alias,
                "receiver": receiver_alias,
                "amount_decimal": transaction.amount_decimal,
                "hash": transaction.hash,
            }
        )

    return render_template(
        "index.html",
        time_frame=time_frame,
        transactions=transactions_with_alias,
        filtered=filtered,
        min_amount=min_amount,
        date_range=date_range.lower(),
        time_frame_display=time_frame_display,
        MINIMUM_DETECTABLE_BAN_AMOUNT=MINIMUM_DETECTABLE_BAN_AMOUNT,
        next_url=next_url,
        prev_url=prev_url,
        trim_account=trim_account,
    )

def shutdown_server():
    global run_websocket_thread, websocket_threads, message_timer

    logging.info("Shutting down server...")

    # Stop the WebSocket connection threads
    run_websocket_thread = False
    for thread in websocket_threads:
        if thread.is_alive():
            thread.join()

    # Cancel the message timer if it's running
    if message_timer:
        message_timer.cancel()
        message_timer = None

    # Close WebSocket connection if it's open
    if ws:
        ws.close()

    logging.info("Server shutdown successfully.")

if __name__ == "__main__":
    try:
        urls = ["ws.banano.trade", "ws2.banano.trade"]
        websocket_threads = []
        for url in urls:
            websocket_url = f"wss://{url}"
            thread = threading.Thread(target=start_websocket_connection, args=(websocket_url,))
            thread.start()
            websocket_threads.append(thread)

        app.run(host=os.getenv("HOST"), port=os.getenv("PORT"))
    finally:
        shutdown_server()  # Ensure the server and its resources are cleanly shut down