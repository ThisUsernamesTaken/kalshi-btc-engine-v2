import json

files = ['data/paper_continuous_qcalveto.gated_full.jsonl', 'data/burnin_4h.qcalveto_nevbail.jsonl']

buys = []
ticker_rows = {}
for fn in files:
    with open(fn) as f:
        for line in f:
            r = json.loads(line)
            ticker_rows.setdefault((fn, r['market_ticker']), []).append(r)
            if r['action'] in ('BUY_YES','BUY_NO'):
                buys.append((fn, r))

def settle(rows):
    last = max(rows, key=lambda r: r['ts_ms'])
    if last['yes_ask_cents'] >= 95 or last['no_ask_cents'] <= 5:
        return 'YES', last
    if last['no_ask_cents'] >= 95 or last['yes_ask_cents'] <= 5:
        return 'NO', last
    return 'UNKNOWN', last

FEE = 3.5

settled = []
unsettled = []
for fn, b in buys:
    rows = ticker_rows[(fn, b['market_ticker'])]
    outcome, last = settle(rows)
    side = b['side'].upper()
    ctr = b['contracts']
    entry = b['no_ask_cents'] if side=='NO' else b['yes_ask_cents']
    if outcome == 'UNKNOWN':
        unsettled.append((fn, b, last, entry, side, ctr))
        continue
    won = (outcome == side)
    gross = ((100-entry) if won else -entry) * ctr / 100
    fee = FEE * ctr / 100
    settled.append(dict(
        file=fn.split('/')[-1][:30], ticker=b['market_ticker'], side=side, ctr=ctr,
        entry=entry, outcome=outcome, won=won, gross=gross, fee=fee, net=gross-fee,
        last_yes=last['yes_ask_cents'], last_no=last['no_ask_cents'], stc=last['seconds_to_close']
    ))

print(f'Total BUYs found: {len(buys)}  (Settled: {len(settled)}  Unsettled: {len(unsettled)})')
print()
print('UNSETTLED TRADES (data ends before market closes):')
for fn, b, last, entry, side, ctr in unsettled:
    fname = fn.split('/')[-1]
    print(f'  {b["market_ticker"]} [{fname}] {side} ctr={ctr} entry={entry}c -- last yes_ask={last["yes_ask_cents"]} no_ask={last["no_ask_cents"]} stc={last["seconds_to_close"]:.1f}s')

print()
print('PER-TRADE HOLD-TO-EXPIRY P&L (settled only)')
print('='*120)
print(f'{"#":>2} {"Ticker":28} {"Side":4} {"Ctr":>4} {"Entry":>6} {"Outcome":7} {"Won":3} {"Gross":>9} {"Fee":>7} {"Net":>9}')
print('-'*120)
for i, r in enumerate(settled, 1):
    won_str = "Y" if r["won"] else "N"
    print(f'{i:>2} {r["ticker"]:28} {r["side"]:4} {r["ctr"]:>4} {r["entry"]:>5}c {r["outcome"]:7} {won_str:3} ${r["gross"]:+8.2f} ${r["fee"]:6.2f} ${r["net"]:+8.2f}')

g = sum(r['gross'] for r in settled)
f = sum(r['fee'] for r in settled)
n = sum(r['net'] for r in settled)
w = sum(1 for r in settled if r['won'])
print('-'*120)
print(f'TOTALS ({len(settled)} settled): wins={w} losses={len(settled)-w} WR={w/len(settled)*100:.1f}%   gross=${g:+.2f}  fees=${f:.2f}  net=${n:+.2f}')

print()
print('COMPARISON vs PROFIT-CAPTURE BASELINE')
print('='*60)
print(f'  Profit-capture (live):  gross=+$2.21   net=+$0.39')
print(f'  Hold-to-expiry:         gross={g:+.2f}   net={n:+.2f}')
print(f'  Delta (HtE - capture):  gross={g-2.21:+.2f}   net={n-0.39:+.2f}')

print()
print('FIXED-SIZING SIMULATION (hold-to-expiry, settled trades only)')
print('='*60)
for fs in [5,10,20,50]:
    gg=0
    for r in settled:
        gg += ((100-r['entry']) if r['won'] else -r['entry']) * fs / 100
    ff = len(settled) * fs * 3.5 / 100
    print(f'  Fixed {fs:3} ct/trade ({len(settled)*fs} ct total): gross=${gg:+8.2f}  fees=${ff:7.2f}  net=${gg-ff:+8.2f}')

print()
print('PER-CONTRACT EDGE BREAKDOWN (fixed-sizing math)')
print('='*60)
win_sum = sum(100-r['entry'] for r in settled if r['won'])
loss_sum = sum(-r['entry'] for r in settled if not r['won'])
print(f'  Sum of profit-per-ct on wins:   {win_sum}c')
print(f'  Sum of loss-per-ct on losses:   {loss_sum}c')
print(f'  Net edge per fixed-size unit:   {(win_sum+loss_sum)}c  =  ${(win_sum+loss_sum)/100:.2f} per 1ct/trade')
print(f'  Per-trade fee (one side):       {len(settled)} * 3.5c = {len(settled)*3.5}c per ct fixed')
print(f'  Per-ct break-even net needs:    +{len(settled)*3.5}c gross')
