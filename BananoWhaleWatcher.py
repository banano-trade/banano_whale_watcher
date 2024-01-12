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
load_dotenv()
import requests
from threading import Thread
import time

# Global variable to store aliases
account_aliases = {}

def trim_account(account):
    if account and len(account) > 30:
        return f"{account[:10]}...{account[-6:]}"
    return account

def fetch_aliases():
    global account_aliases
    while True:
        try:
            response = requests.post('https://api.spyglass.pw/banano/v1/known/accounts')
            if response.status_code == 200:
                aliases = response.json()
                account_aliases = {item['address']: item['alias'] for item in aliases}
        except Exception as e:
            print(f"Error fetching aliases: {e}")
        
        # Wait for one day (86400 seconds) before fetching again
        time.sleep(86400)
# Start the alias fetching thread
alias_thread = Thread(target=fetch_aliases)
alias_thread.start()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///transactions.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
MINIMUM_DETECTABLE_BAN_AMOUNT = 10000
PER_PAGE = 20

db.init_app(app)

# Import models after initializing db with app
from models import Transaction

def create_tables():
    db.create_all()

with app.app_context():
    create_tables()  # Create tables before starting the application

def on_message(ws, message):
    with app.app_context():
        data = json.loads(message)
        if data["topic"] == "confirmation":
            transaction_data = data["message"]
            transaction_time = datetime.fromtimestamp(int(data["time"]) / 1000, timezone.utc)

            # Check if the subtype is 'send' and the amount is greater than the limit
            if transaction_data["block"]["subtype"] == "send" and float(transaction_data.get("amount_decimal", 0)) > MINIMUM_DETECTABLE_BAN_AMOUNT:
                sender = transaction_data["account"]
                receiver = transaction_data["block"].get("link_as_account")

                transaction = Transaction(
                    sender=sender,
                    receiver=receiver,
                    amount_decimal=float(format(float(transaction_data["amount_decimal"]), '.2f')),
                    time=transaction_time,
                    hash=transaction_data["hash"]
                )
                db.session.add(transaction)
                db.session.commit()

def on_error(ws, error):
    print(error)

def on_close(ws, close_status_code, close_msg):
    print("### closed ###")
    # Reconnect after 5 seconds
    time.sleep(5)
    start_websocket()

def on_open(ws):
    subscribe_message = {
        "action": "subscribe",
        "topic": "confirmation",
        "options": {"confirmation_type": "all"}
    }
    ws.send(json.dumps(subscribe_message))

def start_websocket():
    current_url = "ws.banano.trade"
    alternate = True  # Flag to alternate between URLs

    while True:
        websocket_url = f"wss://{current_url}"
        ws = websocket.WebSocketApp(websocket_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close)

        ws.run_forever()

        # Wait for 5 seconds before retrying
        time.sleep(5)

        # Alternate between the two URLs
        if alternate:
            current_url = "ws2.banano.trade"
        else:
            current_url = "ws.banano.trade"
        alternate = not alternate

# Start WebSocket in a separate thread
threading.Thread(target=start_websocket).start()

@app.route('/', methods=['GET', 'POST'])
def index():
    page = request.args.get('page', 1, type=int)
    filtered = False
    min_amount = MINIMUM_DETECTABLE_BAN_AMOUNT
    date_range = None
    time_frame_display = None

    if request.method == 'POST':
        time_frame = request.form.get('time_frame')
        min_amount = int(request.form.get('min_amount', 0))

        if min_amount < MINIMUM_DETECTABLE_BAN_AMOUNT:
            return f"Minimum amount must be at least {MINIMUM_DETECTABLE_BAN_AMOUNT}", 400

        if time_frame:
            if time_frame == '24h':
                start_time = datetime.now() - timedelta(days=1)
                time_frame_display = "Last 24 Hours"
            elif time_frame == '7d':
                start_time = datetime.now() - timedelta(days=7)
                time_frame_display = "Last 7 Days"
            elif time_frame == '30d':
                start_time = datetime.now() - timedelta(days=30)
                time_frame_display = "Last 30 Days"
            end_time = datetime.now()

        elif request.form.get('start_date') and request.form.get('end_date'):
            start_time = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d')
            end_time = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d')
            date_range = f"{start_time.strftime('%Y-%m-%d')} to {end_time.strftime('%Y-%m-%d')}"

        transactions_query = Transaction.query.filter(
            Transaction.time >= start_time,
            Transaction.time <= end_time,
            Transaction.amount_decimal > min_amount
        )
        filtered = True

    if filtered:
        transactions_query = Transaction.query.filter(
            Transaction.time >= start_time,
            Transaction.time <= end_time,
            Transaction.amount_decimal > min_amount
        ).order_by(Transaction.time.desc())
    else:
        transactions_query = Transaction.query.order_by(Transaction.time.desc())

    # Pagination
    transactions_paginated = transactions_query.paginate(page=page, per_page=PER_PAGE, error_out=False)
    next_url = url_for('index', page=transactions_paginated.next_num) if transactions_paginated.has_next else None
    prev_url = url_for('index', page=transactions_paginated.prev_num) if transactions_paginated.has_prev else None

    transactions_with_alias = []
    for transaction in transactions_paginated.items:
        sender_alias = account_aliases.get(transaction.sender, transaction.sender)
        receiver_alias = account_aliases.get(transaction.receiver, transaction.receiver)
        transactions_with_alias.append({
            'time': transaction.time.isoformat(),
            'sender': sender_alias,
            'receiver': receiver_alias,
            'amount_decimal': transaction.amount_decimal,
            'hash': transaction.hash
        })

    return render_template('index.html', transactions=transactions_with_alias, filtered=filtered, min_amount=min_amount, date_range=date_range, time_frame_display=time_frame_display, MINIMUM_DETECTABLE_BAN_AMOUNT=MINIMUM_DETECTABLE_BAN_AMOUNT, next_url=next_url, prev_url=prev_url, trim_account=trim_account)

if __name__ == '__main__':
    app.run(host=os.getenv('HOST'), port=os.getenv('PORT'))