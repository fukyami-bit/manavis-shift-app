"""シフト自動生成エンジン。

方針:
- 基本は「希望を出した人はできるだけ全員採用する」。
- 希望は、その日の実際の開館時間の範囲内に丸める（開館時間を超えた記載
  ミスも自動補正される）。平日はアルバイトスタッフが17時以降のみ勤務する
  前提とし、14〜17時のコマは設けない。
- 各日・各時間帯の必要人数（下限）をハード制約として扱う。上限人数も
  設定でき、余裕があっても1つの時間帯に人が集中しすぎないようにする。
- 1人の連続勤務は隣接する最大2コマ（午前+午後 or 午後+夜）まで。朝から
  夜までの通し勤務にはしない。2コマのロングシフトは、その2コマがどちらも
  不足している場合にのみ使う。
- 1人の連勤は原則3連勤まで。人手が足りず外せない場合のみ超過を許容する。
- 文系のみ／理系のみの出勤は、いる場合はその日のうちに両方1名以上配置する
  ことを優先する。どうしても無理な場合は仕方ないが、2日連続にはしない。
- 予算・上限人数を超える場合は間引く。間引く際は「確定日数/希望日数」の
  比率が高いスタッフから優先的に外し、公平性を保つ。
- 希望者だけでは必要人数を満たせない日・2日連続の偏りを解消できない日は
  「不足」としてそのまま報告する（実在しない人員を作ることはできないため）。
"""
from __future__ import annotations

import datetime
from collections import defaultdict

from .models import Assignment, Band, DayInfo, RequestEntry, ScheduleResult, Staff

ONE_DAY = datetime.timedelta(days=1)
MIN_SHIFT_HOURS = 2.5  # これより短い勤務は割り当てない
MIN_GUARANTEED_DAYS = 4  # この日数以上希望した人は、可能な限りこの日数を確保する


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def default_bands() -> dict:
    # 平日はアルバイトスタッフが17時以降のみ勤務する前提のため、開館時刻
    # (14時)から17時までのコマは設けない。長期休暇でない日曜（20時閉館）は
    # 土日通常のコマ割りとは別に、専用のコマ区分を使う。
    return {
        "weekend": [
            Band(9, 13, 1, "9:00-13:00", max_required=2),
            Band(13, 18, 2, "13:00-18:00", max_required=3),
            Band(18, 21.75, 2, "18:00-21:45", max_required=3),
        ],
        "sunday_short": [
            Band(9, 12, 1, "9:00-12:00", max_required=2),
            Band(12, 16, 2, "12:00-16:00", max_required=3),
            Band(16, 20, 2, "16:00-20:00", max_required=3),
        ],
        "weekday": [
            Band(17, 21.75, 2, "17:00-21:45", max_required=3),
        ],
    }


def _expand_range(entry: RequestEntry, day: DayInfo):
    """希望をその日の実際の開館時間の範囲内に丸めて返す。開館時間より
    後ろの時刻を書き間違えている場合（例: 通常20時閉館の日曜に21:45まで
    申請している等）も、ここで自動的に補正される。"""
    if day.open_start is None or day.open_end is None:
        return None
    if entry.type == "full_day":
        return day.open_start, day.open_end
    if entry.type == "range" and entry.start is not None and entry.end is not None:
        s = max(entry.start, day.open_start)
        e = min(entry.end, day.open_end)
        if s >= e:
            return None
        return s, e
    return None


def compute_cost_from_shift(
    staff_list: list[Staff],
    days: list[DayInfo],
    requests: list[RequestEntry],
) -> ScheduleResult:
    """すでに確定・手直し済みのシフト表を読み取り、シフトの組み直しはせず
    そのまま人件費だけを集計する（修正後の人件費確認用）。"""
    staff_by_name = {s.name: s for s in staff_list}
    days_by_date = {d.date: d for d in days}

    assignments = []
    for r in requests:
        if not r.has_range():
            continue
        day = days_by_date.get(r.date)
        if day is None:
            continue
        rng = _expand_range(r, day)
        if rng is None:
            continue
        s, e = rng
        staff = staff_by_name.get(r.staff)
        if staff is None:
            continue
        assignments.append(Assignment(staff=r.staff, date=r.date, start=s, end=e, tentative=r.tentative, wage=staff.hourly_wage))
    assignments.sort(key=lambda a: (a.date, a.start, a.staff))

    staff_stats = {}
    total_cost = 0.0
    for staff in staff_list:
        staff_assignments = [a for a in assignments if a.staff == staff.name]
        hours = sum(a.hours for a in staff_assignments)
        cost = hours * staff.hourly_wage
        total_cost += cost
        staff_stats[staff.name] = {
            "confirmed": len(staff_assignments),
            "requested": len(staff_assignments),
            "ratio": 1.0 if staff_assignments else None,
            "hours": hours,
            "cost": cost,
        }

    return ScheduleResult(
        assignments=assignments,
        shortages=[],
        warnings=[],
        total_cost=total_cost,
        budget=0,
        staff_stats=staff_stats,
    )


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

    warnings = []

    def day_bands(day: DayInfo):
        return bands.get(day.day_type, [])

    def choose_bands(overlapped, day_bands_list, current_counts):
        """複数コマにまたがる希望を、実際に必要な分だけに絞り込む。
        隣接する2コマの両方が不足していればロングシフトとしてその2つを、
        そうでなければ最も不足している1コマだけを選ぶ。不足が無ければ、
        特定のコマに人が集中しないよう上限に余裕があるコマを優先し、
        それでも並んだ場合は時間の遅いコマを優先する"""
        deficits = {i: max(0, day_bands_list[i].min_required - current_counts[i]) for i in overlapped}
        for i in overlapped:
            if (i + 1) in deficits and deficits[i] > 0 and deficits[i + 1] > 0:
                return (i, i + 1)

        def room(i):
            band = day_bands_list[i]
            if band.max_required is None:
                return 999
            return band.max_required - current_counts[i]

        best_i = max(overlapped, key=lambda i: (deficits[i], room(i), i))
        return (best_i,)

    # 初期案: 有効な希望を出した人は全員採用。ただし1人の連続勤務は
    # 実際に必要な分（原則1コマ、隣接する2コマがどちらも不足している
    # 場合のみ2コマ分のロングシフト）に絞り込み、必要のない早い時間から
    # の勤務や朝から夜までの通し勤務にはしない。
    # 複数コマにまたがる希望が競合したときは、既にその月で確定した日数が
    # 少ない人を優先することで、開館担当などが特定の人に偏らないようにする。
    assigned = {}  # (staff, date) -> (RequestEntry, (start,end))
    days_assigned_so_far = defaultdict(int)
    for day in days:
        b = day_bands(day)
        direct = []
        multi = []
        counts = [0] * len(b)

        def _too_short(new_s, new_e, band_lo, band_hi):
            """短すぎる勤務かどうかを判定する。ただし、コマ自体がその日の
            開館時間の都合で短い場合（例: 早く閉まる日曜の夜コマ）は、
            希望者がそのコマを丸ごとカバーしているなら除外しない。"""
            duration = new_e - new_s
            if duration >= MIN_SHIFT_HOURS:
                return False
            full_len = min(band_hi, day.open_end) - max(band_lo, day.open_start)
            return duration < full_len - 1e-9

        for r in requests_by_day.get(day.date, []):
            rng = _expand_range(r, day)
            if rng is None:
                continue
            s, e = rng
            overlapped = [i for i, band in enumerate(b) if _overlaps(s, e, band.start, band.end)]
            if not overlapped:
                # どのコマにも重ならない希望（例: 平日14-17時のみの希望）は対象外
                continue
            elif len(overlapped) == 1:
                i = overlapped[0]
                new_s, new_e = max(s, b[i].start), min(e, b[i].end)
                if _too_short(new_s, new_e, b[i].start, b[i].end):
                    continue
                direct.append((r, (new_s, new_e)))
                counts[i] += 1
            else:
                multi.append((r, overlapped, s, e))

        # 開館直後（最初のコマの開始時刻）から実際に入れる人を優先的に処理する。
        # そうしないと、後から来る人の希望が先に「開館コマ充足」とカウントされて
        # しまい、本当は開館から入れる人が後回しにされてしまう。
        def _multi_sort_key(item):
            _r, _overlapped, item_s, _e = item
            can_open = bool(b) and item_s <= b[0].start
            return (0 if can_open else 1, days_assigned_so_far[item[0].staff], item[0].staff)

        for r, overlapped, s, e in sorted(multi, key=_multi_sort_key):
            chosen = choose_bands(overlapped, b, counts)
            # コマの境界時刻ではなく、実際の希望時間とコマ範囲の重なりに絞る
            new_s = max(s, b[chosen[0]].start)
            new_e = min(e, b[chosen[-1]].end)
            if _too_short(new_s, new_e, b[chosen[0]].start, b[chosen[-1]].end):
                continue
            direct.append((r, (new_s, new_e)))
            for i in chosen:
                counts[i] += 1

        for r, rng in direct:
            assigned[(r.staff, day.date)] = (r, rng)
            days_assigned_so_far[r.staff] += 1

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
    day_index = {d.date: i for i, d in enumerate(days)}

    def is_adjacent(date_a, date_b) -> bool:
        return abs((date_b - date_a).days) == 1

    def would_create_same_category_streak(date_, resulting_categories) -> bool:
        """指定日のカテゴリ構成が resulting_categories になったとき、
        前後の日と2日連続の同一カテゴリのみになってしまわないかを確認する"""
        if len(resulting_categories) != 1:
            return False
        only_cat = next(iter(resulting_categories))
        idx = day_index[date_]
        for neighbor_idx in (idx - 1, idx + 1):
            if 0 <= neighbor_idx < len(days):
                neighbor_day = days[neighbor_idx]
                if not is_adjacent(date_, neighbor_day.date):
                    continue
                _counts, neighbor_categories = coverage(neighbor_day)
                if neighbor_categories == {only_cat}:
                    return True
        return False

    def is_safe_to_remove(sname, d, s, e) -> bool:
        """このスタッフのこの日の割当を外しても、時間帯の必要人数・文理の
        在籍・文理2日連続ルールのいずれも壊さないかを判定する"""
        day = days_by_date[d]
        band_counts, categories = coverage(day, exclude_key=(sname, d))
        staff = staff_by_name.get(sname)
        if not staff:
            return True
        for i, b in enumerate(day_bands(day)):
            if _overlaps(s, e, b.start, b.end) and band_counts[i] < b.min_required:
                return False
        if staff.category not in categories:
            # このスタッフを外すと当該カテゴリがその日からいなくなる
            return False
        if would_create_same_category_streak(d, categories):
            return False
        return True

    def confirmed_count():
        c = defaultdict(int)
        for (sname, _d) in assigned.keys():
            c[sname] += 1
        return c

    def removal_priority(sname, conf):
        """間引き候補の優先順位を返す（大きいほど先に外してよい）。
        希望日数が少ない人（MIN_GUARANTEED_DAYS以下しか希望していない人を
        除く）が最低保証日数を下回るような削除は、他に選択肢がない限り
        後回しにする。"""
        req = requested_count[sname]
        c = conf[sname]
        under_floor = req >= MIN_GUARANTEED_DAYS and c <= MIN_GUARANTEED_DAYS
        ratio = c / req if req else 0
        return (0 if under_floor else 1, ratio)

    # 連勤上限（原則3連勤まで）。人手が足りず外せない場合のみ超過を許容する。
    MAX_CONSECUTIVE_DAYS = 3
    streak_len = defaultdict(int)
    prev_date = None
    for day in days:
        if prev_date is not None and (day.date - prev_date).days != 1:
            streak_len.clear()
        prev_date = day.date

        working_today = [sname for (sname, d) in list(assigned.keys()) if d == day.date]
        for sname in working_today:
            if streak_len[sname] + 1 > MAX_CONSECUTIVE_DAYS:
                r, (s, e) = assigned[(sname, day.date)]
                if is_safe_to_remove(sname, day.date, s, e):
                    del assigned[(sname, day.date)]
                    continue
            streak_len[sname] += 1
        for sname in staff_by_name:
            if (sname, day.date) not in assigned:
                streak_len[sname] = 0

    # 時間帯ごとの上限人数（過剰配置の防止）。予算に余裕があっても、
    # 必要以上の人数が1つの時間帯に集中しないようにする。
    for day in days:
        b = day_bands(day)
        for i, band in enumerate(b):
            if band.max_required is None:
                continue
            while True:
                band_counts, _categories = coverage(day)
                if band_counts[i] <= band.max_required:
                    break
                conf = confirmed_count()
                removable = []
                for (sname, d), (r, (s, e)) in assigned.items():
                    if d != day.date or not _overlaps(s, e, band.start, band.end):
                        continue
                    if is_safe_to_remove(sname, d, s, e):
                        removable.append(((sname, d), removal_priority(sname, conf)))
                if not removable:
                    break
                removable.sort(key=lambda x: x[1], reverse=True)
                del assigned[removable[0][0]]

    # 不足チェック（希望者だけでは満たせない枠）
    shortages = []
    for day in days:
        band_counts, _categories = coverage(day)
        db = day_bands(day)
        for i, b in enumerate(db):
            if band_counts[i] < b.min_required:
                shortages.append({
                    "date": day.date,
                    "band": b.label,
                    "required": b.min_required,
                    "available": band_counts[i],
                    "message": f"{b.label} が {band_counts[i]}/{b.min_required}名",
                })
        # 開館直後（その日の最初のコマの開始時刻）に誰も出勤していない場合は
        # 単純な人数カウントでは拾えないため、別途チェックする。土日はAAが
        # 開館対応をするため必須。平日は17時ちょうどでなくてもよい。
        if db and day.day_type in ("weekend", "sunday_short"):
            opening_band = db[0]
            anyone_at_open = any(
                d == day.date and s <= opening_band.start
                for (_sname, d), (_r, (s, _e)) in assigned.items()
            )
            if not anyone_at_open:
                shortages.append({
                    "date": day.date,
                    "band": "開館時",
                    "required": 1,
                    "available": 0,
                    "message": f"開館時刻（{opening_band.start:g}時）に出勤している人がいません",
                })

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
                staff = staff_by_name.get(sname)
                if not staff:
                    continue
                if is_safe_to_remove(sname, d, s, e):
                    removable.append(((sname, d), removal_priority(sname, conf), staff.hourly_wage))

            if not removable:
                warnings.append(f"予算超過: 必要人数を維持したままではこれ以上削減できません（残り超過額 約{int(cost - budget):,}円）")
                break

            removable.sort(key=lambda x: (x[1], x[2]), reverse=True)
            key_to_remove = removable[0][0]
            _r, (s, e) = assigned[key_to_remove]
            staff = staff_by_name.get(key_to_remove[0])
            cost -= max(0.0, e - s) * staff.hourly_wage
            del assigned[key_to_remove]

    # 文系のみ／理系のみの出勤が2日連続していないかの最終チェック
    # （希望者側にそもそも該当カテゴリがいない場合は間引きでは解消できないため報告のみ）
    for day in days:
        _counts, categories = coverage(day)
        if len(categories) != 1:
            continue
        only_cat = next(iter(categories))
        idx = day_index[day.date]
        next_idx = idx + 1
        if next_idx < len(days) and is_adjacent(day.date, days[next_idx].date):
            _next_counts, next_categories = coverage(days[next_idx])
            if next_categories == {only_cat}:
                shortages.append({
                    "date": day.date,
                    "band": f"{only_cat}系のみ2日連続",
                    "required": 1,
                    "available": 0,
                    "message": f"{day.date.strftime('%m/%d')}〜{days[next_idx].date.strftime('%m/%d')}が{only_cat}系スタッフのみになっています（希望者に他方の系統がいません）",
                })

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
