# -*- coding: utf-8 -*-
import sqlite3
import json

conn = sqlite3.connect('data/admin_analytics.db')
c = conn.cursor()

def dump_row(rowid):
    c.execute("SELECT rowid, user_query, primary_intent, sub_category, constraint_status, top_skus_json, ui_cards_json, decision_source FROM chat_audit_logs WHERE rowid=?", (rowid,))
    r = c.fetchone()
    print(f"\n--- Rowid {rowid} ---")
    print("Query:", r[1])
    print("Intent:", r[2], "| SubCat:", r[3], "| Status:", r[4], "| Source:", r[7])
    print("Top SKUs:", r[5])
    cards = json.loads(r[6]) if r[6] else {}
    for pc in cards.get("product_cards", []):
        print(f"  Product: {pc.get('sku_id')} - {pc.get('title')} (specs={pc.get('specs')})")

for rid in [15, 17, 24, 26, 28]:
    dump_row(rid)
