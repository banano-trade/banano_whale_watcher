from extensions import db
from datetime import datetime

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account = db.Column(db.String(120), nullable=False)
    amount_decimal = db.Column(db.Float, nullable=False)
    time = db.Column(db.DateTime, default=datetime.utcnow)
    hash = db.Column(db.String(120), nullable=False)

    def __repr__(self):
        return f'<Transaction {self.account}, {self.amount_decimal}>'
