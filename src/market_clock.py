"""NYSE/NASDAQ 常规交易时段的市场时钟。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo


MarketState = Literal["OPEN", "CLOSED"]


class CalendarRangeError(ValueError):
    """requested time is outside the built-in calendar range."""


@dataclass(frozen=True)
class MarketSession:
    open_utc: datetime
    close_utc: datetime
    is_half_day: bool


class MarketClock:
    """美股（NYSE/NASDAQ 常规时段）的市场时钟。

    所有 UTC 时刻都由 ``America/New_York`` 的 09:30、16:00 或 13:00
    本地时间换算得到，因此会自然处理夏令时切换。
    """

    _new_york = ZoneInfo("America/New_York")
    _first_year = 2026
    _last_year = 2027

    # 来源：NYSE 官方 Holidays & Trading Hours 日历。
    # 仅覆盖 2026–2027，跨年使用需更新。
    _holidays: dict[date, str] = {
        date(2026, 1, 1): "New Year's Day",
        date(2026, 1, 19): "Martin Luther King Jr. Day",
        date(2026, 2, 16): "Presidents' Day",
        date(2026, 4, 3): "Good Friday",
        date(2026, 5, 25): "Memorial Day",
        date(2026, 6, 19): "Juneteenth",
        date(2026, 7, 3): "Independence Day (observed)",
        date(2026, 9, 7): "Labor Day",
        date(2026, 11, 26): "Thanksgiving Day",
        date(2026, 12, 25): "Christmas Day",
        date(2027, 1, 1): "New Year's Day",
        date(2027, 1, 18): "Martin Luther King Jr. Day",
        date(2027, 2, 15): "Presidents' Day",
        date(2027, 3, 26): "Good Friday",
        date(2027, 5, 31): "Memorial Day",
        date(2027, 6, 18): "Juneteenth (observed)",
        date(2027, 7, 5): "Independence Day (observed)",
        date(2027, 9, 6): "Labor Day",
        date(2027, 11, 25): "Thanksgiving Day",
        date(2027, 12, 24): "Christmas Day (observed)",
    }
    _half_days = {
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2027, 11, 26),
    }

    def state(self, t: datetime) -> MarketState:
        """返回给定时刻的常规市场状态。"""
        instant = self._as_utc(t)
        session = self._session_for_date(instant.astimezone(self._new_york).date())
        if session is None:
            return "CLOSED"
        if session.open_utc <= instant < session.close_utc:
            return "OPEN"
        return "CLOSED"

    def next_open(self, t: datetime) -> datetime:
        """返回严格晚于给定时刻的下一次开盘。"""
        instant = self._as_utc(t)
        local_date = instant.astimezone(self._new_york).date()
        for candidate in self._dates_from(local_date):
            session = self._session_for_date(candidate)
            if session is not None and session.open_utc > instant:
                return session.open_utc
        raise CalendarRangeError("built-in trading calendar cannot resolve the next open")

    def next_close(self, t: datetime) -> datetime:
        """返回严格晚于给定时刻的下一次收盘。"""
        instant = self._as_utc(t)
        local_date = instant.astimezone(self._new_york).date()
        for candidate in self._dates_from(local_date):
            session = self._session_for_date(candidate)
            if session is not None and session.close_utc > instant:
                return session.close_utc
        raise CalendarRangeError("built-in trading calendar cannot resolve the next close")

    def openings_between(self, t0: datetime, t1: datetime) -> int:
        """统计 ``(t0, t1]`` 区间内发生的开盘次数。"""
        start = self._as_utc(t0)
        end = self._as_utc(t1)
        if end < start:
            raise ValueError("interval end must not precede its start")

        first_date = start.astimezone(self._new_york).date()
        last_date = end.astimezone(self._new_york).date()
        count = 0
        candidate = first_date
        while candidate <= last_date:
            session = self._session_for_date(candidate)
            if session is not None and start < session.open_utc <= end:
                count += 1
            candidate += timedelta(days=1)
        return count

    def skipped_reason(self, t0: datetime, t1: datetime) -> str | None:
        """说明区间内跨过的节假日或周末；没有则返回 ``None``。"""
        start = self._as_utc(t0)
        end = self._as_utc(t1)
        if end < start:
            raise ValueError("interval end must not precede its start")

        first_date = start.astimezone(self._new_york).date()
        last_date = end.astimezone(self._new_york).date()
        reasons: list[str] = []
        candidate = first_date
        while candidate <= last_date:
            self._require_supported(candidate)
            holiday = self._holidays.get(candidate)
            if holiday is not None:
                reasons.append(f"{candidate.isoformat()} is {holiday}; US equity market closed")
            elif candidate.weekday() >= 5:
                reasons.append(f"{candidate.isoformat()} is a weekend; US equity market closed")
            candidate += timedelta(days=1)
        return "; ".join(reasons) if reasons else None

    def session_for_date(self, session_date: date) -> MarketSession | None:
        """返回纽约交易日期对应的常规时段，休市日返回 ``None``。"""
        return self._session_for_date(session_date)

    def _session_for_date(self, session_date: date) -> MarketSession | None:
        self._require_supported(session_date)
        if session_date.weekday() >= 5 or session_date in self._holidays:
            return None

        is_half_day = session_date in self._half_days
        close_time = time(13, 0) if is_half_day else time(16, 0)
        open_local = datetime.combine(
            session_date, time(9, 30), tzinfo=self._new_york
        )
        close_local = datetime.combine(session_date, close_time, tzinfo=self._new_york)
        return MarketSession(
            open_utc=open_local.astimezone(timezone.utc),
            close_utc=close_local.astimezone(timezone.utc),
            is_half_day=is_half_day,
        )

    def _dates_from(self, first_date: date):
        candidate = first_date
        while candidate.year <= self._last_year:
            self._require_supported(candidate)
            yield candidate
            candidate += timedelta(days=1)

    def _require_supported(self, value: date) -> None:
        if not self._first_year <= value.year <= self._last_year:
            raise CalendarRangeError(
                f"built-in trading calendar covers only {self._first_year}-{self._last_year}"
            )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(timezone.utc)
