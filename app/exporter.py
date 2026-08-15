"""確定シフトをExcelに書き出す。

2シート構成:
- アップロード元ファイルと同じグリッド形式（文理ブロック分け・開館時間・不足列）に確定シフトを転記
- 「時間帯一覧」: 日付ごとに「誰が何時から何時まで」を時系列で並べた読みやすいリスト
"""
from __future__ import annotations

from collections import defaultdict

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .models import DayInfo, ScheduleResult, Staff

HEADER_FILL = PatternFill("solid", fgColor="F1EFE8")
SHORTAGE_FILL = PatternFill("solid", fgColor="FAEEDA")
BOLD = Font(bold=True)


def format_time(hours: float) -> str:
    total_minutes = round(hours * 60)
    hh, mm = divmod(total_minutes, 60)
    return f"{hh}:{mm:02d}"


def build_template_workbook(
    original_file_obj,
    layout,
    staff_list: list[Staff],
    days: list[DayInfo],
    result: ScheduleResult,
) -> openpyxl.Workbook:
    """アップロードされた元ファイルそのものをテンプレートとして使い、
    希望が入っていたセルを確定シフトで上書きする。フォント・罫線・列幅・
    色などの元の書式は変更しない。"""
    wb = openpyxl.load_workbook(original_file_obj)
    ws = wb[layout.sheet_name]

    by_day_staff = {}
    for a in result.assignments:
        by_day_staff[(a.date, a.staff)] = a

    shortage_by_day = defaultdict(list)
    for sh in result.shortages:
        shortage_by_day[sh["date"]].append(sh)

    for row, day in zip(range(layout.data_start_row, layout.data_end_row + 1), days):
        shortages_today = shortage_by_day.get(day.date, [])
        shortage_cell = ws.cell(row=row, column=layout.shortage_col)
        shortage_cell.value = "; ".join(s["message"] for s in shortages_today) or None
        if shortages_today:
            shortage_cell.fill = SHORTAGE_FILL

        for col_idx, name, _cat, _wage in layout.staff_columns:
            cell = ws.cell(row=row, column=col_idx)
            a = by_day_staff.get((day.date, name))
            if a is None:
                cell.value = None
                continue
            text = f"{a.start:g}-{a.end:g}"
            if a.tentative:
                text = "△" + text
            cell.value = text

    ws2 = wb.create_sheet("時間帯一覧")
    _build_timeline_sheet(ws2, days, result)
    return wb


def _build_timeline_sheet(ws, days, result: ScheduleResult):
    headers = ["日付", "曜日", "時間帯", "担当"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = BOLD
        cell.fill = HEADER_FILL

    by_day = defaultdict(list)
    for a in result.assignments:
        by_day[a.date].append(a)

    r = 2
    for day in days:
        assigns = sorted(by_day.get(day.date, []), key=lambda a: a.start)
        if not assigns:
            continue
        for a in assigns:
            ws.cell(row=r, column=1, value=day.date.strftime("%m/%d"))
            ws.cell(row=r, column=2, value=day.weekday)
            time_text = f"{format_time(a.start)} - {format_time(a.end)}"
            if a.tentative:
                time_text += "（仮）"
            ws.cell(row=r, column=3, value=time_text)
            ws.cell(row=r, column=4, value=a.staff)
            r += 1

    for c, w in zip(range(1, 5), (10, 8, 20, 14)):
        ws.column_dimensions[get_column_letter(c)].width = w
