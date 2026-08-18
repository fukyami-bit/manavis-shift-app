import io
from dataclasses import replace

import pandas as pd
import streamlit as st

from app.exporter import build_template_workbook, format_time
from app.models import Band
from app.parser import parse_workbook
from app.scheduler import compute_cost_from_shift, default_bands, generate_schedule

st.set_page_config(page_title="マナビス シフト作成", layout="wide")
st.title("シフト自動作成")

if "step" not in st.session_state:
    st.session_state.step = "upload"


def go_to(step: str):
    st.session_state.step = step


# ---------- モード選択 ----------
mode = st.radio(
    "何をしますか？",
    ["新しいシフトを作成する", "修正済みのシフトから人件費を計算する"],
    key="mode_radio",
)
if st.session_state.get("mode") != mode:
    for key in ("parsed", "original_bytes", "uploaded_name", "result", "cost_result"):
        st.session_state.pop(key, None)
    st.session_state.step = "upload"
st.session_state.mode = mode
is_cost_mode = mode == "修正済みのシフトから人件費を計算する"

# ---------- 1. アップロード ----------
st.header("1. " + ("シフト表のアップロード" if is_cost_mode else "勤務希望のアップロード"))
upload_label = (
    "手直し済みの確定シフトExcel(.xlsx)をアップロードしてください（アプリが出力したものと同じ形式）"
    if is_cost_mode
    else "Googleスプレッドシートからダウンロードした勤務希望Excel(.xlsx)をアップロードしてください"
)
uploaded = st.file_uploader(upload_label, type=["xlsx"])

if uploaded is not None:
    if st.session_state.get("uploaded_name") != uploaded.name or "original_bytes" not in st.session_state:
        try:
            parsed = parse_workbook(io.BytesIO(uploaded.getvalue()))
        except Exception as e:
            st.error(f"読み込みに失敗しました: {e}")
            st.stop()
        st.session_state.parsed = parsed
        st.session_state.original_bytes = uploaded.getvalue()
        st.session_state.uploaded_name = uploaded.name
        st.session_state.wages = {s.name: 1200 for s in parsed.staff}
        st.session_state.bands = default_bands()
        st.session_state.step = "configure"

    parsed = st.session_state.parsed
    loaded_msg = (
        f"{len(parsed.staff)}名分のシフト・{parsed.days[0].date.strftime('%m/%d')}〜{parsed.days[-1].date.strftime('%m/%d')}を読み込みました"
        if is_cost_mode
        else f"{len(parsed.staff)}名分の希望・{parsed.days[0].date.strftime('%m/%d')}〜{parsed.days[-1].date.strftime('%m/%d')}を読み込みました"
    )
    st.success(loaded_msg)
    if parsed.warnings:
        with st.expander(f"読み込み時の注意 {len(parsed.warnings)}件"):
            for w in parsed.warnings:
                st.write("- " + w)

def render_wage_editor(parsed):
    st.caption("時給の欄は直接編集できます（スタッフごとに個別の金額を入力可能）")
    wage_df = pd.DataFrame(
        [
            {"氏名": s.name, "区分": s.category, "時給（円）": st.session_state.wages.get(s.name, 1200)}
            for s in parsed.staff
        ]
    )
    edited_wages = st.data_editor(
        wage_df,
        use_container_width=True,
        hide_index=True,
        disabled=["氏名", "区分"],
        column_config={
            "時給（円）": st.column_config.NumberColumn(min_value=0, step=10),
        },
        key="wage_editor",
    )
    st.session_state.wages = {
        row["氏名"]: int(row["時給（円）"]) for _, row in edited_wages.iterrows()
    }


if st.session_state.step in ("configure", "result") and "parsed" in st.session_state:
    parsed = st.session_state.parsed

    if is_cost_mode:
        st.header("2. スタッフの時給を確認")
        render_wage_editor(parsed)

        if st.button("人件費を計算", type="primary"):
            staff_with_wages = [replace(s, hourly_wage=st.session_state.wages[s.name]) for s in parsed.staff]
            st.session_state.cost_result = compute_cost_from_shift(staff_with_wages, parsed.days, parsed.requests)
            st.session_state.step = "result"
    else:
        st.header("2. 設定確認")
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("スタッフと時給")
            render_wage_editor(parsed)

        with col2:
            st.subheader("必要人数（時間帯ごと）")
            bands = st.session_state.bands
            new_bands = {}
            for day_type, label in (
                ("weekend", "土日（長期休暇中の日曜含む）"),
                ("sunday_short", "日曜（20時閉館）"),
                ("weekday", "平日"),
            ):
                st.markdown(f"**{label}**")
                new_list = []
                for b in bands[day_type]:
                    c1, c2 = st.columns(2)
                    n_min = c1.number_input(
                        f"{b.label} 最低人数",
                        min_value=0, max_value=10, value=b.min_required, step=1,
                        key=f"band_min_{day_type}_{b.label}",
                    )
                    default_max = b.max_required if b.max_required is not None else n_min
                    n_max = c2.number_input(
                        f"{b.label} 上限人数",
                        min_value=n_min, max_value=15, value=max(default_max, n_min), step=1,
                        key=f"band_max_{day_type}_{b.label}",
                    )
                    new_list.append(replace(b, min_required=n_min, max_required=n_max))
                new_bands[day_type] = new_list
            st.session_state.bands = new_bands

            st.subheader("今月の予算")
            budget = st.number_input("人件費の上限（円）", min_value=0, value=st.session_state.get("budget", 450000), step=10000)
            st.session_state.budget = budget

        if st.button("この内容でシフトを作成", type="primary"):
            staff_with_wages = [replace(s, hourly_wage=st.session_state.wages[s.name]) for s in parsed.staff]
            st.session_state.result = generate_schedule(
                staff_with_wages, parsed.days, parsed.requests, st.session_state.bands, st.session_state.budget,
            )
            st.session_state.step = "result"

if st.session_state.step == "result" and is_cost_mode and "cost_result" in st.session_state:
    parsed = st.session_state.parsed
    cost_result = st.session_state.cost_result

    st.header("3. 人件費")
    st.metric("合計人件費", f"¥{int(cost_result.total_cost):,}")

    if parsed.warnings:
        with st.expander(f"読み取り時の注意 {len(parsed.warnings)}件"):
            for w in parsed.warnings:
                st.write("- " + w)

    st.subheader("スタッフごとの内訳")
    rows = [
        {
            "氏名": name,
            "勤務日数": v["confirmed"],
            "合計時間": f"{v['hours']:g}h",
            "小計": f"¥{int(v['cost']):,}",
        }
        for name, v in cost_result.staff_stats.items()
        if v["confirmed"] > 0
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

if st.session_state.step == "result" and not is_cost_mode and "result" in st.session_state:
    parsed = st.session_state.parsed
    result = st.session_state.result
    staff_list = [replace(s, hourly_wage=st.session_state.wages[s.name]) for s in parsed.staff]

    st.header("3. 結果")

    m1, m2, m3 = st.columns(3)
    m1.metric("人件費 / 予算", f"¥{int(result.total_cost):,}", f"予算 ¥{result.budget:,}")
    m2.metric("不足コマ", f"{len(result.shortages)}件")
    ratios = [v["ratio"] for v in result.staff_stats.values() if v["ratio"] is not None]
    spread = (max(ratios) - min(ratios)) * 100 if ratios else 0
    m3.metric("希望充足の偏り", f"{spread:.0f}pt", "最大と最小の差")

    if result.shortages:
        with st.expander(f"不足・要確認 {len(result.shortages)}件", expanded=True):
            for s in result.shortages:
                st.warning(f"{s['date'].strftime('%m/%d')}: {s['message']}")
    if result.warnings:
        for w in result.warnings:
            st.warning(w)

    st.subheader("シフト表（日別）")
    by_day = {}
    for a in result.assignments:
        by_day.setdefault(a.date, []).append(a)
    rows = []
    for day in parsed.days:
        assigns = sorted(by_day.get(day.date, []), key=lambda a: a.start)
        text = ", ".join(
            f"{a.staff}{'(仮)' if a.tentative else ''} {format_time(a.start)}-{format_time(a.end)}"
            for a in assigns
        )
        rows.append({"日付": day.date.strftime("%m/%d"), "曜日": day.weekday, "担当": text or "(該当者なし)"})
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.subheader("希望充足の公平性（確定日数 / 希望日数）")
    fairness_rows = sorted(
        [(name, v) for name, v in result.staff_stats.items() if v["requested"] > 0],
        key=lambda x: -(x[1]["ratio"] or 0),
    )
    for name, v in fairness_rows:
        st.progress(v["ratio"], text=f"{name}: {v['confirmed']}/{v['requested']}日 ({v['ratio']*100:.0f}%)")

    st.subheader("Excelとして出力")
    st.caption("アップロードされた元ファイルと同じ書式（罫線・列幅・色）のまま、希望欄を確定シフトで上書きします")
    month_label = parsed.days[0].date.strftime("%Y年%m月")
    wb = build_template_workbook(
        io.BytesIO(st.session_state.original_bytes), parsed.layout, staff_list, parsed.days, result,
    )
    buf = io.BytesIO()
    wb.save(buf)
    st.download_button(
        "確定シフトをダウンロード（.xlsx）",
        data=buf.getvalue(),
        file_name=f"{month_label}_確定シフト.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
