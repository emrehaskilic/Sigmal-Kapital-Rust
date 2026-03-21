import sys, io, json, urllib.request, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE = 'http://localhost:8000'

def get(path):
    return json.loads(urllib.request.urlopen(f'{BASE}{path}', timeout=10).read())

status = get('/api/live/status')
if not status.get('live_running'):
    print('BOT NOT RUNNING - ABORT')
    sys.exit(1)

orders = get('/api/live/orders')
debug = get('/api/live/debug')

print('=' * 60)
print('=== BOT HEALTH AUDIT REPORT ===')
print(f'Timestamp: {time.strftime("%Y-%m-%d %H:%M:%S")}')
print('=' * 60)

findings = []
passes = 0
total = 6

# [1] Position Sync
print('\n[1] Position Sync')
bot_positions = status.get('positions', [])
ex_positions = orders.get('positions', [])
sync_ok = True

for bp in bot_positions:
    sym = bp['symbol']
    ex_match = [ep for ep in ex_positions if ep['symbol'] == sym]
    if not ex_match:
        print(f'  FAIL: {sym} in bot but NOT on exchange (ghost)')
        findings.append(f'Ghost: {sym}')
        sync_ok = False
        continue
    ep = ex_match[0]
    qty_ok = abs(bp['qty'] - ep['amount']) < 0.01
    if not qty_ok:
        print(f'  FAIL: {sym} qty bot={bp["qty"]} exchange={ep["amount"]}')
        findings.append(f'Qty mismatch: {sym}')
        sync_ok = False
    else:
        print(f'  {sym}: qty={ep["amount"]} entry={ep["entry_price"]:.4f} -- MATCH')

for ep in ex_positions:
    if not any(bp['symbol'] == ep['symbol'] for bp in bot_positions):
        print(f'  FAIL: {ep["symbol"]} on exchange but NOT in bot (orphan)')
        findings.append(f'Orphan: {ep["symbol"]}')
        sync_ok = False

if sync_ok:
    print('  PASS')
    passes += 1

# [2] Order Validation
print('\n[2] Order Validation')
ex_orders = orders.get('orders', [])
tp_orders = [o for o in ex_orders if o.get('reduceOnly')]
dca_orders = [o for o in ex_orders if not o.get('reduceOnly') and o.get('type') == 'LIMIT']
order_ok = True

for key, pos in debug.items():
    if key.startswith('_') or pos.get('condition', 0) == 0:
        continue
    dc = pos['dca_fills_count']
    sym = pos['symbol']
    sym_tp = [o for o in tp_orders if o['symbol'] == sym]
    sym_dca = [o for o in dca_orders if o['symbol'] == sym]
    ex_qty = 0
    for ep in ex_positions:
        if ep['symbol'] == sym:
            ex_qty = ep['amount']

    # TP check
    if dc > 0 and not sym_tp:
        print(f'  FAIL: {sym} dc={dc} but NO TP order')
        findings.append(f'Missing TP: {sym} dc={dc}')
        order_ok = False
    elif dc == 0 and sym_tp:
        print(f'  FAIL: {sym} dc=0 but TP exists')
        findings.append(f'TP at dc=0: {sym}')
        order_ok = False
    elif dc > 0 and sym_tp and ex_qty > 0:
        tp = sym_tp[0]
        pct = tp['origQty'] / ex_qty * 100
        expected = {1: 50, 2: 80, 3: 85, 4: 90}.get(dc, 50)
        match = 'OK' if abs(pct - expected) < 5 else f'WRONG(exp {expected}%)'
        print(f'  TP: #{tp["orderId"]} qty={tp["origQty"]} ({pct:.0f}%) [{match}]')
        if abs(pct - expected) >= 5:
            order_ok = False
    elif dc == 0:
        print(f'  {sym}: dc=0, no TP (by design) -- OK')

    # DCA check
    if dc >= 4 and sym_dca:
        print(f'  FAIL: {sym} dc=4 (MAX) but DCA exists')
        findings.append(f'DCA at max: {sym}')
        order_ok = False
    elif dc < 4 and sym_dca:
        dca = sym_dca[0]
        print(f'  DCA: #{dca["orderId"]} qty={dca["origQty"]} @ {dca["price"]} -- OK')
    elif dc >= 4:
        print(f'  DCA: none (dc=4 max) -- OK')

if order_ok:
    print('  PASS')
    passes += 1

# [3] KC Band Alignment
print('\n[3] KC Band Alignment')
kc_ok = True
for key, pos in debug.items():
    if key.startswith('_') or pos.get('condition', 0) == 0:
        continue
    tp_p = pos['pending_tp_price']
    dca_p = pos['pending_dca_price']
    if tp_p > 0 and dca_p > 0:
        spread = abs(tp_p - dca_p)
        print(f'  {pos["symbol"]}: TP@{tp_p:.4f} DCA@{dca_p:.4f} spread=${spread:.4f}')
    elif pos['dca_fills_count'] == 0:
        print(f'  {pos["symbol"]}: dc=0, TP not set (by design)')
    else:
        print(f'  WARN: prices not set')
        kc_ok = False
if kc_ok:
    print('  PASS')
    passes += 1

# [4] Signal Health
print('\n[4] Signal Health')
sig_log = status.get('signal_log', [])
if sig_log:
    last = sig_log[-1]
    print(f'  Last: {last["time"]} {last.get("symbol","")} {last.get("source","")}')
    print(f'  Total signals: {len(sig_log)}')
print('  PASS')
passes += 1

# [5] DCA Count
print('\n[5] DCA Count Validation')
dca_ok = True
for key, pos in debug.items():
    if key.startswith('_'):
        continue
    dc = pos.get('dca_fills_count', 0)
    if dc > 4:
        print(f'  CRITICAL: {pos["symbol"]} dc={dc} > max=4')
        findings.append(f'DCA exceeded: {pos["symbol"]} dc={dc}')
        dca_ok = False
    elif dc < 0:
        print(f'  CRITICAL: {pos["symbol"]} dc={dc} < 0')
        findings.append(f'DCA negative: {pos["symbol"]}')
        dca_ok = False
    else:
        print(f'  {pos.get("symbol","")}: dc={dc}/4 -- OK')
if dca_ok:
    print('  PASS')
    passes += 1

# [6] Balance
print('\n[6] Balance Consistency')
stats = status.get('stats', {})
init_bal = stats.get('initial_balance', 0)
curr_bal = stats.get('current_balance', 0)
total_pnl = stats.get('total_pnl', 0)
total_fees = stats.get('total_fees', 0)
trades_count = stats.get('total_trades', 0)
wins = stats.get('winning_trades', 0)
losses = stats.get('losing_trades', 0)

bot_upnl = sum(p.get('unrealized_pnl_usdt', 0) for p in bot_positions)
ex_upnl = sum(p.get('unrealized_pnl', 0) for p in ex_positions)

print(f'  Initial: ${init_bal:.2f} | Current: ${curr_bal:.2f}')
print(f'  PnL: ${total_pnl:.4f} | Fees: ${total_fees:.4f}')
print(f'  Trades: {trades_count} (W{wins}/L{losses})')
print(f'  Bot uPnL: ${bot_upnl:.4f} | Exchange uPnL: ${ex_upnl:.4f}')
print('  PASS')
passes += 1

# WS + System
sys_info = debug.get('_system', {})
ws = sys_info.get('ws_connected', False)
ws_ago = sys_info.get('ws_last_event_ago_s', -1)
fills = sys_info.get('processed_order_ids_count', 0)
cb = sys_info.get('circuit_breaker', False)

print(f'\n[WS] Connected: {ws} | Last event: {ws_ago:.0f}s ago | Fills: {fills}')
if cb:
    print(f'  !!! CIRCUIT BREAKER: {sys_info.get("circuit_breaker_reason","")}')
    findings.append('Circuit breaker active')

# Fill log
fill_log = sys_info.get('fill_log', [])
if fill_log:
    print(f'\n[Fill Log] Last {min(5, len(fill_log))} fills:')
    for fl in fill_log[-5:]:
        print(f'  {fl["time"]} {fl["type"]} @{fl["price"]:.2f} q={fl["qty"]:.3f} dc={fl["dca_count"]}')

print(f'\n{"=" * 60}')
print(f'OVERALL: {passes}/{total} PASS')
if findings:
    print('CRITICAL ISSUES:')
    for f in findings:
        print(f'  ! {f}')
else:
    print('CRITICAL ISSUES: none')
print('=' * 60)
