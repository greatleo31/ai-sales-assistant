import sqlite3
import json

conn = sqlite3.connect("data/admin_analytics.db")
cur = conn.cursor()
tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables in db:", tables)

for table in tables:
    cnt = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"Table: {table}, count: {cnt}")

# 找到存储对话的表
msg_table = next((t for t in tables if "msg" in t or "chat" in t or "conv" in t), None)
if msg_table:
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({msg_table})").fetchall()]
    print(f"Columns in {msg_table}:", cols)
    rows = cur.execute(f"SELECT * FROM {msg_table} ORDER BY rowid DESC LIMIT 20").fetchall()
    print(f"\nRecent {len(rows)} rows from {msg_table}:")
    for r in rows:
        d = dict(zip(cols, r))
        print("="*60)
        print(f"ID: {d.get('id') or d.get('message_id')} | Time: {d.get('timestamp') or d.get('created_at')} | Session: {d.get('session_id')}")
        print(f"Query: {d.get('user_query')}")
        resp = str(d.get('ai_response') or '')
        print(f"AI Response: {resp[:200]}...")
        cards_val = d.get('ui_cards')
        if cards_val:
            try:
                c_json = json.loads(cards_val)
                p_cards = [c.get('sku_id') for c in c_json.get('product_cards', [])]
                print(f"Cards: product_cards={p_cards}, aftersales={bool(c_json.get('aftersales_card'))}, wishlist={bool(c_json.get('wishlist_card'))}")
            except Exception:
                print(f"Cards: {str(cards_val)[:100]}")
        meta_val = d.get('decision_meta')
        if meta_val:
            try:
                m_json = json.loads(meta_val)
                print(f"Meta: intent={m_json.get('primary_intent')}, sub_cat={m_json.get('extracted_constraints', {}).get('sub_category')}, status={m_json.get('constraint_status')}")
            except Exception:
                pass
