CREATE TABLE customers (customer_id TEXT PRIMARY KEY, segment TEXT NOT NULL);
CREATE TABLE orders (order_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(customer_id), order_date TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0));
CREATE TABLE refunds (refund_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id), refund_date TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0));
