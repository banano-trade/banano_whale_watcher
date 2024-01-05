from flask import Flask, render_template, request
from flask_sqlalchemy import SQLAlchemy
import threading
import websocket
import json
import time
from datetime import datetime, timedelta
from extensions import db
import os
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///transactions.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
MINIMUM_DETECTABLE_BAN_AMOUNT = 10000

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
            transaction_time = datetime.fromtimestamp(int(data["time"]) / 1000)

            # Check if the amount is greater than the limit
            if float(transaction_data.get("amount_decimal", 0)) > MINIMUM_DETECTABLE_BAN_AMOUNT:
                transaction = Transaction(
                    account=transaction_data["account"],
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
    if request.method == 'POST':
        time_frame = request.form.get('time_frame')
        min_amount = float(request.form.get('min_amount', 0))
        # Check if the minimum amount is less than MINIMUM_DETECTABLE_BAN_AMOUNT
        if min_amount < MINIMUM_DETECTABLE_BAN_AMOUNT:
            return f"Minimum amount must be at least {MINIMUM_DETECTABLE_BAN_AMOUNT}", 400
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')

        if start_date_str and end_date_str:
            # Format the dates to 'yyyy-mm-dd'
            start_time = datetime.strptime(start_date_str, '%Y-%m-%d')
            end_time = datetime.strptime(end_date_str, '%Y-%m-%d')
        else:
            if time_frame == '24h':
                start_time = datetime.now() - timedelta(days=1)
            elif time_frame == '7d':
                start_time = datetime.now() - timedelta(days=7)
            elif time_frame == '30d':
                start_time = datetime.now() - timedelta(days=30)
            else:
                return "Invalid time frame", 400
            end_time = datetime.now()

        transactions = Transaction.query.filter(
            Transaction.time >= start_time,
            Transaction.time <= end_time,
            Transaction.amount_decimal > min_amount
        ).all()
        return render_template('index.html', transactions=transactions, filtered=True, MINIMUM_DETECTABLE_BAN_AMOUNT=MINIMUM_DETECTABLE_BAN_AMOUNT)

    else:
        latest_transactions = Transaction.query.order_by(Transaction.time.desc()).limit(50).all()
        return render_template('index.html', transactions=latest_transactions, filtered=False, MINIMUM_DETECTABLE_BAN_AMOUNT=MINIMUM_DETECTABLE_BAN_AMOUNT)

if __name__ == '__main__':
    app.run(host=os.getenv('HOST'), port=os.getenv('PORT'))