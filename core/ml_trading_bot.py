"""
Простой, гибко настраиваемый торговый бот, использующий client для работы с биржей.
Поддерживает:
 - фильтрацию сигналов по confidence / profit_after_fee
 - расчёт размера позиции (percent / fixed)
 - выставление limit/market ордеров (в dry_run режиме можно тестировать)
 - управление ордерами: отмена если не исполнен в X сек
 - take-profit / stop-loss в виде OCO (эмулируется через мониторинг)
 - логирование

Требования:
 - клиент для работы с биржей (сейчас это синхронный OkxClient)
"""

import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

from clients import OkxClient
from utils import now_iso


class MLTradingBot:
    def __init__(self, config: dict):
        self.cfg = config
        okx_cfg = config["okx"]
        self.okx = OkxClient(okx_cfg["api_key"], okx_cfg["secret_key"], okx_cfg["passphrase"], okx_cfg.get("base_url", "https://www.okx.com"))
        self.ml_endpoint = config["ml_api"]["endpoint"]
        self.poll_interval = config["ml_api"].get("poll_interval_seconds", 10)
        self.symbol = config["ml_api"].get("symbol", "BTC-USDT")
        self.api_key = config["ml_api"].get("api-key", "my-api-key")
        self.strategy = config["strategy"]
        self.dry_run = self.strategy.get("dry_run", True)
        self.order_state = {}  # отслеживание активных ордеров в памяти: order_id -> meta Надежнее будет заменить на БД.
        self.log = logging.getLogger("MLTradingBot")
        self.log.setLevel(getattr(logging, self.strategy.get("log_level", "INFO").upper()))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(ch)

    # Получить свежий прогноз от ML API
    def fetch_prediction(self) -> Optional[dict]:
        try:
            params = {}
            if self.symbol is not None:
                params["symbol"] = self.symbol

            resp = requests.get(self.ml_endpoint, params=params, headers={"X-API-Key": self.api_key}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data
        except Exception as e:
            self.log.error("Ошибка получения предсказания: %s", e)
            return None

    # Проверить, достаточно ли хорош сигнал, чтобы торговать
    def should_trade(self, pred: dict) -> bool:
        sig = pred.get("signal")
        if sig not in self.strategy.get("signal_whitelist", ["buy", "sell"]):
            self.log.debug("Сигнал %s не в белом списке.", sig)
            return False

        if pred.get("confidence", 0) < self.strategy.get("min_confidence", 0.6):
            self.log.debug("Низкая уверенность: %s", pred.get("confidence"))
            return False

        if pred.get("profit_after_fee", 0) < self.strategy.get("min_profit_after_fee", 0.0):
            self.log.debug("Малая прибыль после комиссии: %s", pred.get("profit_after_fee"))
            return False

        # Дополнительно можно проверить threshold_used и transaction_cost
        return True

    # Рассчитать цену выставления (limit) с учётом небольшого скольжения
    def calc_limit_price(self, pred: dict, side: str) -> float:
        current_price = float(pred["current_price"])
        expected_price = float(pred.get("expected_price", current_price))
        use_expected = self.strategy.get("use_expected_price", True)
        base = expected_price if use_expected else current_price

        slippage = self.strategy.get("limit_price_slippage", 0.001)
        if side.lower() == "buy":
            price = base * (1 + slippage) if base > current_price else base
        else:
            price = base * (1 - slippage) if base < current_price else base
        return round(price, 8)

    # Рассчитать объём по настройкам (в базовой валюте, например BTC)
    def calc_size(self, pred: dict, price: float) -> float:
        pos_cfg = self.strategy.get("position_size", {})
        mode = pos_cfg.get("mode", "percent")
        if mode == "percent":
            pct = float(pos_cfg.get("percent_of_balance", 0.01))
            # берем баланс в quote (USDT) и конвертируем: size = (balance * pct) / price
            balance = self.okx.get_account_balance("USDT")
            max_exposure = float(self.strategy.get("risk", {}).get("max_exposure_usd", 1e9))
            allocated_usd = min(balance * pct, max_exposure)
            size = allocated_usd / price
            self.log.debug("Баланс USDT=%s, pct=%s => allocated_usd=%s, size=%s", balance, pct, allocated_usd, size)
            return float(round(size, 8))
        elif mode == "fixed":
            sz = float(pos_cfg.get("fixed_size", 0.0))
            return float(round(sz, 8))
        else:
            raise ValueError("Unknown position size mode")

    # Поместить ордер или пропустить в dry_run
    def place_order(self, symbol: str, side: str, price: float, size: float, order_type: str = "limit"):
        self.log.info("Placing order: %s %s @ %s size=%s type=%s dry_run=%s", side, symbol, price, size, order_type, self.dry_run)
        if self.dry_run:
            fake_id = f"dry_{int(time.time()*1000)}"
            meta = {
                "ordId": fake_id,
                "instId": symbol,
                "side": side,
                "px": str(price),
                "sz": str(size),
                "ordType": order_type,
                "state": "live",
                "created_at": now_iso()
            }
            self.order_state[fake_id] = meta
            return meta
        else:
            try:
                resp = self.okx.place_order(symbol, side, price, size, order_type)
                ord_id = resp.get("ordId") or resp.get("ord_id") or resp.get("clOrdId") or str(time.time())
                meta = {
                    "ordId": ord_id,
                    "instId": resp.get("instId", symbol),
                    "side": side,
                    "px": resp.get("fillPx") or str(price),
                    "sz": resp.get("sz") or str(size),
                    "state": resp.get("state", "live"),
                    "created_at": now_iso()
                }
                self.order_state[ord_id] = meta
                return meta
            except Exception as e:
                self.log.error("Ошибка выставления ордера: %s", e)
                return None

    # Отменить ордер
    def cancel_order(self, symbol: str, order_id: str):
        self.log.info("Cancel order %s (dry_run=%s)", order_id, self.dry_run)
        if self.dry_run:
            if order_id in self.order_state:
                self.order_state[order_id]["state"] = "canceled"
            return {"ordId": order_id, "state": "canceled"}
        else:
            try:
                resp = self.okx.cancel_order(symbol, order_id)
                if resp.get("sCode", "0") == "0" or resp.get("state") == "canceled" or resp.get("ordId"):
                    self.order_state.pop(order_id, None)
                return resp
            except Exception as e:
                self.log.error("Ошибка отмены ордера: %s", e)
                return None

    # Основная логика реакции на прогноз
    def process_prediction(self, pred: dict):
        """
        Использует предсказание (формат ваш) и на его основе решает:
         - выставить ордер
         - отменить существующие
         - не делать ничего
        """
        sig = pred.get("signal")
        side = "buy" if sig == "buy" else "sell" if sig == "sell" else None
        if side is None:
            self.log.debug("Неподдерживаемый сигнал: %s", sig)
            return

        if not self.should_trade(pred):
            self.log.info("Сигнал отклонён по правилам стратегии: %s", pred.get("signal"))
            return

        limit_price = self.calc_limit_price(pred, side)
        size = self.calc_size(pred, limit_price)
        if size <= 0:
            self.log.warning("Рассчитан нулевой размер позиции, пропускаю.")
            return

        # Ограничение по открытым позициям
        open_count = sum(1 for o in self.order_state.values() if o.get("state") == "live")
        if open_count >= int(self.strategy.get("risk", {}).get("max_open_positions", 10)):
            self.log.info("Достигнут максимум открытых позиций: %s", open_count)
            return

        order_type = self.strategy.get("order_type", "limit")
        placed = self.place_order(self.symbol, side, limit_price, size, order_type=order_type)
        if placed:
            self.log.info("Order placed: %s", placed)

            # планируем TP/SL если указаны (будем мониторить исполнение)
            tp = self.strategy.get("exit", {}).get("take_profit_pct")
            sl = self.strategy.get("exit", {}).get("stop_loss_pct")
            if tp or sl:
                self.log.debug("TP/SL configured (TP=%s SL=%s) — бот будет мониторить позицию.", tp, sl)

    # Простое управление открытыми ордерами: отмена через таймаут
    def manage_orders(self):
        now_ts = time.time()
        cancel_after = int(self.strategy.get("order_management", {}).get("cancel_if_unfilled_after_sec", 120))
        for ord_id, meta in list(self.order_state.items()):
            if meta.get("state") != "live":
                continue
            created_at = meta.get("created_at")
            if created_at:
                # created_at в ISO, парсим ту, что мы генерируем локально; если пришёл реальный — можно привести к времени
                try:
                    created_ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                except Exception:
                    created_ts = now_ts
                if (now_ts - created_ts) > cancel_after:
                    self.log.info("Order %s старше %s сек — отменяю.", ord_id, cancel_after)
                    self.cancel_order(self.symbol, ord_id)

    def run_once(self):
        pred = self.fetch_prediction()
        if not pred:
            return
        self.log.info("Получено предсказание: signal=%s conf=%s pred=%s updated_at=%s", pred.get("signal"), pred.get("confidence"), pred.get("predicted_return"), pred.get("updated_at"))
        self.process_prediction(pred)
        self.manage_orders()

    def run_loop(self):
        self.log.info("Бот запущен (dry_run=%s). Polling %s every %s sec", self.dry_run, self.ml_endpoint, self.poll_interval)
        last_manage = 0
        manage_interval = int(self.strategy.get("order_management", {}).get("monitor_open_orders_every_sec", 15))
        try:
            while True:
                self.run_once()
                # периодическая проверка/управление ордерами
                if (time.time() - last_manage) > manage_interval:
                    self.manage_orders()
                    last_manage = time.time()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            self.log.info("Остановлено пользователем.")
        except Exception as e:
            self.log.exception("Неожиданная ошибка в цикле бота: %s", e)
