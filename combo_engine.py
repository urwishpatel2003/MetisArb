# combo_engine.py
"""
Core combo logic:
1. Find EV legs — where Kalshi price < fair Pinnacle implied prob
2. Build 4-leg combos across different games
3. Calculate fair parlay odds and Kalshi combo payout
4. Return combos sorted by gap (Kalshi - fair)
"""

import re
import logging
from itertools import combinations
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

SPORT_SHORT = {
    'baseball_mlb':         'MLB',
    'basketball_nba':       'NBA',
    'icehockey_nhl':        'NHL',
    'americanfootball_nfl': 'NFL',
}


def implied_prob(american: int) -> float:
    if american < 0:
        return abs(american) / (abs(american) + 100)
    return 100 / (american + 100)


def remove_vig(odds1: int, odds2: int) -> tuple:
    p1 = implied_prob(odds1)
    p2 = implied_prob(odds2)
    total = p1 + p2
    return p1 / total, p2 / total


def american_odds(prob: float) -> int:
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return round(-prob * 100 / (1 - prob))
    return round(100 / prob - 100)


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match_kalshi_contract(sharp_line, kalshi_contracts: list) -> Optional[dict]:
    """
    Find the Kalshi winner contract matching a sharp moneyline outcome.
    Returns the best matching contract dict or None.
    """
    outcome_l = sharp_line.outcome.lower()
    words     = [w for w in outcome_l.split() if len(w) > 3]

    best, bs = None, 0.60
    for c in kalshi_contracts:
        title_l = c.title.lower()
        # Only look at winner/game contracts
        if 'winner' not in title_l and 'wins' not in title_l:
            continue
        word_hit = any(w in title_l for w in words)
        score    = max(sim(outcome_l, title_l), 0.80 if word_hit else 0.0)
        if score > bs:
            bs   = score
            best = c

    return best


def find_ev_legs(sharp_lines: list, kalshi_contracts: list) -> list:
    """
    Find legs where:
    - Sharp implied prob is clearly > Kalshi price (edge exists)
    - Outcome is a heavy favorite (-150 to -500 range)
    - Kalshi has a matching liquid contract
    """
    ev_legs     = []
    seen_events = set()

    # Group h2h lines by game
    h2h_lines = [l for l in sharp_lines if l.market_key == 'h2h']

    # Pair outcomes by game and remove vig
    games = {}
    for line in h2h_lines:
        key = (line.away_team, line.home_team, line.sport)
        if key not in games:
            games[key] = []
        games[key].append(line)

    for (away, home, sport), lines in games.items():
        if len(lines) < 2:
            continue

        # Get both sides and remove vig
        try:
            l1, l2 = lines[0], lines[1]
            fp1, fp2 = remove_vig(l1.american_odds, l2.american_odds)
        except Exception:
            continue

        for line, fair_prob in [(l1, fp1), (l2, fp2)]:
            # Only heavy favorites: -150 to -500
            fair_am = american_odds(fair_prob)
            if fair_am > -150 or fair_am < -500:
                continue

            # Find matching Kalshi contract
            kalshi_c = match_kalshi_contract(line, kalshi_contracts)
            if not kalshi_c:
                continue

            kalshi_price = kalshi_c.yes_price  # cents
            kalshi_prob  = kalshi_price / 100.0

            # Sanity check: heavy favorites (-150 to -500) should be 60-83c on Kalshi
            # If Kalshi price < 55c for a -150 to -500 favorite, it is a wrong match
            if kalshi_price < 55:
                continue

            # Edge: Kalshi price < fair prob (Kalshi underpricing this favorite)
            edge = (fair_prob - kalshi_prob) / kalshi_prob * 100
            if edge < 3:
                continue

            sport_s = SPORT_SHORT.get(sport, sport.upper())
            event   = f"{away} @ {home}"

            # Skip duplicate events
            event_key = event.lower()
            if event_key in seen_events:
                continue
            seen_events.add(event_key)

            ev_legs.append({
                'display':       f"{line.outcome} ML ({sport_s})",
                'kalshi_ticker': kalshi_c.ticker,
                'kalshi_price':  kalshi_price,
                'kalshi_prob':   kalshi_prob,
                'fair_prob':     fair_prob,
                'fair_american': fair_am,
                'sharp_american':line.american_odds,
                'side':          'yes',
                'sport':         sport,
                'event':         event,
                'edge_pct':      edge,
            })
            logger.info(
                f"  EV LEG: {line.outcome} ML ({sport_s}) | "
                f"Kalshi {kalshi_price:.0f}c | Fair {fair_prob:.1%} ({fair_am:+d}) | "
                f"Edge {edge:.1f}% | {event}"
            )

    ev_legs.sort(key=lambda x: x['edge_pct'], reverse=True)
    return ev_legs


def build_combos(ev_legs: list, n_legs: int = 4, max_combos: int = 500) -> list:
    """
    Build n-leg combos from EV legs across different games.
    Returns combos sorted by gap (Kalshi payout - fair payout), descending.
    """
    combos = []
    count  = 0

    for combo in combinations(ev_legs, n_legs):
        if count >= max_combos:
            break
        count += 1

        legs = list(combo)

        # Must be different games
        if len(set(leg['event'] for leg in legs)) != n_legs:
            continue

        fair_am   = calc_fair_parlay(legs)
        kalshi_am = calc_kalshi_payout(legs)
        gap       = kalshi_am - fair_am

        combos.append({
            'legs':      legs,
            'fair_am':   fair_am,
            'kalshi_am': kalshi_am,
            'gap':       gap,
        })

    combos.sort(key=lambda x: x['gap'], reverse=True)

    # Return flat list of leg lists for the caller
    return [c['legs'] for c in combos]


def calc_fair_parlay(legs: list) -> int:
    """True fair parlay odds from Pinnacle implied probs."""
    prob = 1.0
    for leg in legs:
        prob *= leg['fair_prob']
    return american_odds(prob)


def calc_kalshi_payout(legs: list) -> int:
    """What Kalshi pays: multiply individual decimal odds."""
    dec = 1.0
    for leg in legs:
        dec *= 100.0 / leg['kalshi_price']
    if dec >= 2.0:
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))
