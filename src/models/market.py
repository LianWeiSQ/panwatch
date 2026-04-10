from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo


class MarketCode(str, Enum):
    CN = "CN"  # A股
    HK = "HK"  # 港股
    US = "US"  # 美股
    CN_FUT = "CN_FUT"  # 国内期货
    CN_OPT = "CN_OPT"  # 国内期权


@dataclass
class TradingSession:
    """一个交易时段"""
    start: time
    end: time


@dataclass
class MarketDef:
    """市场定义"""
    code: MarketCode
    name: str
    timezone: str
    sessions: list[TradingSession]
    symbol_pattern: str  # 正则，用于校验股票代码格式

    def get_tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_trading_time(self, dt: datetime | None = None) -> bool:
        """判断给定时间是否在交易时段内"""
        if dt is None:
            dt = datetime.now(self.get_tz())
        else:
            dt = dt.astimezone(self.get_tz())

        # 周末不交易
        if dt.weekday() >= 5:
            return False

        current_time = dt.time()
        def _in_session(session: TradingSession) -> bool:
            if session.start <= session.end:
                return session.start <= current_time <= session.end
            return current_time >= session.start or current_time <= session.end

        return any(_in_session(session) for session in self.sessions)


# 预定义市场
MARKETS: dict[MarketCode, MarketDef] = {
    MarketCode.CN: MarketDef(
        code=MarketCode.CN,
        name="A股",
        timezone="Asia/Shanghai",
        sessions=[
            TradingSession(time(9, 30), time(11, 30)),
            TradingSession(time(13, 0), time(15, 0)),
        ],
        symbol_pattern=r"^[036]\d{5}$",
    ),
    MarketCode.HK: MarketDef(
        code=MarketCode.HK,
        name="港股",
        timezone="Asia/Hong_Kong",
        sessions=[
            TradingSession(time(9, 30), time(12, 0)),
            TradingSession(time(13, 0), time(16, 0)),
        ],
        symbol_pattern=r"^\d{5}$",
    ),
    MarketCode.US: MarketDef(
        code=MarketCode.US,
        name="美股",
        timezone="America/New_York",
        sessions=[
            TradingSession(time(9, 30), time(16, 0)),
        ],
        symbol_pattern=r"^[A-Z]{1,5}$",
    ),
    MarketCode.CN_FUT: MarketDef(
        code=MarketCode.CN_FUT,
        name="国内期货",
        timezone="Asia/Shanghai",
        sessions=[
            TradingSession(time(9, 0), time(10, 15)),
            TradingSession(time(10, 30), time(11, 30)),
            TradingSession(time(13, 30), time(15, 0)),
            TradingSession(time(21, 0), time(23, 0)),
        ],
        symbol_pattern=r"^[A-Z]{1,4}\d{0,4}$",
    ),
    MarketCode.CN_OPT: MarketDef(
        code=MarketCode.CN_OPT,
        name="国内期权",
        timezone="Asia/Shanghai",
        sessions=[
            TradingSession(time(9, 30), time(11, 30)),
            TradingSession(time(13, 0), time(15, 0)),
        ],
        symbol_pattern=r"^[A-Z0-9\-]{2,32}$",
    ),
}


@dataclass
class StockData:
    """标准化行情数据"""
    symbol: str
    name: str
    market: MarketCode
    current_price: float
    change_pct: float       # 涨跌幅 %
    change_amount: float    # 涨跌额
    volume: float           # 成交量（手）
    turnover: float         # 成交额（元）
    open_price: float
    high_price: float
    low_price: float
    prev_close: float
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class IndexData:
    """大盘指数数据"""
    symbol: str
    name: str
    market: MarketCode
    current_price: float
    change_pct: float
    change_amount: float
    volume: float
    turnover: float
    timestamp: datetime = field(default_factory=datetime.now)
