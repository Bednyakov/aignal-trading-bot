"""
Модуль для работы с API биржи OKX.

Реализует базовые методы для:
- получения исторических данных (OHLCV)
- получения текущих цен
- выставления и отмены ордеров
- получение баланса

Реализован через REST API с использованием requests.
Можно доработать под WebSocket для стриминга.
"""

import requests
import time
import hmac
import hashlib
import base64
import json
from datetime import datetime, timezone


def iso_to_millis(iso_str):
    return int(datetime.fromisoformat(iso_str).timestamp() * 1000)

class OkxClient:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, base_url: str = "https://www.okx.com"):
        self.api_key = api_key
        self.secret_key = secret_key.encode()
        self.passphrase = passphrase
        self.base_url = base_url.rstrip('/')

    def _get_timestamp(self) -> str:
        now = datetime.utcnow()
        return now.isoformat(timespec='milliseconds') + "Z"

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(self.secret_key, message.encode(), hashlib.sha256)
        d = mac.digest()
        return base64.b64encode(d).decode()

    def _headers(self, method: str, request_path: str, body: dict = None) -> dict:
        timestamp = self._get_timestamp()
        body_str = json.dumps(body) if body else ""
        sign = self._sign(timestamp, method, request_path, body_str)
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }

    def get_candles(self, symbol: str, timeframe: str = "1H", limit: int = 200) -> list:
        """
        Получить исторические свечи (OHLCV) по символу.
        :param symbol: например "BTC-USDT"
        :param timeframe: "1m", "1h", "1d" и т.п.
        :param limit: количество свечей (макс 200)
        :return: список свечей (timestamp, open, high, low, close, volume)
        """
        path = f"/api/v5/market/candles?instId={symbol}&bar={timeframe}&limit={limit}"
        url = self.base_url + path
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0":
            return data.get("data", [])
        else:
            raise Exception(f"OKX API error: {data}")
        

    def get_account_balance(self, currency: str = "USDT") -> float:
        """
        Получить баланс указанной валюты на счете.
        """
        path = "/api/v5/account/balance"
        url = self.base_url + path
        headers = self._headers("GET", path)
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0":
            for item in data.get("data", []):
                for detail in item.get("details", []):
                    if detail.get("ccy") == currency:
                        return float(detail.get("availBal", 0))
            return 0.0
        else:
            raise Exception(f"OKX API error: {data}")

    def place_order(self, symbol: str, side: str, price: float, quantity: float, order_type: str = "limit") -> dict:
        """
        Создать ордер.
        :param symbol: торговая пара, например "BTC-USDT"
        :param side: "buy" или "sell"
        :param price: цена ордера
        :param quantity: количество (в базовой валюте)
        :param order_type: "limit" или "market"
        :return: ответ API с информацией об ордере
        """
        path = "/api/v5/trade/order"
        url = self.base_url + path
        body = {
            "instId": symbol,
            "tdMode": "cash",       # обязательно для спота. Для деривативов, маржи, фьючерсов → "cross" или "isolated", нужно выносить в аргументы функции.
            "side": side,
            "ordType": order_type,
            "sz": str(quantity)
        }
        if order_type == "limit":
            body["px"] = str(price)
        # Удаляем ключ с None
        body = {k: v for k, v in body.items() if v is not None}
        headers = self._headers("POST", path, body)
        resp = requests.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0":
            return data.get("data", [{}])[0]
        else:
            raise Exception(f"OKX API error: {data}")

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """
        Отменить ордер по ID.
        """
        path = "/api/v5/trade/cancel-order"
        url = self.base_url + path
        body = {
            "instId": symbol,
            "ordId": order_id
        }
        headers = self._headers("POST", path, body)
        resp = requests.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "0":
            return data.get("data", [{}])[0]
        else:
            raise Exception(f"OKX API error: {data}")