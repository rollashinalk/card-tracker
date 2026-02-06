import streamlit as st
import pandas as pd
import uuid
from datetime import date, datetime
from dateutil.relativedelta import relativedelta

import gspread
from google.oauth2.service_account import Credentials
import json

# -----------------------------
# Google Sheets helpers
# -----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_client():
    creds = Credentials.from_service_account_info(
        json.loads(st.secrets["gcp_service_account"]),
        scopes=SCOPES
    )
    return gspread.authorize(creds)

@st.cache_resource
def get_worksheets():
    gc = get_client()
    sh = gc.open_by_key(st.secrets["spreadsheet_id"])
    cards_ws = sh.worksheet("cards")
    tx_ws = sh.worksheet("tx")
    return cards_ws, tx_ws

def ws_to_df(ws) -> pd.DataFrame:
    rows = ws.get_all_records()
    return pd.DataFrame(rows)

def append_row(ws, row: list):
    ws.append_row(row, value_input_option="USER_ENTERED")

def update_ws_from_df(ws, df: pd.DataFrame):
    # 헤더 포함 전체 덮어쓰기(삭제 반영용)
    ws.clear()
    ws.update([df.columns.tolist()] + df.values.tolist())

# -----------------------------
# Business logic
# -----------------------------
def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"

def allowed_months(today: date):
    this_m = date(today.year, today.month, 1)
    m_prev = this_m - relativedelta(months=1)
    m_next = this_m + relativedelta(months=1)
    return [ym(m_prev), ym(this_m), ym(m_next)]

def cleanup_tx(tx_df: pd.DataFrame, allowed: list[str]) -> pd.DataFrame:
    if tx_df.empty:
        return tx_df
    # month 컬럼이 문자열이어야 함
    tx_df["month"] = tx_df["month"].astype(str)
    return tx_df[tx_df["month"].isin(allowed)].copy()

def compute_dashboard(cards_df: pd.DataFrame, tx_df: pd.DataFrame, month: str) -> pd.DataFrame:
    active_cards = cards_df[cards_df["active"] == True].copy()
    if active_cards.empty:
        return pd.DataFrame(columns=["card_name","monthly_target","spent","remaining","status"])

    tx_m = tx_df[tx_df["month"] == month].copy() if not tx_df.empty else pd.DataFrame(columns=["card_id","amount"])
    if not tx_m.empty:
        tx_m["amount"] = pd.to_numeric(tx_m["amount"], errors="coerce").fillna(0).astype(int)
        spent = tx_m.groupby("card_id", as_index=False)["amount"].sum().rename(columns={"amount":"spent"})
    else:
        spent = pd.DataFrame({"card_id": [], "spent": []})

    out = active_cards.merge(spent, on="card_id", how="left")
    out["spent"] = out["spent"].fillna(0).astype(int)
    out["monthly_target"] = pd.to_numeric(out["monthly_target"], errors="coerce").fillna(0).astype(int)
    out["remaining"] = (out["monthly_target"] - out["spent"]).clip(lower=0)
    out["status"] = out.apply(lambda r: "✅" if r["spent"] >= r["monthly_target"] and r["monthly_target"] > 0 else "❌", axis=1)

    # 숫자 포맷용 컬럼 생성 (표시용)
    out["목표 실적"] = out["monthly_target"].map(lambda x: f"{x:,}")
    out["사용 금액"] = out["spent"].map(lambda x: f"{x:,}")
    out["남은 금액"] = out["remaining"].map(lambda x: f"{x:,}")
    
    return out[
        ["card_name", "목표 실적", "사용 금액", "남은 금액", "status"]
    ].rename(columns={"card_name": "카드명", "status": "상태"}).sort_values(
        ["상태", "남은 금액", "카드명"]
    )


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="카드 실적 트래커", layout="wide")

cards_ws, tx_ws = get_worksheets()

cards_df = ws_to_df(cards_ws)
tx_df = ws_to_df(tx_ws)

# active 컬럼 정규화 (문자열/숫자 → Boolean)
if "active" in cards_df.columns:
    cards_df["active"] = (
        cards_df["active"]
        .astype(str)
        .str.strip()
        .str.upper()
        .isin(["TRUE", "1", "YES", "Y"])
    )

# 빈 시트 대비(최초 실행)
if cards_df.empty:
    cards_df = pd.DataFrame(columns=["card_id","card_name","monthly_target","active"])
if tx_df.empty:
    tx_df = pd.DataFrame(columns=["tx_id","date","month","card_id","amount"])

today = date.today()
months = allowed_months(today)

# 자동 삭제(허용 월 밖 tx 삭제)
tx_df_clean = cleanup_tx(tx_df, months)
if len(tx_df_clean) != len(tx_df):
    update_ws_from_df(tx_ws, tx_df_clean)
    tx_df = tx_df_clean

st.title("카드 실적 트래커")

tab1, tab2, tab3 = st.tabs(["대시보드", "결제 입력", "카드 관리"])

with tab1:
    sel_month = st.selectbox("월 선택", months, index=1)
    dash = compute_dashboard(cards_df, tx_df, sel_month)
    st.dataframe(dash, use_container_width=True)

with tab2:
    st.subheader("결제 내역 추가(실적 포함만)")

    # 활성 카드 목록
    active = cards_df[cards_df["active"] == True].copy() if "active" in cards_df.columns else cards_df.copy()
    if active.empty:
        st.info("먼저 '카드 관리'에서 카드를 추가해 주세요.")
    else:
        card_map = dict(zip(active["card_name"], active["card_id"]))
        id_to_name = dict(zip(active["card_id"], active["card_name"]))

        c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
        with c1:
            card_name = st.selectbox("카드", list(card_map.keys()))
        with c2:
            amount = st.number_input("금액", min_value=0, step=1000, value=0)
        with c3:
            d = st.date_input("날짜", value=today)
        with c4:
            item = st.text_input("항목", placeholder="예: 편의점 / 택시 / 점심 등 (선택)")

        # 저장 버튼
        if st.button("추가", type="primary", use_container_width=True):
            if amount <= 0:
                st.warning("금액을 1원 이상 입력해 주세요.")
            else:
                m = ym(d.replace(day=1))
                if m not in months:
                    st.error("허용 기간(이번달-1 ~ 이번달+1) 밖의 날짜는 입력할 수 없습니다.")
                else:
                    append_row(
                        tx_ws,
                        [str(uuid.uuid4()), d.isoformat(), m, card_map[card_name], int(amount), item.strip()]
                    )
                    st.rerun()

        st.divider()

        # 📌 해당 월 히스토리 표
        # 대시보드와 같은 월 선택 기준(기본: 이번달)
        sel_month_tx = st.selectbox("히스토리 월", months, index=1, key="tx_month")

        tx_view = tx_df.copy()
        if "item" not in tx_view.columns:
            tx_view["item"] = ""

        tx_view["month"] = tx_view["month"].astype(str)
        tx_view = tx_view[tx_view["month"] == sel_month_tx].copy()

        if tx_view.empty:
            st.info("해당 월에 입력된 내역이 없습니다.")
        else:
            tx_view["카드"] = tx_view["card_id"].map(id_to_name).fillna(tx_view["card_id"].astype(str))
            tx_view["항목"] = tx_view["item"].astype(str)
            tx_view["금액"] = pd.to_numeric(tx_view["amount"], errors="coerce").fillna(0).astype(int).map(lambda x: f"{x:,}")
            tx_view["날짜"] = tx_view["date"].astype(str)

            # 날짜 내림차순(최근 먼저)
            tx_view = tx_view.sort_values(["날짜"], ascending=False)

            st.dataframe(
                tx_view[["날짜", "카드", "항목", "금액"]],
                use_container_width=True,
                hide_index=True
            )


with tab3:
    st.subheader("카드 관리")
    # 카드 추가
    with st.expander("카드 추가", expanded=True):
        c1, c2 = st.columns([3,2])
        with c1:
            new_name = st.text_input("카드명")
        with c2:
            new_target = st.number_input("목표 실적(월)", min_value=0, step=10000, value=300000)
        if st.button("카드 추가", use_container_width=True):
            if not new_name.strip():
                st.warning("카드명을 입력해 주세요.")
            else:
                append_row(cards_ws, [str(uuid.uuid4()), new_name.strip(), int(new_target), True])
                st.success("카드가 추가되었습니다.")
                st.rerun()

    # 카드 목록 편집(목표 수정/비활성)
    st.markdown("### 카드 목록")
    if cards_df.empty:
        st.info("등록된 카드가 없습니다.")
    else:
        edit_df = cards_df.copy()
        edit_df["monthly_target"] = pd.to_numeric(edit_df["monthly_target"], errors="coerce").fillna(0).astype(int)
        edited = st.data_editor(
            edit_df[["card_id","card_name","monthly_target","active"]],
            use_container_width=True,
            disabled=["card_id"],
            hide_index=True
        )
        if st.button("변경사항 저장", use_container_width=True):
            update_ws_from_df(cards_ws, edited)
            st.success("저장되었습니다.")
            st.rerun()
