"""Batch 6C - scripted LLM seed answers for the two Batch 6B scenarios (what a real model would have to return).
They are fixtures for repeatable offline tests; they say nothing about how well a real model would behave."""

BOOKING_SEED = {"tables": {
    "customers": [
        {"customer_id": 1, "full_name": "Maryam Karimi", "email": "maryam.karimi@example.com", "phone": "+98 917 555 0101"},
        {"customer_id": 2, "full_name": "Sara Ahmadi", "email": "sara.ahmadi@example.com", "phone": None},
        {"customer_id": 3, "full_name": "Neda Hosseini", "email": "neda.hosseini@example.com", "phone": "+98 917 555 0103"},
        {"customer_id": 4, "full_name": "Leila Rostami", "email": "leila.rostami@example.com", "phone": "+98 917 555 0104"},
        {"customer_id": 5, "full_name": "Parisa Najafi", "email": "parisa.najafi@example.com", "phone": "+98 917 555 0105"}],
    "employees": [
        {"employee_id": 1, "full_name": "Azadeh Sadeghi", "email": "azadeh@salon.example"},
        {"employee_id": 2, "full_name": "Roya Mohammadi", "email": "roya@salon.example"},
        {"employee_id": 3, "full_name": "Mina Farahani", "email": "mina@salon.example"},
        {"employee_id": 4, "full_name": "Shirin Davoudi", "email": "shirin@salon.example"},
        {"employee_id": 5, "full_name": "Hengameh Ebrahimi", "email": "hengameh@salon.example"}],
    "services": [
        {"service_id": 1, "name": "Haircut", "duration_minutes": 45, "price": 25.0},
        {"service_id": 2, "name": "Hair colouring", "duration_minutes": 120, "price": 80.0},
        {"service_id": 3, "name": "Manicure", "duration_minutes": 30, "price": 18.5},
        {"service_id": 4, "name": "Facial treatment", "duration_minutes": 60, "price": 45.0},
        {"service_id": 5, "name": "Blow dry", "duration_minutes": 1, "price": 12.0}],
    "employee_services": [
        {"employee_id": 1, "service_id": 1}, {"employee_id": 1, "service_id": 5}, {"employee_id": 2, "service_id": 1},
        {"employee_id": 2, "service_id": 2}, {"employee_id": 3, "service_id": 3}, {"employee_id": 4, "service_id": 4},
        {"employee_id": 5, "service_id": 3}],
    "appointments": [
        {"appointment_id": 1, "starts_at": "2026-11-16 09:00:00", "ends_at": "2026-11-16 09:45:00", "status": "booked",
         "notes": "First visit", "customer_id": 1, "employee_id": 1, "service_id": 1},
        {"appointment_id": 2, "starts_at": "2026-11-16 10:00:00", "ends_at": "2026-11-16 12:00:00", "status": "confirmed",
         "notes": None, "customer_id": 2, "employee_id": 2, "service_id": 2},
        {"appointment_id": 3, "starts_at": "2026-11-09 14:00:00", "ends_at": "2026-11-09 14:30:00", "status": "completed",
         "notes": None, "customer_id": 3, "employee_id": 3, "service_id": 3},
        # history: the same employee and start time as appointment 1, but cancelled -> must stay allowed
        {"appointment_id": 4, "starts_at": "2026-11-16 09:00:00", "ends_at": "2026-11-16 09:45:00", "status": "cancelled",
         "notes": "Customer cancelled by phone", "customer_id": 4, "employee_id": 1, "service_id": 1},
        {"appointment_id": 5, "starts_at": "2026-11-10 11:00:00", "ends_at": "2026-11-10 12:00:00", "status": "no_show",
         "notes": "Did not arrive", "customer_id": 5, "employee_id": 4, "service_id": 4}],
    "status_changes": [
        {"change_id": 1, "old_status": None, "new_status": "booked", "changed_at": "2026-11-12 08:00:00", "reason": None, "appointment_id": 1},
        {"change_id": 2, "old_status": "booked", "new_status": "confirmed", "changed_at": "2026-11-13 09:30:00", "reason": "Customer replied to the reminder", "appointment_id": 2},
        {"change_id": 3, "old_status": "confirmed", "new_status": "completed", "changed_at": "2026-11-09 14:35:00", "reason": None, "appointment_id": 3},
        {"change_id": 4, "old_status": "booked", "new_status": "cancelled", "changed_at": "2026-11-14 16:00:00", "reason": "Customer cancelled by phone", "appointment_id": 4},
        {"change_id": 5, "old_status": "confirmed", "new_status": "no_show", "changed_at": "2026-11-10 12:15:00", "reason": "Customer did not arrive", "appointment_id": 5}]},
    "notes": "Salon sample data."}

ORDERING_SEED = {"tables": {
    "customers": [
        {"customer_id": 1, "full_name": "Kaveh Rahimi", "email": "kaveh.rahimi@example.com"},
        {"customer_id": 2, "full_name": "Nasrin Tehrani", "email": "nasrin.tehrani@example.com"},
        {"customer_id": 3, "full_name": "Omid Shams", "email": "omid.shams@example.com"},
        {"customer_id": 4, "full_name": "Yasaman Gholami", "email": "yasaman.gholami@example.com"},
        {"customer_id": 5, "full_name": "Bijan Alavi", "email": "bijan.alavi@example.com"}],
    "products": [
        {"product_id": 1, "sku": "KB-100", "name": "Mechanical keyboard", "current_price": 89.9},
        {"product_id": 2, "sku": "MS-200", "name": "Wireless mouse", "current_price": 24.5},
        {"product_id": 3, "sku": "MN-300", "name": "27-inch monitor", "current_price": 259.0},
        {"product_id": 4, "sku": "HD-400", "name": "USB-C hub", "current_price": 0},
        {"product_id": 5, "sku": "CB-500", "name": "HDMI cable 2 m", "current_price": 9.99}],
    "product_prices": [
        {"price_id": 1, "price": 79.9, "valid_from": "2026-01-01 00:00:00", "product_id": 1},
        {"price_id": 2, "price": 89.9, "valid_from": "2026-09-01 00:00:00", "product_id": 1},
        {"price_id": 3, "price": 24.5, "valid_from": "2026-01-01 00:00:00", "product_id": 2},
        {"price_id": 4, "price": 259.0, "valid_from": "2026-01-01 00:00:00", "product_id": 3},
        {"price_id": 5, "price": 9.99, "valid_from": "2026-01-01 00:00:00", "product_id": 5}],
    "sales_orders": [
        {"order_id": 1, "placed_at": "2026-08-15 10:20:00", "status": "shipped", "customer_id": 1},
        {"order_id": 2, "placed_at": "2026-10-02 18:45:00", "status": "paid", "customer_id": 2},
        {"order_id": 3, "placed_at": "2026-11-01 09:05:00", "status": "pending", "customer_id": 3},
        {"order_id": 4, "placed_at": "2026-11-05 21:30:00", "status": "cancelled", "customer_id": 4},
        {"order_id": 5, "placed_at": "2026-11-10 13:10:00", "status": "paid", "customer_id": 1}],
    # order 1 was placed when the keyboard cost 79.90: the line keeps that price although the catalogue price is now 89.90
    "order_items": [
        {"order_id": 1, "product_id": 1, "quantity": 1, "unit_price": 79.9},
        {"order_id": 1, "product_id": 2, "quantity": 2, "unit_price": 24.5},
        {"order_id": 2, "product_id": 3, "quantity": 1, "unit_price": 259.0},
        {"order_id": 3, "product_id": 5, "quantity": 3, "unit_price": 9.99},
        {"order_id": 5, "product_id": 1, "quantity": 1, "unit_price": 89.9}],
    "payments": [
        {"payment_id": 1, "amount": 128.9, "method": "card", "paid_at": "2026-08-15 10:25:00", "order_id": 1},
        {"payment_id": 2, "amount": 100.0, "method": "transfer", "paid_at": "2026-10-02 19:00:00", "order_id": 2},
        {"payment_id": 3, "amount": 159.0, "method": "cash", "paid_at": None, "order_id": 2},
        {"payment_id": 4, "amount": 29.97, "method": "card", "paid_at": None, "order_id": 3},
        {"payment_id": 5, "amount": 89.9, "method": "card", "paid_at": "2026-11-10 13:12:00", "order_id": 5}],
    "order_status_events": [
        {"event_id": 1, "status": "pending", "changed_at": "2026-08-15 10:20:00", "order_id": 1},
        {"event_id": 2, "status": "paid", "changed_at": "2026-08-15 10:25:00", "order_id": 1},
        {"event_id": 3, "status": "shipped", "changed_at": "2026-08-17 08:00:00", "order_id": 1},
        {"event_id": 4, "status": "cancelled", "changed_at": "2026-11-05 22:00:00", "order_id": 4},
        {"event_id": 5, "status": "paid", "changed_at": "2026-11-10 13:12:00", "order_id": 5}]},
    "notes": "Shop sample data."}
