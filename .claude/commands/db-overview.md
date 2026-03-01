Run the following command and present the results as a formatted summary:

```bash
.venv/bin/python -c "
import sqlite3, datetime
conn = sqlite3.connect('data/tokens.db')
conn.row_factory = sqlite3.Row

print('=== Tokens by status ===')
for row in conn.execute('SELECT status, COUNT(*) as n FROM tokens GROUP BY status ORDER BY n DESC'):
    print(f'  {row[\"status\"]:<12} {row[\"n\"]}')

print()
print('=== Trades ===')
r = conn.execute('SELECT COUNT(*) as n, SUM(sol_amount) as vol FROM trades WHERE tx_type=\"buy\"').fetchone()
print(f'  buy trades:  {r[\"n\"]}  ({r[\"vol\"]:.1f} SOL volume)')
r = conn.execute('SELECT COUNT(*) as n FROM trades WHERE tx_type=\"sell\"').fetchone()
print(f'  sell trades: {r[\"n\"]}')

print()
print('=== 5 most recent tokens ===')
for row in conn.execute('SELECT mint, name, symbol, status, created_at FROM tokens ORDER BY created_at DESC LIMIT 5'):
    ts = datetime.datetime.fromtimestamp(row['created_at']).strftime('%H:%M:%S')
    print(f'  [{ts}] {row[\"symbol\"]:<12} {row[\"name\"][:30]:<30} {row[\"status\"]}')

print()
print('=== Price snapshots ===')
for row in conn.execute('SELECT delay_minutes, COUNT(*) as n, COUNT(ath_market_cap_usd) as with_ath FROM price_snapshots GROUP BY delay_minutes ORDER BY delay_minutes'):
    print(f'  {row[\"delay_minutes\"]:>5} min: {row[\"n\"]} snapshots  ({row[\"with_ath\"]} with ATH mc)')

print()
print('=== Market snapshots ===')
r = conn.execute('SELECT COUNT(*) as n, MIN(ts) as first, MAX(ts) as last FROM market_snapshots').fetchone()
if r['n']:
    first = datetime.datetime.fromtimestamp(r['first']).strftime('%H:%M:%S')
    last  = datetime.datetime.fromtimestamp(r['last']).strftime('%H:%M:%S')
    print(f'  {r[\"n\"]} rows  ({first} -> {last})')
else:
    print('  none yet')

conn.close()
"
```
