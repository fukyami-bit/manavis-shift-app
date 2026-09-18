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
- 時給がリーダー水準（既定1,500円以上）のスタッフは校舎運営上重要なため、
  間引きで優先的に保護し、週3日を目安に優先的に配置する。平日3人体制の
  時間短縮の対象からも外し、なるべく長め（1コマ分）に勤務してもらう。
- 希望者だけでは必要人数を満たせない日・2日連続の偏りを解消できない日は
  「不足」としてそのまま報告する（実在しない人員を作ることはできないため）。
"""
from __future__ import annotations

import datetime
import math
from collections import defaultdict

from .models import Assignment, Band, DayInfo, RequestEntry, ScheduleResult, Staff

ONE_DAY = datetime.timedelta(days=1)
MIN_SHIFT_HOURS = 2.5  # これより短い勤務は割り当てない
MIN_GUARANTEED_DAYS = 4  # この日数までの希望は、できる限りすべて通すことを目標にする
RATIO_TARGET_SLOPE = 0.4  # MIN_GUARANTEED_DAYSを超えた希望日数のうち、目標日数に上乗せする割合
LEADER_WAGE_THRESHOLD = 1500  # この時給以上のスタッフは「リーダー」として優先配置する
LEADER_MONTHLY_TARGET = 10  # リーダーに目指してほしい月あたりの勤務日数（これに届いたら以降は充足率を優先）
SMALL_REQUEST_THRESHOLD = 11  # この日数以下の希望者は、下の充足率を確実に達成させる
SMALL_REQUEST_RATIO_GUARANTEE = 0.6  # SMALL_REQUEST_THRESHOLD以下の希望者に確実に保証する充足率


def _is_leader(staff: Staff | None) -> bool:
    return staff is not None and staff.hourly_wage >= LEADER_WAGE_THRESHOLD


def _target_days(req: int) -> int:
    """希望日数に対する目標確定日数。希望が少ない人ほど充足率が高くなる
    よう、MIN_GUARANTEED_DAYSまでは全部、それを超えた分はRATIO_TARGET_SLOPE
    の割合だけ上乗せする右肩下がりの目標にする。"""
    if req <= 0:
        return 0
    if req <= MIN_GUARANTEED_DAYS:
        return req
    extra = req - MIN_GUARANTEED_DAYS
    return MIN_GUARANTEED_DAYS + math.ceil(extra * RATIO_TARGET_SLOPE)


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

    # 間引き前の候補一覧を保持しておく（週1回以上のペース確保のため、
    # 一度外れた候補を後で復活させることがある）
    all_candidates = dict(assigned)

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

    def is_safe_to_remove(sname, d, s, e, replacement_category=None) -> bool:
        """このスタッフのこの日の割当を外しても、時間帯の必要人数・文理の
        在籍・文理2日連続ルールのいずれも壊さないかを判定する。
        replacement_categoryに外す人と同じカテゴリを渡すと、代わりに
        同カテゴリの人を入れる前提として文理系のチェックをスキップする
        （入れ替えても在籍カテゴリの構成は変わらないため）。"""
        day = days_by_date[d]
        band_counts, categories = coverage(day, exclude_key=(sname, d))
        staff = staff_by_name.get(sname)
        if not staff:
            return True
        for i, b in enumerate(day_bands(day)):
            if _overlaps(s, e, b.start, b.end) and band_counts[i] < b.min_required:
                return False
        if replacement_category == staff.category:
            return True
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

    def total_cost():
        total = 0.0
        for (_sname, _d), (_r, (s, e)) in assigned.items():
            staff = staff_by_name.get(_sname)
            if staff:
                total += max(0.0, e - s) * staff.hourly_wage
        return total

    def removal_priority(sname, d, conf):
        """間引き候補の優先順位を返す（大きいほど先に外してよい）。
        希望日数が少ない人（MIN_GUARANTEED_DAYS以下しか希望していない人を
        除く）が最低保証日数を下回るような削除や、リーダー（時給が
        LEADER_WAGE_THRESHOLD以上）が週・月の目標日数を下回るような削除は、
        他に選択肢がない限り後回しにする。同着の場合はリーダー以外を
        優先的に外す。"""
        staff = staff_by_name.get(sname)
        req = requested_count[sname]
        c = conf[sname]
        under_floor = req > 0 and c <= _target_days(req)
        leader = _is_leader(staff)
        # リーダーの保護は月間の目標日数のみで判定する。月10回に届いたら
        # それ以上は特別扱いせず、他のスタッフと同じく充足率で判断する。
        leader_under_target = leader and c < LEADER_MONTHLY_TARGET
        protected = under_floor or leader_under_target
        ratio = c / req if req else 0
        # 充足率（ratio）を優先順位の主軸にする。リーダーかどうかは、
        # 充足率が同点のときにだけ働く最後のタイブレークにとどめる
        # （そうしないと、非リーダーは充足率に関係なく全員が先に
        # 削られる対象になってしまう）。
        non_leader_tiebreak = 0 if protected else (1 if not leader else 0)
        return (0 if protected else 1, ratio, non_leader_tiebreak)

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
                        removable.append(((sname, d), removal_priority(sname, d, conf)))
                if not removable:
                    break
                removable.sort(key=lambda x: x[1], reverse=True)
                del assigned[removable[0][0]]

    # 予算内に収まらない場合に限り、平日で3人体制になっている日の中から
    # 最も早く入る人の勤務時間を短縮してコストを抑える（早く来た人が
    # 早めに帰る形にする）。予算に余裕があるときは時間をずらさず、
    # 希望通りフルで入ってもらう。2人体制のときも変更しない。リーダー
    # （時給がLEADER_WAGE_THRESHOLD以上）は校舎運営上長めに入ってほしい
    # ため、他に短縮できる人がいる限り対象から外す。
    STAGGER_HOURS = 3.0
    if total_cost() > budget:
        for day in days:
            if total_cost() <= budget:
                break
            if day.day_type != "weekday":
                continue
            b = day_bands(day)
            if not b:
                continue
            band = b[0]
            members = [
                (sname, s, e) for (sname, d), (_r, (s, e)) in assigned.items()
                if d == day.date and _overlaps(s, e, band.start, band.end)
            ]
            if len(members) < 3:
                continue
            non_leader_members = [m for m in members if not _is_leader(staff_by_name.get(m[0]))]
            pool = non_leader_members if non_leader_members else members
            earliest_sname, s, e = min(pool, key=lambda x: x[1])
            new_e = min(e, s + STAGGER_HOURS)
            if new_e < e:
                r, _ = assigned[(earliest_sname, day.date)]
                assigned[(earliest_sname, day.date)] = (r, (s, new_e))

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
                    removable.append(((sname, d), removal_priority(sname, d, conf), staff.hourly_wage))

            if not removable:
                warnings.append(f"予算超過: 必要人数を維持したままではこれ以上削減できません（残り超過額 約{int(cost - budget):,}円）")
                break

            removable.sort(key=lambda x: (x[1], x[2]), reverse=True)
            key_to_remove = removable[0][0]
            _r, (s, e) = assigned[key_to_remove]
            staff = staff_by_name.get(key_to_remove[0])
            cost -= max(0.0, e - s) * staff.hourly_wage
            del assigned[key_to_remove]

    # 週1回以上のペースを保つ: 確定した勤務日の間隔が7日を超える場合、
    # 間引きで外れた候補の中から1つだけ復活させて空きすぎを防ぐ
    # （上限人数・連勤上限は守った上で行う）
    def is_safe_to_add(sname, d, s, e) -> bool:
        day = days_by_date[d]
        band_counts, _categories = coverage(day)
        for i, b in enumerate(day_bands(day)):
            if _overlaps(s, e, b.start, b.end) and b.max_required is not None and band_counts[i] >= b.max_required:
                return False
        idx = day_index[d]
        run = 1
        j = idx - 1
        while j >= 0 and is_adjacent(days[j].date, days[j + 1].date) and (sname, days[j].date) in assigned:
            run += 1
            j -= 1
        j = idx + 1
        while j < len(days) and is_adjacent(days[j - 1].date, days[j].date) and (sname, days[j].date) in assigned:
            run += 1
            j += 1
        return run <= MAX_CONSECUTIVE_DAYS

    def try_add_with_bump(sname, d, s, e, is_bumpable) -> bool:
        """このスタッフをこの日に追加できるか試す。上限人数だけが理由で
        入れない場合、is_bumpable(他のスタッフ名)がTrueを返す人が同じ
        コマにいれば、その人を1人外して代わりに入れる（充足率が高い人を
        優先して譲ってもらう）。それでも入れられなければ元に戻す。"""
        if is_safe_to_add(sname, d, s, e):
            r0 = all_candidates[(sname, d)][0]
            assigned[(sname, d)] = (r0, (s, e))
            return True

        day = days_by_date[d]
        band_counts, _categories = coverage(day)
        db = day_bands(day)
        blocked_bands = [
            i for i, b in enumerate(db)
            if _overlaps(s, e, b.start, b.end) and b.max_required is not None and band_counts[i] >= b.max_required
        ]
        if not blocked_bands:
            return False  # 上限人数以外の理由（連勤上限など）ではやり取りしない

        sname_staff = staff_by_name.get(sname)
        sname_category = sname_staff.category if sname_staff else None

        bumped = []
        for i in blocked_bands:
            b = db[i]
            candidates_to_bump = []
            for (n2, d2), (_r2, (s2, e2)) in assigned.items():
                if d2 != d or n2 == sname or not _overlaps(s2, e2, b.start, b.end):
                    continue
                if not is_bumpable(n2):
                    continue
                if not is_safe_to_remove(n2, d2, s2, e2, replacement_category=sname_category):
                    continue
                req2 = requested_count[n2]
                conf2 = sum(1 for (nn, _dd) in assigned if nn == n2)
                ratio2 = conf2 / req2 if req2 else 0
                candidates_to_bump.append((n2, ratio2))
            if not candidates_to_bump:
                for bn, bd in bumped:
                    assigned[(bn, bd)] = all_candidates[(bn, bd)]
                return False
            candidates_to_bump.sort(key=lambda x: -x[1])
            bump_name = candidates_to_bump[0][0]
            bumped.append((bump_name, d))
            del assigned[(bump_name, d)]

        if is_safe_to_add(sname, d, s, e):
            r0 = all_candidates[(sname, d)][0]
            assigned[(sname, d)] = (r0, (s, e))
            return True
        for bn, bd in bumped:
            assigned[(bn, bd)] = all_candidates[(bn, bd)]
        return False

    candidate_dates_by_staff = defaultdict(list)
    for (sname, d) in all_candidates:
        candidate_dates_by_staff[sname].append(d)

    # リーダー（時給がLEADER_WAGE_THRESHOLD以上）は校舎運営上重要なため最優先。
    # 間引きの結果に関わらず、月あたりの勤務日数がLEADER_MONTHLY_TARGETに
    # 届いていなければ、外れていた候補から優先的に復活させる。10回に
    # 届いたらそれ以上は特別扱いしない。
    def _not_leader(n2):
        return not _is_leader(staff_by_name.get(n2))

    for staff in staff_list:
        if not _is_leader(staff):
            continue
        sname = staff.name
        cand_dates = sorted(d for (n2, d) in all_candidates if n2 == sname)
        while True:
            confirmed_this_month = sum(1 for d in cand_dates if (sname, d) in assigned)
            if confirmed_this_month >= LEADER_MONTHLY_TARGET:
                break
            remaining = [d for d in cand_dates if (sname, d) not in assigned]
            if not remaining:
                break
            added = False
            for d in remaining:
                r, (s, e) = all_candidates[(sname, d)]
                if try_add_with_bump(sname, d, s, e, _not_leader):
                    added = True
                    break
            if not added:
                break

    # 月10回に届かないリーダーは、土日の既存シフトを可能な範囲で隣接コマへ
    # 伸ばし、ロングシフト化して日数の不足分を勤務時間で補う。
    for staff in staff_list:
        if not _is_leader(staff):
            continue
        sname = staff.name
        total_confirmed = sum(1 for (n2, _d2) in assigned if n2 == sname)
        if total_confirmed >= LEADER_MONTHLY_TARGET:
            continue
        for (n2, d2), (r, (s, e)) in list(assigned.items()):
            if n2 != sname:
                continue
            day = days_by_date[d2]
            if day.day_type == "weekday":
                continue  # 平日はコマが1つしかないため対象外
            b = day_bands(day)
            raw = _expand_range(r, day)
            if raw is None:
                continue
            raw_s, raw_e = raw
            overlapped = [i for i, band in enumerate(b) if _overlaps(raw_s, raw_e, band.start, band.end)]
            current_bands = sorted(i for i, band in enumerate(b) if _overlaps(s, e, band.start, band.end))
            if not current_bands:
                continue
            for i in overlapped:
                if i in current_bands:
                    continue
                if abs(i - current_bands[0]) != 1 and abs(i - current_bands[-1]) != 1:
                    continue  # 隣接コマでなければロングシフト化しない
                band_counts, _categories = coverage(day, exclude_key=(sname, d2))
                if b[i].max_required is not None and band_counts[i] >= b[i].max_required:
                    continue
                merged = sorted(current_bands + [i])
                new_s = max(min(s, raw_s), b[merged[0]].start)
                new_e = min(max(e, raw_e), b[merged[-1]].end)
                assigned[(sname, d2)] = (r, (new_s, new_e))
                break

    # 希望日数がSMALL_REQUEST_THRESHOLD以下の人には、
    # SMALL_REQUEST_RATIO_GUARANTEE(60%)を確実に達成させる。同じくらい
    # 困っている人同士では譲り合いが起きないことがあるため、この保証枠に
    # 限っては希望日数が多い人（非リーダー）から優先的に譲ってもらう。
    def _bumpable_for_small_guarantee(n2):
        if _is_leader(staff_by_name.get(n2)):
            return False
        return requested_count[n2] > SMALL_REQUEST_THRESHOLD

    for staff in staff_list:
        sname = staff.name
        req = requested_count[sname]
        if req == 0 or req > SMALL_REQUEST_THRESHOLD:
            continue
        target = math.ceil(req * SMALL_REQUEST_RATIO_GUARANTEE)
        cand_dates = sorted(d for (n2, d) in all_candidates if n2 == sname)
        while True:
            confirmed_now = sum(1 for d in cand_dates if (sname, d) in assigned)
            if confirmed_now >= target:
                break
            remaining = [d for d in cand_dates if (sname, d) not in assigned]
            if not remaining:
                break
            added = False
            for d in remaining:
                r, (s, e) = all_candidates[(sname, d)]
                if try_add_with_bump(sname, d, s, e, _bumpable_for_small_guarantee):
                    added = True
                    break
            if not added:
                break

    # 希望日数が少ない人ほど充足率が高くなるようにする（_target_days）。
    # 目標日数に届いていない場合は、外れていた候補から復活させて底上げ
    # する（連勤上限・上限人数などの安全確認はそのまま維持する）。上限
    # 人数だけがネックの場合は、リーダー・小口保証枠が優先されることで
    # 割を食う中間層が出ないよう、「今の自分より充足率が高い非リーダー」
    # からなら誰でも1人譲ってもらえるようにする（比較の公平性を優先）。
    def confirmed_ratio(n2):
        req2 = requested_count[n2]
        if req2 == 0:
            return 1.0
        conf2 = sum(1 for (nn, _dd) in assigned if nn == n2)
        return conf2 / req2

    # 充足率が低い人から順に処理することで、一番困っている人から優先的に
    # 埋めていく。
    ordered_staff = sorted(
        (s for s in staff_list if requested_count[s.name] > 0),
        key=lambda s: confirmed_ratio(s.name),
    )
    for staff in ordered_staff:
        sname = staff.name
        req = requested_count[sname]
        target = _target_days(req)
        cand_dates = sorted(d for (n2, d) in all_candidates if n2 == sname)

        def _bumpable_relative(n2, _sname=sname):
            if _is_leader(staff_by_name.get(n2)):
                return False
            ratio_self = confirmed_ratio(_sname)
            if confirmed_ratio(n2) <= ratio_self:
                return False
            req2 = requested_count[n2]
            conf2 = sum(1 for (nn, _dd) in assigned if nn == n2)
            if req2 <= SMALL_REQUEST_THRESHOLD:
                # 小口保証枠の60%だけは死守する
                guarantee = math.ceil(req2 * SMALL_REQUEST_RATIO_GUARANTEE)
                return (conf2 - 1) >= guarantee
            # それ以外は「逆転しない」（譲った後も自分の元の充足率より
            # 下がらない）ことだけを条件にする。お互いが目標未達のまま
            # 膠着するのを避け、少しずつ差を縮められるようにするため。
            post_ratio2 = (conf2 - 1) / req2 if req2 else 1.0
            return post_ratio2 >= ratio_self

        while True:
            confirmed_now = sum(1 for d in cand_dates if (sname, d) in assigned)
            if confirmed_now >= target:
                break
            remaining = [d for d in cand_dates if (sname, d) not in assigned]
            if not remaining:
                break
            added = False
            for d in remaining:
                r, (s, e) = all_candidates[(sname, d)]
                if try_add_with_bump(sname, d, s, e, _bumpable_relative):
                    added = True
                    break
            if not added:
                break

    # 月のどこか（特に月初・月末）に偏らないようにする。確定日と確定日の
    # 間だけでなく、月の始まり・終わりとの間も「空きすぎ」とみなして
    # チェックする（月初だけ・月末だけに集中するのを防ぐことを優先する）。
    month_start = days[0].date
    month_end = days[-1].date

    def _bumpable_spacing(n2, _sname):
        # 月のどこかに偏るのを防ぐことを充足率の細かい比較より優先するため、
        # 充足率の逆転チェックは行わない。小口保証枠(60%)だけは死守する。
        if _is_leader(staff_by_name.get(n2)):
            return False
        req2 = requested_count[n2]
        conf2 = sum(1 for (nn, _dd) in assigned if nn == n2)
        if req2 <= SMALL_REQUEST_THRESHOLD:
            guarantee = math.ceil(req2 * SMALL_REQUEST_RATIO_GUARANTEE)
            return (conf2 - 1) >= guarantee
        return True

    for sname, all_dates in candidate_dates_by_staff.items():
        all_dates = sorted(all_dates)
        while True:
            confirmed_dates = sorted(d for d in all_dates if (sname, d) in assigned)
            anchors = [month_start] + confirmed_dates + [month_end]
            added_any = False
            for i in range(len(anchors) - 1):
                lo, hi = anchors[i], anchors[i + 1]
                gap_days = (hi - lo).days
                if gap_days <= 7:
                    continue
                gap_candidates = [
                    d for d in all_dates
                    if lo <= d <= hi and (sname, d) not in assigned
                ]
                if not gap_candidates:
                    continue
                # 空白期間を最もよく2分割できる候補を優先する
                mid_offset = gap_days / 2
                gap_candidates.sort(key=lambda d: abs((d - lo).days - mid_offset))
                for d in gap_candidates:
                    r, (s, e) = all_candidates[(sname, d)]
                    if try_add_with_bump(sname, d, s, e, lambda n2, _s=sname: _bumpable_spacing(n2, _s)):
                        added_any = True
                        break
                if added_any:
                    break  # confirmed_datesが古くなったので最初からやり直す
            if not added_any:
                break

    # 小口保証枠(希望11日以下)の60%保証を、他のパスによる巻き戻しが
    # ないか最後にもう一度確認する。この段階では、11日を超える非リーダー
    # からであれば充足率の比較なしに譲ってもらえる（保証を最優先する）。
    def _bumpable_for_small_guarantee_final(n2):
        if _is_leader(staff_by_name.get(n2)):
            return False
        return requested_count[n2] > SMALL_REQUEST_THRESHOLD

    for staff in staff_list:
        sname = staff.name
        req = requested_count[sname]
        if req == 0 or req > SMALL_REQUEST_THRESHOLD:
            continue
        target = math.ceil(req * SMALL_REQUEST_RATIO_GUARANTEE)
        cand_dates = sorted(d for (n2, d) in all_candidates if n2 == sname)
        while True:
            confirmed_now = sum(1 for d in cand_dates if (sname, d) in assigned)
            if confirmed_now >= target:
                break
            remaining = [d for d in cand_dates if (sname, d) not in assigned]
            if not remaining:
                break
            added = False
            for d in remaining:
                r, (s, e) = all_candidates[(sname, d)]
                if try_add_with_bump(sname, d, s, e, _bumpable_for_small_guarantee_final):
                    added = True
                    break
            if not added:
                break

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
