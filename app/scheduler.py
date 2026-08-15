"""シフト自動生成エンジン。

方針:
- 基本は「希望を出した人はできるだけ全員採用する」。
- 各日・各時間帯の必要人数（下限）と、文理各1名以上/日をハード制約として扱う。
- 予算を超える場合のみ、必要人数を満たす範囲で人員を間引く。間引く際は
  「確定日数/希望日数」の比率が高いスタッフから優先的に外し、公平性を保つ。
- 希望者だけでは必要人数を満たせない日は「不足」としてそのまま報告する
  （実在しない人員を作ることはできないため）。
"""
from __future__ import annotations

from collections import defaultdict

from .models import Assignment, Band, DayInfo, RequestEntry, ScheduleResult, Staff


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def default_bands() -> dict:
    return {
        "weekend": [
            Band(9, 13, 1, "9:00-13:00"),
            Band(13, 18, 2, "13:00-18:00"),
            Band(18, 21.75, 2, "18:00-21:45"),
        ],
        "weekday": [
            Band(14, 17, 1, "14:00-17:00"),
            Band(17, 21.75, 2, "17:00-21:45"),
        ],
    }


def _expand_range(entry: RequestEntry, day: DayInfo):
    if entry.type == "full_day":
        if day.open_start is None or day.open_end is None:
            return None
        return day.open_start, day.open_end
    if entry.type == "range" and entry.start is not None and entry.end is not None:
        return entry.start, entry.end
    return None


def generate_schedule(
    staff_list: list[Staff],
    days: list[DayInfo],
    requests: list[RequestEntry],
    bands: dict,
    budget: int,
) -> ScheduleResult:
    staff_by_name = {s.name: s for s in staff_list}
    requests_by_day = defaultdict(list)
    for r in requests:
        if r.has_range():
            requests_by_day[r.date].append(r)

    requested_count = defaultdict(int)
    for r in requests:
        if r.has_range():
            requested_count[r.staff] += 1

    # 初期案: 有効な希望を出した人は全員採用
    candidates = {}  # date -> list[(RequestEntry, (start,end))]
    for day in days:
        entries = []
        for r in requests_by_day.get(day.date, []):
            rng = _expand_range(r, day)
            if rng is not None:
                entries.append((r, rng))
        candidates[day.date] = entries

    assigned = {}  # (staff, date) -> (RequestEntry, (start,end))
    for day in days:
        for r, rng in candidates[day.date]:
            assigned[(r.staff, day.date)] = (r, rng)

    warnings = []

    def day_bands(day: DayInfo):
        return bands.get(day.day_type, [])

    def coverage(day: DayInfo, exclude_key=None):
        """その日の各バンドの現在の充足人数と、文理の在籍状況を返す"""
        band_counts = [0] * len(day_bands(day))
        categories = set()
        for (sname, d), (r, (s, e)) in assigned.items():
            if d != day.date or (sname, d) == exclude_key:
                continue
            staff = staff_by_name.get(sname)
            if staff:
                categories.add(staff.category)
            for i, b in enumerate(day_bands(day)):
                if _overlaps(s, e, b.start, b.end):
                    band_counts[i] += 1
        return band_counts, categories

    days_by_date = {d.date: d for d in days}

    # 不足チェック（希望者だけでは満たせない枠、文理どちらかが不在の日）
    shortages = []
    for day in days:
        band_counts, categories = coverage(day)
        for i, b in enumerate(day_bands(day)):
            if band_counts[i] < b.min_required:
                shortages.append({
                    "date": day.date,
                    "band": b.label,
                    "required": b.min_required,
                    "available": band_counts[i],
                })
        for cat in ("文", "理"):
            if cat not in categories and any(s.category == cat for s in staff_list):
                shortages.append({
                    "date": day.date,
                    "band": f"{cat}系スタッフ",
                    "required": 1,
                    "available": 0,
                })

    def confirmed_count():
        c = defaultdict(int)
        for (sname, _d) in assigned.keys():
            c[sname] += 1
        return c

    def total_cost():
        total = 0.0
        for (sname, d), (r, (s, e)) in assigned.items():
            staff = staff_by_name.get(sname)
            if staff:
                total += max(0.0, e - s) * staff.hourly_wage
        return total

    # 予算超過の場合は間引く
    cost = total_cost()
    if cost > budget:
        while cost > budget:
            conf = confirmed_count()
            removable = []
            for (sname, d), (r, (s, e)) in assigned.items():
                day = days_by_date[d]
                band_counts, categories = coverage(day, exclude_key=(sname, d))
                staff = staff_by_name.get(sname)
                if not staff:
                    continue
                ok = True
                for i, b in enumerate(day_bands(day)):
                    if _overlaps(s, e, b.start, b.end) and band_counts[i] < b.min_required:
                        ok = False
                        break
                if ok and staff.category not in categories:
                    # このスタッフを外すと当該カテゴリが日内から消える
                    ok = False
                if ok:
                    ratio = conf[sname] / requested_count[sname] if requested_count[sname] else 0
                    removable.append(((sname, d), ratio, staff.hourly_wage))

            if not removable:
                warnings.append(f"予算超過: 必要人数を維持したままではこれ以上削減できません（残り超過額 約{int(cost - budget):,}円）")
                break

            removable.sort(key=lambda x: (-x[1], -x[2]))
            key_to_remove = removable[0][0]
            _r, (s, e) = assigned[key_to_remove]
            staff = staff_by_name.get(key_to_remove[0])
            cost -= max(0.0, e - s) * staff.hourly_wage
            del assigned[key_to_remove]

    # 出力用アサインメント一覧
    assignments = []
    for (sname, d), (r, (s, e)) in assigned.items():
        staff = staff_by_name.get(sname)
        assignments.append(Assignment(staff=sname, date=d, start=s, end=e, tentative=r.tentative, wage=staff.hourly_wage))
    assignments.sort(key=lambda a: (a.date, a.start, a.staff))

    conf = confirmed_count()
    staff_stats = {}
    for staff in staff_list:
        req = requested_count[staff.name]
        c = conf[staff.name]
        staff_stats[staff.name] = {
            "confirmed": c,
            "requested": req,
            "ratio": (c / req) if req else None,
        }

    return ScheduleResult(
        assignments=assignments,
        shortages=shortages,
        warnings=warnings,
        total_cost=total_cost(),
        budget=budget,
        staff_stats=staff_stats,
    )
