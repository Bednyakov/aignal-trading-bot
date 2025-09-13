from utils import load_config
from .ml_trading_bot import MLTradingBot

cfg = load_config("config.json")
bot = MLTradingBot(cfg)