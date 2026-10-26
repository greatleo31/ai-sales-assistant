import sqlite3, json

conn = sqlite3.connect("data/admin_analytics.db")
c = conn.cursor()
c.execute("SELECT rowid, user_query, primary_intent, sub_category, top_skus_json, ui_cards_json, ai_response, created_at, constraint_status FROM chat_audit_logs WHERE session_id = 'hg-mvkljwzq' ORDER BY rowid ASC")
rows = c.fetchall()
print(f"Total traces in hg-mvkljwzq: {len(rows)}")
for r in rows:
    print(f"=== ROW {r[0]} | TIME: {r[7]} ===")
    print("QUERY:", r[1])
    print(f"INTENT: {r[2]} | SUBCAT: {r[3]} | STATUS: {r[8]}")
    print("TOP SKUS:", r[4])
    cards = json.loads(r[5]) if r[5] else {}
    prod_cards = cards.get("product_cards", [])
    print("PROD CARDS:", [f"{p.get('sku_id')}: {p.get('title')}" for p in prod_cards])
    print("RESP:", (r[6] or "")[:250])
    print("-" * 60)
