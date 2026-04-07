# main.py — KalshiArb
"""
KalshiArb — Daily combo arb bot
1. Pull sharp Pinnacle odds for today's games
2. Find EV legs (Pinnacle implied prob > Kalshi price)
3. Build 4-leg combos across different games
4. Compare Kalshi combo payout vs fair parlay odds
5. If gap > MIN_GAP_PTS → place all 4 legs via Kalshi API (fill_or_kill)
6. Max 4 combos per day, $50 per combo
"""

import os
import time
import logging
import uuid
from datetime import datetime, timezone
from itertools import combinations

from kalshi_client import KalshiClient
from odds_client import OddsClient
from combo_engine import find_ev_legs, build_combos, calc_fair_parlay, calc_kalshi_payout

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('KalshiArb')

# ── Config ─────────────────────────────────────────────────────────────────────
KALSHI_KEY_ID     = os.environ['KALSHI_KEY_ID']
KALSHI_PRIVATE_KEY = os.environ['KALSHI_PRIVATE_KEY_B64']
ODDS_API_KEY      = os.environ['ODDS_API_KEY']

COST_PER_COMBO    = 50       # dollars per combo
MAX_COMBOS_PER_DAY = 4       # max trades per day
MIN_GAP_PTS       = 50       # minimum Kalshi payout - fair payout gap (american odds pts)
LEGS_PER_COMBO    = 3        # legs per combo (min 3, max 4)
SCAN_INTERVAL     = 300      # seconds between scans (5 min)

SPORTS = [
    'baseball_mlb',
    'basketball_nba',
    'icehockey_nhl',
    'americanfootball_nfl',
]

SHARP_BOOKS = ['pinnacle', 'betfair']


def run():
    kalshi = KalshiClient(KALSHI_KEY_ID, KALSHI_PRIVATE_KEY)
    odds   = OddsClient(ODDS_API_KEY)

    combos_placed_today = 0
    last_date           = datetime.now(timezone.utc).date()

    logger.info(f"KalshiArb starting | ${COST_PER_COMBO}/combo | max {MAX_COMBOS_PER_DAY}/day | min gap {MIN_GAP_PTS}pts")

    while True:
        try:
            # Reset daily counter
            today = datetime.now(timezone.utc).date()
            if today != last_date:
                combos_placed_today = 0
                last_date = today
                logger.info("New day — daily counter reset")

            if combos_placed_today >= MAX_COMBOS_PER_DAY:
                logger.info(f"Daily limit reached ({MAX_COMBOS_PER_DAY} combos). Waiting...")
                time.sleep(SCAN_INTERVAL)
                continue

            logger.info("=" * 60)
            logger.info(f"Scan | {combos_placed_today}/{MAX_COMBOS_PER_DAY} combos placed today")

            # ── Step 1: Pull sharp odds ────────────────────────────────────
            all_lines = []
            for sport in SPORTS:
                try:
                    lines = odds.get_sharp_lines(sport, SHARP_BOOKS)
                    all_lines.extend(lines)
                except Exception as e:
                    logger.warning(f"Odds fetch failed for {sport}: {e}")

            logger.info(f"Fetched {len(all_lines)} sharp lines")

            # ── Step 2: Pull Kalshi contracts ──────────────────────────────
            kalshi_contracts = kalshi.get_sports_markets()
            logger.info(f"Fetched {len(kalshi_contracts)} Kalshi contracts")

            # ── Step 3: Find EV legs ───────────────────────────────────────
            ev_legs = find_ev_legs(all_lines, kalshi_contracts)
            logger.info(f"Found {len(ev_legs)} EV legs")

            if len(ev_legs) < 3:
                logger.info("Not enough EV legs for a combo")
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Step 4: Build combos and find best gap ─────────────────────
            # Try 4-leg combos first, fall back to 3-leg
            combos = build_combos(ev_legs, n_legs=4, max_combos=500)
            if not combos:
                combos = build_combos(ev_legs, n_legs=3, max_combos=500)
            logger.info(f"Built {len(combos)} candidate combos")

            placed = False
            for combo in combos:
                fair_am   = calc_fair_parlay(combo)
                kalshi_am = calc_kalshi_payout(combo)
                gap       = kalshi_am - fair_am

                if gap < MIN_GAP_PTS:
                    continue

                ev_pct = sum(leg['edge_pct'] for leg in combo) / len(combo)
                logger.info(f"SIGNAL: {len(combo)}L | Kalshi {kalshi_am:+d} vs Fair {fair_am:+d} | Gap {gap:+d}pts | Avg edge {ev_pct:.1f}%")
                for leg in combo:
                    logger.info(f"  {leg['display']} | Kalshi {leg['kalshi_price']:.0f}c | Fair {leg['fair_prob']:.1%} | {leg['event']}")

                # ── Step 5: Place orders ───────────────────────────────────
                success = place_combo(kalshi, combo, COST_PER_COMBO)
                if success:
                    combos_placed_today += 1
                    logger.info(f"✓ Combo placed ({combos_placed_today}/{MAX_COMBOS_PER_DAY}) | ${COST_PER_COMBO} at {kalshi_am:+d}")
                    placed = True
                    break  # one combo per scan cycle

            if not placed:
                logger.info(f"No combo met the gap threshold of {MIN_GAP_PTS}pts")

        except Exception as e:
            logger.error(f"Scan error: {e}", exc_info=True)

        time.sleep(SCAN_INTERVAL)


def place_combo(kalshi: KalshiClient, combo: list, total_cost: int) -> bool:
    """
    Place all legs of a combo as fill_or_kill limit orders.
    Cost is split evenly across legs.
    Returns True if all legs filled.
    """
    cost_per_leg = total_cost / len(combo)
    orders       = []
    group_id     = str(uuid.uuid4())[:8]

    for leg in combo:
        ticker    = leg['kalshi_ticker']
        side      = leg['side']           # 'yes' or 'no'
        price_c   = int(leg['kalshi_price'])  # cents
        # Number of contracts = cost / price per contract
        # Each contract costs price_c cents = price_c/100 dollars
        n_contracts = max(1, int(cost_per_leg / (price_c / 100)))

        yes_p = price_c if side == 'yes' else (100 - price_c)
        order = {
            'ticker':          ticker,
            'action':          'buy',
            'side':            side,
            'type':            'limit',
            'yes_price':       yes_p,
            'count':           n_contracts,
            'time_in_force':   'fill_or_kill',
            'client_order_id': str(uuid.uuid4()),
        }
        orders.append(order)

    # Place all orders
    filled = []
    failed = []
    for order in orders:
        try:
            result = kalshi.place_order(order)
            if result.get('status') in ('filled', 'executed'):
                filled.append(order['ticker'])
                logger.info(f"  ✓ Filled: {order['ticker']} {order['side']} {order['count']}x @ {order['yes_price']}c")
            else:
                failed.append(order['ticker'])
                logger.warning(f"  ✗ Not filled: {order['ticker']} status={result.get('status')}")
        except Exception as e:
            failed.append(order['ticker'])
            logger.error(f"  ✗ Order error: {order['ticker']}: {e}")

    if failed:
        logger.warning(f"Combo partially failed: {len(filled)} filled, {len(failed)} failed")
        return False

    return len(filled) == len(orders)


if __name__ == '__main__':
    run()
