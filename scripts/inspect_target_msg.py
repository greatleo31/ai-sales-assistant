import sqlite3
import json

conn = sqlite3.connect("data/admin_analytics.db")
cur = conn.cursor()
row = cur.execute("SELECT user_query, ai_response, ui_cards_json, primary_intent, sub_category FROM chat_audit_logs WHERE message_id='msg-1177be416999'").fetchone()
if row:
    print("Query:", row[0])
    print("Response:\n", row[1])
    cards = json.loads(row[2])
    print("\nCards count:", len(cards.get("product_cards", [])))
    for c in cards.get("product_cards", []):
        print("  - SKU:", c.get("sku_id"), "| Title:", c.get("title"), "| Specs:", c.get("specs"))
    print("\nIntent:", row[3], "| SubCategory:", row[4])
