# odds_client.py
import requests, logging
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class OddsLine:
    book:         str
    market_key:   str
    sport:        str
    home_team:    str
    away_team:    str
    outcome:      str
    american_odds: int
    implied_prob: float
    raw_prob:     float
    timestamp:    datetime = field(default_factory=datetime.utcnow)


def american_to_prob(odds: int) -> float:
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


class OddsClient:
    def __init__(self, api_key: str):
        self.api_key  = api_key
        self.base_url = "https://api.the-odds-api.com/v4"

    def get_sharp_lines(self, sport: str, books: list) -> list:
        params = {
            'apiKey':      self.api_key,
            'sport':       sport,
            'regions':     'us',
            'markets':     'h2h',
            'oddsFormat':  'american',
            'bookmakers':  ','.join(books),
        }
        r = requests.get(f"{self.base_url}/sports/{sport}/odds", params=params)
        r.raise_for_status()
        lines = []
        for game in r.json():
            home = game.get('home_team', '')
            away = game.get('away_team', '')
            for bm in game.get('bookmakers', []):
                book = bm.get('key', '')
                for market in bm.get('markets', []):
                    if market.get('key') != 'h2h':
                        continue
                    outcomes = market.get('outcomes', [])
                    if len(outcomes) < 2:
                        continue
                    raw_probs = [american_to_prob(o['price']) for o in outcomes]
                    total     = sum(raw_probs)
                    for o, rp in zip(outcomes, raw_probs):
                        lines.append(OddsLine(
                            book=book, market_key='h2h', sport=sport,
                            home_team=home, away_team=away,
                            outcome=o['name'], american_odds=o['price'],
                            implied_prob=rp/total, raw_prob=rp,
                        ))
        logger.info(f"Fetched {len(lines)} lines for {sport}")
        return lines
