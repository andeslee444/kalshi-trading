#!/bin/bash
# Auto-scorer: checks every 15 min until signals are settled
cd "$(dirname "$0")/.."

echo "Auto-scorer started at $(date)"
echo "Checking every 15 minutes for settled markets..."

for i in $(seq 1 24); do  # max 6 hours
    echo ""
    echo "--- Check $i at $(date) ---"
    
    result=$(python3 -c "
import requests, json
signals = json.loads(open('data/signals.json').read())['signals']
settled = 0
for s in signals[:20]:
    resp = requests.get(f'https://api.elections.kalshi.com/trade-api/v2/markets/{s[\"ticker\"]}', timeout=10)
    if resp.status_code == 200:
        m = resp.json().get('market', {})
        if m.get('status') in ('finalized', 'settled', 'determined'):
            settled += 1
print(settled)
" 2>/dev/null)
    
    echo "Settled: $result/20 sampled"
    
    if [ "$result" -gt "5" ] 2>/dev/null; then
        echo "Markets settling! Running full scorer..."
        python3 -m analysis.score_signals
        echo "Scoring complete at $(date)"
        exit 0
    fi
    
    echo "Waiting 15 minutes..."
    sleep 900
done

echo "Timeout after 6 hours. Running scorer with whatever settled..."
python3 -m analysis.score_signals
