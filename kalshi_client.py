# kalshi_client.py
import time, base64, logging, requests
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

GAME_SERIES = [
    'kxmlbgame', 'kxnbagame', 'kxnhlgame', 'kxnflgame',
]


@dataclass
class KalshiContract:
    ticker:        str
    title:         str
    yes_price:     float
    no_price:      float
    yes_prob:      float
    volume:        int   = 0
    open_interest: int   = 0
    close_time:    datetime = field(default_factory=datetime.utcnow)


def _fix_pem(pem: str) -> str:
    pem = pem.strip()
    try:
        decoded = base64.b64decode(pem).decode('utf-8')
        if '-----BEGIN' in decoded:
            return decoded
    except Exception:
        pass
    pem = pem.replace('\\n', '\n')
    if '-----BEGIN' in pem:
        return pem
    body = pem.replace(' ', '').replace('\n', '')
    lines = '\n'.join(body[i:i+64] for i in range(0, len(body), 64))
    return f'-----BEGIN RSA PRIVATE KEY-----\n{lines}\n-----END RSA PRIVATE KEY-----'


class KalshiClient:
    def __init__(self, api_key_id: str, private_key_b64: str):
        self.api_key_id  = api_key_id
        self.session     = requests.Session()
        pem              = _fix_pem(private_key_b64)
        self.private_key = serialization.load_pem_private_key(
            pem.encode(), password=None, backend=default_backend()
        )
        logger.info("Kalshi client initialized")

    def _sign(self, ts: str, method: str, path: str) -> str:
        msg = f"{ts}{method}{path}"
        sig = self.private_key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            'KALSHI-ACCESS-KEY':       self.api_key_id,
            'KALSHI-ACCESS-SIGNATURE': self._sign(ts, method.upper(), path),
            'KALSHI-ACCESS-TIMESTAMP': ts,
            'Content-Type':            'application/json',
        }

    def _get(self, endpoint: str, params: dict = {}) -> dict:
        path = f"/trade-api/v2/{endpoint}"
        r = self.session.get(f"{BASE_URL}/{endpoint}", params=params, headers=self._headers('GET', path))
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, body: dict) -> dict:
        path = f"/trade-api/v2/{endpoint}"
        url  = f"{BASE_URL}/{endpoint}"
        headers = self._headers('POST', path)
        r = self.session.post(url, json=body, headers=headers)
        if r.status_code != 201:
            logger.error(f"POST {url} → {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    def get_series_markets(self, series: str) -> list:
        contracts = []
        cursor    = None
        while True:
            params = {'status': 'open', 'limit': 200, 'series_ticker': series.upper()}
            if cursor:
                params['cursor'] = cursor
            path = "/trade-api/v2/markets"
            r    = self.session.get(f"{BASE_URL}/markets", params=params, headers=self._headers('GET', path))
            r.raise_for_status()
            data    = r.json()
            markets = data.get('markets', [])
            for m in markets:
                try:
                    raw = m.get('yes_ask_dollars') or m.get('yes_bid_dollars') or m.get('last_price_dollars')
                    if not raw or float(raw) == 0:
                        continue
                    yes_price = float(raw) * 100
                    contracts.append(KalshiContract(
                        ticker        = m.get('ticker', ''),
                        title         = m.get('title', ''),
                        yes_price     = yes_price,
                        no_price      = 100 - yes_price,
                        yes_prob      = yes_price / 100,
                        volume        = m.get('volume', 0) or 0,
                        open_interest = m.get('open_interest', 0) or 0,
                    ))
                except Exception:
                    pass
            cursor = data.get('cursor')
            if not cursor or not markets:
                break
            time.sleep(0.3)
        return contracts

    def get_sports_markets(self) -> list:
        contracts = []
        for series in GAME_SERIES:
            try:
                c = self.get_series_markets(series)
                logger.info(f"Series {series}: {len(c)} contracts")
                contracts.extend(c)
            except Exception as e:
                logger.warning(f"Failed {series}: {e}")
            time.sleep(0.5)
        logger.info(f"Total: {len(contracts)} Kalshi contracts")
        return contracts

    def place_order(self, order: dict) -> dict:
        result = self._post('portfolio/orders', order)
        return result.get('order', {})

    def get_balance(self) -> float:
        data = self._get('portfolio/balance')
        return float(data.get('balance', 0)) / 100
