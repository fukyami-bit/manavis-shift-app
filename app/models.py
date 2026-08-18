from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class Staff:
    name: str
    category: str  # "文" or "理"
    hourly_wage: int


@dataclass
class DayInfo:
    date: date
    weekday: str
    open_start: Optional[float]
    open_end: Optional[float]
    special_note: Optional[str]

    @property
    def day_type(self) -> str:
        if self.open_start is None:
            return "weekday"
        if self.open_start > 10:
            return "weekday"
        # 長期休暇でない日曜（20時閉館）は土日と異なるコマ区分を使う
        if self.weekday == "日" and self.open_end is not None and self.open_end <= 20.5:
            return "sunday_short"
        return "weekend"


@dataclass
class RequestEntry:
    staff: str
    date: date
    raw: str
    type: str  # "none" | "unavailable" | "full_day" | "range" | "unknown"
    start: Optional[float] = None
    end: Optional[float] = None
    tentative: bool = False
    note: Optional[str] = None

    def has_range(self) -> bool:
        return self.type in ("range", "full_day")


@dataclass
class Band:
    start: float
    end: float
    min_required: int
    label: str = ""
    max_required: Optional[int] = None


@dataclass
class ScheduleConfig:
    bands: dict  # day_type -> list[Band]
    budget: int


@dataclass
class Assignment:
    staff: str
    date: date
    start: float
    end: float
    tentative: bool
    wage: int

    @property
    def hours(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def cost(self) -> float:
        return self.hours * self.wage


@dataclass
class ScheduleResult:
    assignments: list  # list[Assignment]
    shortages: list  # list[dict]
    warnings: list  # list[str]
    total_cost: float
    budget: int
    staff_stats: dict  # name -> {"confirmed": int, "requested": int, "ratio": float}
