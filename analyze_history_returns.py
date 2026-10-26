import json
import glob
from pathlib import Path
import math
import pandas as pd

DOCS_HISTORY_DIR = Path("docs/history")
PRICES_DIR = Path("data/prices")
START_DATE = "2026-09-01"   # 起始日期自 9/1 起
HOLDING_DAYS = 30           # 計算 30 天供動態切換

price_cache = {}

def get_stock_prices(stock_id: str):
    """讀取本機現有 Parquet 日 K（含收盤、最高、最低、成交量）"""
    if stock_id in price_cache:
        return price_cache[stock_id]
        
    candidates = [
        PRICES_DIR / f"{stock_id}.parquet",
        PRICES_DIR / f"{stock_id}.TW.parquet",
        PRICES_DIR / f"{stock_id}.TWO.parquet",
    ]
    for p in candidates:
        if p.exists():
            try:
                df = pd.read_parquet(p)
                date_col = next((c for c in df.columns if str(c).lower() in ["date", "datetime", "time"]), None)
                close_col = next((c for c in df.columns if str(c).lower() in ["close", "price"]), None)
                high_col = next((c for c in df.columns if str(c).lower() in ["high", "max_price"]), None)
                vol_col = next((c for c in df.columns if str(c).lower() in ["volume", "vol", "trading_volume"]), None)
                
                if date_col and close_col:
                    df["date_str"] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
                    df["close_price"] = df[close_col].astype(float)
                    df["high_price"] = df[high_col].astype(float) if high_col else df["close_price"]
                    df["vol_val"] = df[vol_col].astype(float) if vol_col else 0.0
                    res = df.sort_values("date_str").reset_index(drop=True)
                    price_cache[stock_id] = res
                    return res
            except Exception:
                pass
    price_cache[stock_id] = None
    return None

def detect_downtrend_breakout(df_price, idx, target_pivot):
    """
    偵測『突破下降趨勢線』訊號：
    1. 回溯前 12~40 根日 K，尋找前波最高點 P1 與後續次高點 P2 (P2 < P1) 構成之向下趨勢線
    2. 確認突破前股價受制於趨勢線下方，且入選當日或突破價正式向上穿越下降趨勢線
    3. 檢查成交量是否放大 (大於 5 日均量 1.2 倍) 且收盤站穩趨勢線之上 (對應最理想型態)
    """
    if df_price is None or idx < 12:
        return False, False, None

    start_i = max(0, idx - 35)
    sub = df_price.loc[start_i:idx].reset_index(drop=True)
    n = len(sub)
    if n < 12:
        return False, False, None

    highs = sub["high_price"].tolist()
    closes = sub["close_price"].tolist()
    vols = sub["vol_val"].tolist()

    # 1. 尋找前波高點 P1 (排除最後 5 天，確保有足夠時間形成下降趨勢)
    search_end_p1 = n - 5
    p1_idx = 0
    p1_val = highs[0]
    for i in range(search_end_p1):
        if highs[i] >= p1_val:
            p1_val = highs[i]
            p1_idx = i

    if p1_val <= 0 or (n - 1 - p1_idx) < 5:
        return False, False, None

    # 2. 尋找連接 P1 的上切線次高點 P2 (形成壓制價格的下降趨勢線)
    best_slope = None
    p2_idx = None
    for k in range(p1_idx + 2, n - 1):
        if highs[k] < p1_val * 0.995:  # 次高點必須低於前高 (向下傾斜)
            slope = (highs[k] - p1_val) / float(k - p1_idx)
            if best_slope is None or slope > best_slope:
                best_slope = slope
                p2_idx = k

    if best_slope is None or best_slope >= 0:
        return False, False, None

    # 3. 計算下降趨勢線延伸至入選日 (n-1) 的壓力位價格
    tl_today = p1_val + best_slope * float((n - 1) - p1_idx)
    tl_prev = p1_val + best_slope * float((n - 2) - p1_idx)

    if tl_today <= 0 or tl_today < closes[-1] * 0.82:
        return False, False, None

    # 確認中間過程有明顯回檔整理 (最低收盤價低於 P1 至少 3%)
    min_close_between = min(closes[p1_idx:n - 1])
    if min_close_between > p1_val * 0.97:
        return False, False, None

    # 確認前幾日確實受制於下降趨勢線附近或下方，且當日(或突破樞紐價時)向上突破趨勢線
    curr_close = closes[-1]
    curr_high = highs[-1]
    check_pivot = target_pivot if target_pivot else curr_close

    broke_today = (curr_close >= tl_today * 0.995) or (curr_high >= tl_today and curr_close >= tl_today * 0.985)
    broke_on_pivot = (check_pivot >= tl_today * 0.995) and ( curr_close >= tl_today * 0.96 )
    was_below_recently = (closes[-2] <= tl_prev * 1.015) or (min(closes[max(p1_idx, n - 5):n - 1]) < tl_today)

    is_trend_break = bool((broke_today or broke_on_pivot) and was_below_recently)
    if not is_trend_break:
        return False, False, None

    # 4. 檢查是否為「帶量站穩」最理想型態 (量增 >= 5日均量1.2倍 且 收盤站上趨勢線)
    prior_vols = [v for v in vols[max(0, n - 6):n - 1] if v > 0]
    avg_vol_5 = (sum(prior_vols) / len(prior_vols)) if prior_vols else 0.0
    vol_expanded = (vols[-1] >= avg_vol_5 * 1.2) if avg_vol_5 > 0 else True
    held_above = curr_close >= tl_today

    is_ideal_break = bool(is_trend_break and vol_expanded and held_above)
    return is_trend_break, is_ideal_break, round(tl_today, 2)

def analyze_and_build_dual_report():
    json_files = sorted(glob.glob(str(DOCS_HISTORY_DIR / "*.json")))
    if not json_files:
        print("未找到任何 history JSON 檔案！")
        return

    recent_files = [f for f in json_files if Path(f).stem >= START_DATE]
    if not recent_files:
        print(f"在 {START_DATE} 之後未找到任何選股 JSON 檔案！")
        return

    latest_date_str = Path(recent_files[-1]).stem
    total_days = len(recent_files)
    print(f"載入自 {START_DATE} 起至最新 {latest_date_str} 共 {total_days} 個交易日選股名單...")
    
    date_grouped_data = {}

    for file_idx, file_path in enumerate(recent_files, 1):
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                continue

        date_str = data.get("as_of", Path(file_path).stem)
        date_grouped_data[date_str] = []
        print(f"[{file_idx}/{total_days}] 正在處理 {date_str} 選股行情...", flush=True)

        for stage in ["BREAKOUT", "READY", "LEADERS"]:
            for item in data.get(stage, []):
                sid = str(item.get("stock_id") or item.get("symbol") or item.get("code") or item.get("sid") or "") if isinstance(item, dict) else str(item)
                name = item.get("stock_name", item.get("name", "")) if isinstance(item, dict) else ""
                if not sid:
                    continue

                bonus_tags = []
                grade = ""
                pivot_price = None

                if isinstance(item, dict):
                    trust_streak = item.get("trust_streak")
                    if trust_streak and int(trust_streak) >= 1:
                        bonus_tags.append(f"投信連{trust_streak}買")
                    elif item.get("inst_net_buy"):
                        bonus_tags.append("法人買超")

                    if item.get("rs_line_new_high"):
                        bonus_tags.append("RS創高")
                    if item.get("vol_contraction"):
                        bonus_tags.append("量縮緊湊")
                    
                    dist_52w = item.get("dist_52w_high")
                    if dist_52w is not None:
                        try:
                            if float(dist_52w) <= 0.05:
                                bonus_tags.append("近52w高")
                        except Exception:
                            pass

                    if item.get("fundamental_ok"):
                        bonus_tags.append("基本面佳")
                    else:
                        rev_yoy = item.get("rev_3m_yoy") or item.get("eps_yoy")
                        if rev_yoy:
                            try:
                                if float(rev_yoy) > 0.15:
                                    bonus_tags.append("營收亮眼")
                            except Exception:
                                pass

                    bonus_tags = bonus_tags[:2]

                    pv_val = item.get("pivot_high") or item.get("pivot")
                    if pv_val is not None:
                        try:
                            pivot_price = round(float(pv_val), 2)
                        except Exception:
                            pass

                    grade = str(item.get("grade") or item.get("score_grade") or "").strip().upper()
                    if not grade:
                        rs = item.get("rs_rating")
                        if rs is not None:
                            try:
                                r_val = float(rs)
                                grade = "A" if r_val >= 90 else ("B" if r_val >= 80 else "C")
                            except Exception:
                                pass

                if grade not in ["A", "B", "C"]:
                    grade = "B"

                df_price = get_stock_prices(sid)
                idx = -1
                select_day_close = None
                
                if df_price is not None and not df_price.empty:
                    match = df_price[df_price["date_str"] == date_str]
                    if not match.empty:
                        idx = match.index[0]
                        select_day_close = df_price.loc[idx, "close_price"]

                target_pivot = pivot_price if pivot_price else select_day_close

                # 偵測「突破下降趨勢線」訊號
                trend_break, trend_ideal, tl_price = detect_downtrend_breakout(df_price, idx, target_pivot)

                raw_bars = []
                for d in range(1, HOLDING_DAYS + 1):
                    if df_price is not None and idx != -1 and (idx + d) < len(df_price):
                        curr_row = df_price.loc[idx + d]
                        curr_date = curr_row["date_str"][5:]
                        curr_close = round(float(curr_row["close_price"]), 2)
                        curr_high = round(float(curr_row["high_price"]), 2)
                        raw_bars.append([curr_date, curr_close, curr_high])
                    else:
                        raw_bars.append(None)

                date_grouped_data[date_str].append({
                    "date_str": date_str,
                    "stock_id": sid,
                    "name": name[:5],
                    "stage": stage,
                    "grade": grade,
                    "bonus": bonus_tags,
                    "trend_break": trend_break,
                    "trend_ideal": trend_ideal,
                    "tl_price": tl_price,
                    "pivot": pivot_price,
                    "target_pivot": round(target_pivot, 2) if target_pivot else None,
                    "raw_bars": raw_bars
                })

    cleaned_grouped_data = {}
    for d, items in date_grouped_data.items():
        seen = set()
        cleaned_items = []
        for it in items:
            key = (it["stage"], it["stock_id"])
            if key not in seen:
                seen.add(key)
                cleaned_items.append(it)
        cleaned_grouped_data[d] = cleaned_items

    json_data_str = json.dumps(cleaned_grouped_data, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW" data-theme="clean">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>趙阿瑋要賺爆了</title>
<style>
    * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
    
    :root[data-theme="clean"] {{
        --bg-main: #f4f5f8;
        --card-bg: #ffffff;
        --card-border: #e2e8f0;
        --card-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
        --accent-brand: #3b82f6;
        --accent-gold: #d97706;
        --accent-gold-bg: #fef3c7;
        --text-title: #0f172a;
        --text-main: #1e293b;
        --text-muted: #64748b;
        --border-subtle: #edf2f7;
        --ctrl-bg: #ffffff;
        --ctrl-border: #cbd5e1;
        --ctrl-active-bg: #0f172a;
        --ctrl-active-text: #ffffff;
        --table-th-bg: #f8fafc;
        --table-stripe: #fafbfc;
        --red-profit: #e11d48;
        --red-profit-bg: #ffe4e6;
        --green-loss: #059669;
        --green-loss-bg: #d1fae5;
        --badge-blue: #2563eb;
        --badge-blue-bg: #eff6ff;
    }}

    :root[data-theme="dark"] {{
        --bg-main: #0c0618;
        --card-bg: #150a28;
        --card-border: #2e1752;
        --card-shadow: 0 4px 15px rgba(0, 0, 0, 0.5);
        --accent-brand: #a855f7;
        --accent-gold: #facc15;
        --accent-gold-bg: rgba(250, 204, 21, 0.15);
        --text-title: #facc15;
        --text-main: #fdf8f0;
        --text-muted: #a79bb7;
        --border-subtle: #23123d;
        --ctrl-bg: #1e0e38;
        --ctrl-border: #4a2185;
        --ctrl-active-bg: #facc15;
        --ctrl-active-text: #1a0b2e;
        --table-th-bg: #110722;
        --table-stripe: #170c2d;
        --red-profit: #f87171;
        --red-profit-bg: rgba(239, 68, 68, 0.22);
        --green-loss: #34d399;
        --green-loss-bg: rgba(52, 211, 153, 0.2);
        --badge-blue: #38bdf8;
        --badge-blue-bg: rgba(56, 189, 248, 0.2);
    }}

    body {{
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, "PingFang TC", "Microsoft JhengHei", sans-serif;
        background: var(--bg-main);
        color: var(--text-main);
        margin: 0;
        padding: 10px 12px;
        -webkit-font-smoothing: antialiased;
        transition: background-color 0.25s ease, color 0.25s ease;
    }}
    
    .header-area {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 8px;
        flex-wrap: wrap;
        gap: 6px;
        border-bottom: 1px solid var(--card-border);
        padding-bottom: 7px;
    }}
    h1 {{
        margin: 0;
        font-size: 19px;
        font-weight: 900;
        display: flex;
        align-items: center;
        gap: 6px;
        color: var(--text-title);
        letter-spacing: 0.5px;
    }}

    .theme-switch-group {{
        display: flex;
        align-items: center;
        gap: 3px;
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 20px;
        padding: 2px 3px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    .theme-btn {{
        border: none;
        background: transparent;
        color: var(--text-muted);
        font-size: 10.5px;
        font-weight: 700;
        padding: 3px 8px;
        border-radius: 14px;
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 3px;
        transition: all 0.2s;
    }}
    .theme-btn.active {{
        background: var(--ctrl-active-bg);
        color: var(--ctrl-active-text);
        box-shadow: 0 1px 4px rgba(0,0,0,0.12);
    }}

    .scheme-bar {{
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 9px;
        padding: 7px 9px;
        margin-bottom: 8px;
        box-shadow: var(--card-shadow);
        display: flex;
        flex-direction: column;
        gap: 5px;
    }}
    .scheme-btn-row {{
        display: flex;
        align-items: center;
        gap: 6px;
        overflow-x: auto;
        scrollbar-width: none;
        white-space: nowrap;
    }}
    .scheme-btn-row::-webkit-scrollbar {{ display: none; }}
    .scheme-label {{
        font-size: 11.5px;
        font-weight: 900;
        color: var(--text-title);
        flex-shrink: 0;
    }}
    .scheme-btn {{
        background: var(--ctrl-bg);
        border: 1px solid var(--ctrl-border);
        color: var(--text-muted);
        padding: 4px 10px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 11.5px;
        font-weight: 800;
        transition: all 0.18s;
        flex-shrink: 0;
    }}
    .scheme-btn.active {{
        background: var(--accent-brand);
        color: #ffffff;
        border-color: var(--accent-brand);
        box-shadow: 0 2px 6px rgba(59, 130, 246, 0.28);
    }}

    .legend-tag {{ font-size: 11px; color: var(--text-muted); display: flex; gap: 5px; align-items: center; flex-wrap: wrap; }}

    .strat-scope-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 6px;
        padding: 0 2px;
    }}
    .strat-scope-title {{
        font-size: 13.5px;
        font-weight: 800;
        color: var(--text-title);
    }}
    .scope-toggle-btn {{
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        color: var(--text-muted);
        font-size: 11px;
        font-weight: 800;
        padding: 3px 9px;
        border-radius: 6px;
        cursor: pointer;
        transition: all 0.2s;
    }}
    .scope-toggle-btn.active {{
        background: var(--ctrl-active-bg);
        color: var(--ctrl-active-text);
        border-color: var(--ctrl-active-bg);
    }}

    .overall-compact {{
        display: flex;
        gap: 8px;
        overflow-x: auto;
        scrollbar-width: none;
        margin-bottom: 8px;
    }}
    .overall-compact::-webkit-scrollbar {{ display: none; }}
    
    .mini-strat-card {{
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 9px;
        padding: 8px 10px;
        flex: 1;
        min-width: 134px;
        box-shadow: var(--card-shadow);
        display: flex;
        flex-direction: column;
        gap: 3px;
    }}
    .mini-strat-title {{ font-size: 12px; font-weight: 800; color: var(--accent-brand); display: flex; justify-content: space-between; align-items: center; }}
    .mini-strat-rule-tag {{ font-size: 9.5px; color: var(--text-muted); font-weight: 700; }}
    .mini-strat-highlight {{ font-size: 12px; color: var(--accent-gold); font-weight: 800; }}
    
    .mini-stat-ret {{
        font-size: 14px;
        font-weight: 900;
        line-height: 1.2;
    }}
    .mini-stat-sub-win {{
        font-size: 11px;
        color: var(--text-muted);
        font-weight: 700;
    }}
    
    .mini-strat-quant {{
        font-size: 10.5px;
        color: var(--text-muted);
        display: flex;
        justify-content: space-between;
        margin-top: 2px;
    }}
    .mini-strat-quant b {{ color: var(--text-main); font-size: 11px; }}

    .mini-strat-sub {{
        font-size: 10.5px;
        color: var(--text-muted);
        display: flex;
        justify-content: space-between;
        border-top: 1px solid var(--border-subtle);
        padding-top: 4px;
        margin-top: 2px;
    }}

    .controls-wrapper {{
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 9px;
        padding: 8px 9px;
        margin-bottom: 8px;
        display: flex;
        flex-direction: column;
        gap: 7px;
        box-shadow: var(--card-shadow);
    }}
    .ctrl-row {{
        display: flex;
        align-items: center;
        gap: 6px;
        overflow-x: auto;
        scrollbar-width: none;
        white-space: nowrap;
    }}
    .ctrl-row::-webkit-scrollbar {{ display: none; }}
    
    .days-navigator-wrapper {{
        display: flex;
        align-items: center;
        gap: 4px;
        flex: 1;
        min-width: 0;
    }}

    .days-select-dropdown {{
        background: var(--ctrl-bg);
        color: var(--text-main);
        border: 1px solid var(--ctrl-border);
        padding: 4px 6px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 700;
        outline: none;
        cursor: pointer;
        flex-shrink: 0;
    }}

    .days-nav-btn {{
        background: var(--ctrl-bg);
        border: 1px solid var(--ctrl-border);
        color: var(--text-main);
        font-size: 12px;
        font-weight: 900;
        cursor: pointer;
        padding: 4px 7px;
        border-radius: 6px;
        display: flex;
        align-items: center;
        justify-content: center;
        user-select: none;
        flex-shrink: 0;
        transition: all 0.15s;
    }}
    .days-nav-btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}

    .days-scroll-group {{
        display: flex;
        align-items: center;
        gap: 4px;
        overflow-x: auto;
        scrollbar-width: none;
        -webkit-overflow-scrolling: touch;
        padding: 1px 0;
        flex: 1;
    }}
    .days-scroll-group::-webkit-scrollbar {{ display: none; }}

    .pill-day-btn {{
        background: var(--ctrl-bg);
        border: 1px solid var(--ctrl-border);
        color: var(--text-muted);
        padding: 4px 8px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 11.5px;
        font-weight: 700;
        transition: all 0.15s;
        flex-shrink: 0;
        min-width: 34px;
        text-align: center;
    }}
    .pill-day-btn.active-days {{
        background: var(--ctrl-active-bg) !important;
        border-color: var(--ctrl-active-bg) !important;
        color: var(--ctrl-active-text) !important;
        font-weight: 900 !important;
    }}

    .search-box-wrapper {{
        position: relative;
        display: flex;
        align-items: center;
        flex: 1;
        min-width: 140px;
    }}
    .search-input {{
        background: var(--ctrl-bg);
        border: 1px solid var(--ctrl-border);
        color: var(--text-main);
        padding: 5px 24px 5px 9px;
        border-radius: 6px;
        font-size: 12px;
        width: 100%;
        outline: none;
    }}
    .search-input:focus {{
        border-color: var(--accent-brand);
    }}
    .search-clear-btn {{
        position: absolute;
        right: 7px;
        color: var(--text-muted);
        cursor: pointer;
        font-size: 12px;
        user-select: none;
        display: none;
    }}

    .date-nav-group {{
        display: flex;
        align-items: center;
        gap: 2px;
        background: var(--ctrl-bg);
        border: 1px solid var(--ctrl-border);
        border-radius: 6px;
        padding: 2px 3px;
        flex-shrink: 0;
    }}

    .nav-arrow-btn {{
        background: transparent;
        border: none;
        color: var(--text-main);
        font-size: 13px;
        font-weight: bold;
        cursor: pointer;
        padding: 2px 5px;
        border-radius: 4px;
        display: flex;
        align-items: center;
        justify-content: center;
        user-select: none;
    }}
    .nav-arrow-btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}

    .date-select {{
        background: transparent;
        color: var(--text-main);
        border: none;
        padding: 2px 3px;
        font-size: 12px;
        font-weight: 800;
        outline: none;
        cursor: pointer;
    }}

    .month-select {{
        background: var(--ctrl-bg);
        color: var(--text-main);
        border: 1px solid var(--ctrl-border);
        padding: 4px 6px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 800;
        outline: none;
        flex-shrink: 0;
    }}

    .pill-btn {{
        background: var(--ctrl-bg);
        border: 1px solid var(--ctrl-border);
        color: var(--text-muted);
        padding: 4px 10px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 11.5px;
        font-weight: 700;
        transition: all 0.2s;
        flex-shrink: 0;
    }}
    .pill-btn.active-grade {{
        background: var(--ctrl-active-bg);
        color: var(--ctrl-active-text);
        border-color: var(--ctrl-active-bg);
    }}
    .pill-btn.active-a {{ background: #16a34a !important; border-color: #16a34a !important; color: #fff !important; }}
    .pill-btn.active-b {{ background: #2563eb !important; border-color: #2563eb !important; color: #fff !important; }}
    .pill-btn.active-c {{ background: #d97706 !important; border-color: #d97706 !important; color: #fff !important; }}

    .tabs-wrapper {{
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 6px;
        overflow-x: auto;
        scrollbar-width: none;
        padding-bottom: 2px;
    }}
    .tabs-wrapper::-webkit-scrollbar {{ display: none; }}
    
    .tab-pill {{
        background: var(--card-bg);
        color: var(--text-muted);
        border: 1px solid var(--card-border);
        padding: 5px 12px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 11.5px;
        font-weight: 800;
        white-space: nowrap;
        flex-shrink: 0;
        transition: all 0.2s;
    }}
    .tab-pill.active {{
        background: var(--ctrl-active-bg);
        color: var(--ctrl-active-text);
        border-color: var(--ctrl-active-bg);
    }}
    
    .checkbox-control-group {{
        display: flex;
        align-items: center;
        gap: 8px;
        margin-left: 4px;
        padding: 3px 8px;
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 6px;
        flex-shrink: 0;
    }}
    .custom-check-label {{
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 11px;
        color: var(--text-muted);
        cursor: pointer;
        user-select: none;
        white-space: nowrap;
    }}
    .custom-check-label input[type="checkbox"] {{
        accent-color: var(--accent-brand);
        width: 14px;
        height: 14px;
        cursor: pointer;
        margin: 0;
    }}
    
    .day-stat-strip {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: var(--card-bg);
        border-left: 3.5px solid var(--accent-brand);
        border-radius: 6px;
        padding: 6px 9px;
        margin-bottom: 6px;
        font-size: 11.5px;
        color: var(--text-main);
        white-space: nowrap;
        box-shadow: var(--card-shadow);
        border-top: 1px solid var(--card-border);
        border-right: 1px solid var(--card-border);
        border-bottom: 1px solid var(--card-border);
    }}

    .table-hint-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 10.5px;
        color: var(--text-muted);
        margin-bottom: 4px;
        padding: 0 2px;
    }}

    .table-wrapper {{
        width: 100%;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        background: var(--card-bg);
        border-radius: 9px;
        border: 1px solid var(--card-border);
        box-shadow: var(--card-shadow);
        margin-bottom: 15px;
    }}

    table {{
        border-collapse: separate;
        border-spacing: 0;
        width: 100%;
        font-size: 12px;
    }}

    th, td {{
        padding: 6px 5px;
        text-align: center;
        border-bottom: 1px solid var(--border-subtle);
        border-right: 1px solid var(--border-subtle);
        white-space: nowrap;
    }}

    th {{
        background: var(--table-th-bg);
        color: var(--text-muted);
        font-size: 11px;
        font-weight: 800;
        position: sticky;
        top: 0;
        z-index: 10;
        border-bottom: 1px solid var(--card-border);
    }}

    th.sortable {{
        cursor: pointer;
        user-select: none;
        color: var(--text-title) !important;
        background: var(--table-th-bg) !important;
    }}

    .sticky-stock {{
        position: sticky;
        left: 0px;
        background: var(--card-bg) !important;
        z-index: 5;
        min-width: 94px;
        box-shadow: 3px 0 8px rgba(0,0,0,0.04);
        padding: 6px 4px;
    }}
    th.sticky-stock {{ background: var(--table-th-bg) !important; z-index: 15; }}

    .stock-combo {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 2px;
    }}
    .stock-id-txt {{ font-size: 15px; font-weight: 900; color: var(--text-main); line-height: 1.1; }}
    .stock-name-txt {{ font-size: 12px; color: var(--accent-brand); line-height: 1.1; font-weight: 800; }}
    
    .date-badge-box {{
        background: var(--ctrl-bg);
        color: var(--text-muted);
        font-size: 9.5px;
        font-weight: 700;
        padding: 1px 5px;
        border-radius: 4px;
        border: 1px solid var(--ctrl-border);
        margin-bottom: 3px;
        cursor: pointer;
        user-select: none;
    }}

    .bonus-box {{
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 2px;
        margin-top: 3px;
        max-width: 94px;
    }}
    .bonus-tag {{
        display: inline-block;
        background: var(--accent-gold-bg);
        color: var(--accent-gold);
        font-size: 8.5px;
        padding: 1px 3px;
        border-radius: 3px;
        border: 1px solid rgba(217, 119, 6, 0.25);
        font-weight: 700;
        line-height: 1.1;
    }}
    .bonus-tag-gold {{
        background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
        color: #b45309;
        font-weight: 950;
        border: 1.5px solid #f59e0b;
        box-shadow: 0 1px 3px rgba(245, 158, 11, 0.3);
    }}
    /* 📈 突破下降趨勢專屬標籤 */
    .bonus-tag-trend {{
        display: inline-block;
        background: #e0f2fe;
        color: #0369a1;
        font-size: 8.5px;
        padding: 1px 3px;
        border-radius: 3px;
        border: 1px solid #7dd3fc;
        font-weight: 800;
        line-height: 1.1;
    }}
    .bonus-tag-trend-ideal {{
        display: inline-block;
        background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
        color: #047857;
        font-size: 8.5px;
        padding: 1px 3.5px;
        border-radius: 3px;
        border: 1.2px solid #10b981;
        font-weight: 900;
        line-height: 1.1;
        box-shadow: 0 1px 3px rgba(16, 185, 129, 0.25);
    }}

    .stage-grade-combo {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 3px;
    }}

    .entry-box {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 2px;
    }}
    .badge-trigger-ok {{
        background: var(--badge-blue-bg);
        color: var(--badge-blue);
        border: 1px solid rgba(37, 99, 235, 0.2);
        font-size: 9.5px;
        font-weight: 800;
        padding: 1px 4px;
        border-radius: 3px;
        line-height: 1.2;
    }}
    .badge-trigger-no {{
        background: var(--border-subtle);
        color: var(--text-muted);
        font-size: 9.5px;
        padding: 1px 4px;
        border-radius: 3px;
        line-height: 1.2;
    }}

    .badge-grade {{ display: inline-block; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 4px; font-weight: 800; font-size: 10px; color: #fff; }}
    .badge-grade-a {{ background: #16a34a; }}
    .badge-grade-b {{ background: #2563eb; }}
    .badge-grade-c {{ background: #d97706; }}
    .badge-tag {{ display: inline-block; padding: 1px 4px; border-radius: 3px; font-size: 9px; background: var(--border-subtle); color: var(--text-muted); font-weight: bold; border: 1px solid var(--card-border); }}

    .cell-box {{ display: flex; flex-direction: column; gap: 2px; align-items: center; min-width: 56px; }}
    .date-label {{ font-size: 9.5px; color: var(--text-muted); }}
    .close-price {{ font-size: 12px; font-weight: 700; color: var(--text-main); }}
    .day-val {{ font-size: 10.5px; font-weight: 800; padding: 0 1px; border-radius: 2px; }}
    .cum-box {{ font-size: 9.5px; padding: 1px 3px; border-radius: 3px; font-weight: 800; }}
    
    .c-red {{ color: var(--red-profit); }}
    .c-green {{ color: var(--green-loss); }}
    .c-gray {{ color: var(--text-muted); }}
    .limit-up {{ background: #e11d48 !important; color: #ffffff !important; font-weight: 900 !important; border-radius: 3px; padding: 0 3px !important; }}
    .bg-red {{ background: var(--red-profit-bg); color: var(--red-profit); }}
    .bg-green {{ background: var(--green-loss-bg); color: var(--green-loss); }}
    .bg-gray {{ background: var(--border-subtle); color: var(--text-muted); }}
    
    .badge-entry-day {{
        background: var(--badge-blue-bg) !important;
        color: var(--badge-blue) !important;
        border: 1px solid rgba(37, 99, 235, 0.25);
        font-weight: 800;
        font-size: 9px;
        padding: 1px 4px;
        border-radius: 3px;
    }}
    .badge-tp-10 {{
        background: var(--accent-gold-bg) !important;
        color: var(--accent-gold) !important;
        border: 1px solid rgba(217, 119, 6, 0.3);
        font-weight: 900;
        font-size: 9px;
        padding: 1px 4px;
        border-radius: 3px;
    }}
    .badge-tp-20 {{
        background: var(--red-profit-bg) !important;
        color: var(--red-profit) !important;
        border: 1px solid rgba(225, 29, 72, 0.3);
        font-weight: 900;
        font-size: 9px;
        padding: 1px 4px;
        border-radius: 3px;
    }}
    .badge-stop-15 {{
        background: #fee2e2 !important;
        color: #b91c1c !important;
        border: 1px solid #f87171;
        font-weight: 800;
        font-size: 9px;
        padding: 1px 4px;
        border-radius: 3px;
    }}
    .badge-stop-7 {{
        background: #ffedd5 !important;
        color: #c2410c !important;
        border: 1px solid #fb923c;
        font-weight: 800;
        font-size: 9px;
        padding: 1px 4px;
        border-radius: 3px;
    }}
    .badge-share-1 {{
        background: var(--border-subtle);
        color: var(--text-muted);
        font-size: 8.5px;
        border: 1px solid var(--card-border);
        padding: 1px 3px;
        border-radius: 3px;
    }}

    .waiting-box {{ opacity: 0.4; }}
    .exited-box {{ opacity: 0.55; }}
    
    .status-highlight {{ font-size: 11px; font-weight: 800; }}
    .st-holding {{ color: var(--badge-blue); }}
    .st-tp {{ color: var(--accent-gold); }}
    .st-winall {{ color: var(--red-profit); }}
    .st-stop {{ color: #dc2626; }}
    .st-wait {{ color: var(--text-muted); }}
    
    .ret-highlight {{
        font-weight: 900;
        font-size: 13.5px;
        background: var(--card-bg);
        min-width: 66px;
    }}

    .max-highlight {{
        font-weight: 900;
        font-size: 13.5px;
        background: var(--card-bg);
        min-width: 74px;
    }}
    .max-super {{
        color: var(--accent-gold) !important;
    }}
    .max-good {{
        color: var(--red-profit) !important;
    }}
</style>
</head>
<body>

<div class="header-area">
    <div style="display:flex; align-items:center; gap:8px;">
        <h1>🚀 趙阿瑋要賺爆了</h1>
    </div>
    
    <div class="theme-switch-group">
        <button class="theme-btn active" id="btnThemeClean" onclick="setAppTheme('clean')">⚪ 質感風格</button>
        <button class="theme-btn" id="btnThemeDark" onclick="setAppTheme('dark')">🟣 深紫金</button>
    </div>
</div>

<div class="scheme-bar">
    <div class="scheme-btn-row">
        <span class="scheme-label">🎯 停損停利方案：</span>
        <button class="scheme-btn active" id="btnScheme-scheme_a" onclick="selectScheme('scheme_a', true)">🏆 方案A：7天全能王</button>
        <button class="scheme-btn" id="btnScheme-scheme_b" onclick="selectScheme('scheme_b', true)">💰 方案B：10天獲利王</button>
        <button class="scheme-btn" id="btnScheme-scheme_c" onclick="selectScheme('scheme_c', true)">⚡ 方案C：三策略專屬最強</button>
        <button class="scheme-btn" id="btnScheme-scheme_orig" onclick="selectScheme('scheme_orig', true)">📌 原始設定 (-8.5%)</button>
    </div>
    <div class="legend-tag" id="dynamicLegendBar"></div>
</div>

<div class="strat-scope-header">
    <span class="strat-scope-title" id="stratScopeTitle">📅 當月績效總覽</span>
    <div style="display:flex; gap:4px;">
        <button class="scope-toggle-btn active" id="btnScopeMonth" onclick="setStatScope('month')">按當月</button>
        <button class="scope-toggle-btn" id="btnScopeAll" onclick="setStatScope('all')">全歷史</button>
    </div>
</div>

<div class="overall-compact">
    <div class="mini-strat-card">
        <div class="mini-strat-title">
            <span>👑 領頭 (LEADERS)</span>
            <span class="mini-strat-rule-tag" id="card-leaders-rule"></span>
        </div>
        <div class="mini-strat-highlight" id="card-leaders-max">最高: --</div>
        <div class="mini-stat-ret" id="card-leaders-ret">--</div>
        <div class="mini-stat-sub-win" id="card-leaders-wr">--</div>
        <div class="mini-strat-quant">
            <span>風暴比 <b id="card-leaders-plr">--</b></span>
            <span>夏普 <b id="card-leaders-sr">--</b></span>
        </div>
        <div class="mini-strat-sub">
            <span>達標 <b id="card-leaders-hit">--</b></span>
            <span>進場 <span id="card-leaders-cnt">--</span></span>
        </div>
    </div>
    <div class="mini-strat-card">
        <div class="mini-strat-title">
            <span>🚀 突破 (BREAKOUT)</span>
            <span class="mini-strat-rule-tag" id="card-breakout-rule"></span>
        </div>
        <div class="mini-strat-highlight" id="card-breakout-max">最高: --</div>
        <div class="mini-stat-ret" id="card-breakout-ret">--</div>
        <div class="mini-stat-sub-win" id="card-breakout-wr">--</div>
        <div class="mini-strat-quant">
            <span>風暴比 <b id="card-breakout-plr">--</b></span>
            <span>夏普 <b id="card-breakout-sr">--</b></span>
        </div>
        <div class="mini-strat-sub">
            <span>達標 <b id="card-breakout-hit">--</b></span>
            <span>進場 <span id="card-breakout-cnt">--</span></span>
        </div>
    </div>
    <div class="mini-strat-card">
        <div class="mini-strat-title">
            <span>⏳ 蓄勢 (READY)</span>
            <span class="mini-strat-rule-tag" id="card-ready-rule"></span>
        </div>
        <div class="mini-strat-highlight" id="card-ready-max">最高: --</div>
        <div class="mini-stat-ret" id="card-ready-ret">--</div>
        <div class="mini-stat-sub-win" id="card-ready-wr">--</div>
        <div class="mini-strat-quant">
            <span>風暴比 <b id="card-ready-plr">--</b></span>
            <span>夏普 <b id="card-ready-sr">--</b></span>
        </div>
        <div class="mini-strat-sub">
            <span>達標 <b id="card-ready-hit">--</b></span>
            <span>進場 <span id="card-ready-cnt">--</span></span>
        </div>
    </div>
</div>

<div class="controls-wrapper">
    <div class="ctrl-row">
        <select id="monthSelector" class="month-select" onchange="onMonthChange()"></select>
        <div class="date-nav-group">
            <button class="nav-arrow-btn" id="btnPrevDate" onclick="shiftDate(-1)" title="前一個交易日">◀</button>
            <select id="daySelector" class="date-select" onchange="applyFilters()"></select>
            <button class="nav-arrow-btn" id="btnNextDate" onclick="shiftDate(1)" title="後一個交易日">▶</button>
        </div>
        
        <div class="days-navigator-wrapper">
            <select id="daysDropdown" class="days-select-dropdown" onchange="onDaysDropdownChange()"></select>
            <button class="days-nav-btn" id="btnPrevDayStep" onclick="shiftDayStep(-1)" title="前一天期">◀</button>
            <div class="days-scroll-group" id="daysBtnContainer"></div>
            <button class="days-nav-btn" id="btnNextDayStep" onclick="shiftDayStep(1)" title="後一天期">▶</button>
        </div>
    </div>

    <div class="ctrl-row">
        <div class="search-box-wrapper">
            <input type="text" id="stockSearchInput" class="search-input" placeholder="🔍 跨全歷史搜尋代號/名稱..." oninput="onSearchInput()">
            <span id="searchClearBtn" class="search-clear-btn" onclick="clearSearch()">✕</span>
        </div>
        <button class="pill-btn active-grade" id="btn-grade-ALL" onclick="setGradeFilter('ALL')">全部評分</button>
        <button class="pill-btn" id="btn-grade-A" onclick="setGradeFilter('A')">🏆 A級</button>
        <button class="pill-btn" id="btn-grade-B" onclick="setGradeFilter('B')">⭐ B級</button>
        <button class="pill-btn" id="btn-grade-C" onclick="setGradeFilter('C')">🔹 C級</button>
    </div>
</div>

<div class="tabs-wrapper">
    <button class="tab-pill" id="tab-btn-ALL" onclick="switchCategory('ALL')">全部 (<span id="count-ALL">0</span>)</button>
    <button class="tab-pill active" id="tab-btn-LEADERS" onclick="switchCategory('LEADERS')">領頭 (<span id="count-LEADERS">0</span>)</button>
    <button class="tab-pill" id="tab-btn-BREAKOUT" onclick="switchCategory('BREAKOUT')">突破 (<span id="count-BREAKOUT">0</span>)</button>
    <button class="tab-pill" id="tab-btn-READY" onclick="switchCategory('READY')">蓄勢 (<span id="count-READY">0</span>)</button>
    
    <div class="checkbox-control-group">
        <label class="custom-check-label" title="過濾連接前波高點形成下降趨勢線，並向上突破轉強的個股">
            <input type="checkbox" id="chkTrendBreak" onchange="onCheckboxToggle()">
            <span style="color:var(--accent-brand); font-weight:800;">📈 突破下降趨勢</span>
        </label>
        <label class="custom-check-label">
            <input type="checkbox" id="chkOnlyD1" onchange="onCheckboxToggle()">
            <span>🎯 只看 D1 首日進場</span>
        </label>
        <label class="custom-check-label">
            <input type="checkbox" id="chkShowStage" onchange="onCheckboxToggle()">
            <span>分級評分</span>
        </label>
        <label class="custom-check-label">
            <input type="checkbox" id="chkShowStops" onchange="onCheckboxToggle()">
            <span>停損停利</span>
        </label>
    </div>
</div>

<div class="day-stat-strip" id="dayInlineStat">
    <span>當日入選標的</span>
    <span>勝率: -- | 均報: --</span>
</div>

<div class="table-hint-row">
    <span>💡 點「累計」或「最高衝刺」可排序</span>
    <span>👉 表格向右滑</span>
</div>

<div id="tableContainer"></div>

<script>
const RAW_DATA = {json_data_str};

const SCHEMES = {{
    'scheme_a': {{
        name: '方案A：7天全能王',
        defaultDays: 7,
        autoDayByTab: false,
        rules: {{
            'LEADERS':  {{ sl: -5.0, tp1: 15.0, tp2: 30.0, sl2: 2.0, bestD: 7 }},
            'BREAKOUT': {{ sl: -5.0, tp1: 15.0, tp2: 30.0, sl2: 2.0, bestD: 7 }},
            'READY':    {{ sl: -5.0, tp1: 15.0, tp2: 30.0, sl2: 2.0, bestD: 7 }}
        }}
    }},
    'scheme_b': {{
        name: '方案B：10天獲利王',
        defaultDays: 10,
        autoDayByTab: false,
        rules: {{
            'LEADERS':  {{ sl: -5.0, tp1: 15.0, tp2: 25.0, sl2: 2.0, bestD: 10 }},
            'BREAKOUT': {{ sl: -5.0, tp1: 15.0, tp2: 25.0, sl2: 2.0, bestD: 10 }},
            'READY':    {{ sl: -5.0, tp1: 15.0, tp2: 25.0, sl2: 2.0, bestD: 10 }}
        }}
    }},
    'scheme_c': {{
        name: '方案C：三策略專屬最強',
        defaultDays: 10,
        autoDayByTab: true,
        rules: {{
            'LEADERS':  {{ sl: -5.0,  tp1: 15.0, tp2: 25.0, sl2: 2.0, bestD: 10 }},
            'BREAKOUT': {{ sl: -10.0, tp1: 15.0, tp2: 30.0, sl2: 2.0, bestD: 4 }},
            'READY':    {{ sl: -5.0,  tp1: 10.0, tp2: 30.0, sl2: 2.0, bestD: 22 }}
        }}
    }},
    'scheme_orig': {{
        name: '原始設定 (-8.5%)',
        defaultDays: 10,
        autoDayByTab: false,
        rules: {{
            'LEADERS':  {{ sl: -8.5, tp1: 10.0, tp2: 20.0, sl2: 0.0, bestD: 10 }},
            'BREAKOUT': {{ sl: -8.5, tp1: 10.0, tp2: 20.0, sl2: 0.0, bestD: 10 }},
            'READY':    {{ sl: -8.5, tp1: 10.0, tp2: 20.0, sl2: 0.0, bestD: 10 }}
        }}
    }}
}};

let currentScheme = 'scheme_a';
let currentCategory = 'LEADERS';
let currentGrade = 'ALL';
let currentDays = 7;
let searchQuery = '';
let sortField = 'none';
let sortDirection = 'none';
let statScope = 'month';

let showStageCol = false;
let showStopsCol = false;
let onlyD1Filter = false;
let onlyTrendBreakFilter = false;

function setAppTheme(theme) {{
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('vcp_theme_pref', theme);
    
    const btnClean = document.getElementById('btnThemeClean');
    const btnDark = document.getElementById('btnThemeDark');
    if (btnClean && btnDark) {{
        btnClean.classList.toggle('active', theme === 'clean');
        btnDark.classList.toggle('active', theme === 'dark');
    }}
}}

function initTheme() {{
    const saved = localStorage.getItem('vcp_theme_pref') || 'clean';
    setAppTheme(saved);
}}

function recalculateAllTrades() {{
    const schemeCfg = SCHEMES[currentScheme] || SCHEMES['scheme_a'];

    Object.keys(RAW_DATA).forEach(d => {{
        const list = RAW_DATA[d] || [];
        list.forEach(r => {{
            const rule = schemeCfg.rules[r.stage] || schemeCfg.rules['LEADERS'];
            r._rule = rule;

            const slMult = 1.0 + (rule.sl / 100.0);
            const tp1Mult = 1.0 + (rule.tp1 / 100.0);
            const tp2Mult = 1.0 + (rule.tp2 / 100.0);
            const sl2Mult = 1.0 + (rule.sl2 / 100.0);

            const targetPivot = r.target_pivot;
            let hasEntered = false;
            let entryDayNum = null;
            let entryPrice = targetPivot;
            let heldShares = 0;
            let realizedRetSum = 0.0;
            let statusDesc = '待突破';
            let lastClose = targetPivot;
            const dailyList = [];

            for (let dayIdx = 0; dayIdx < 30; dayIdx++) {{
                const dNum = dayIdx + 1;
                const bar = r.raw_bars[dayIdx];

                if (bar !== null && bar !== undefined) {{
                    const currDate = bar[0];
                    const currClose = bar[1];
                    const currHigh = bar[2];

                    let dayPct = null;
                    let cumPct = null;
                    let stateTag = 'waiting';

                    if (!hasEntered) {{
                        if (targetPivot && currHigh >= targetPivot) {{
                            hasEntered = true;
                            entryDayNum = dNum;
                            entryPrice = targetPivot;
                            heldShares = 2;
                            statusDesc = '持有中(2張)';
                            stateTag = 'entry_first';
                            const curFloatPct = Math.round(((currClose / entryPrice) - 1.0) * 1000) / 10;
                            cumPct = curFloatPct;
                            dayPct = curFloatPct;
                            lastClose = currClose;
                        }} else {{
                            stateTag = 'waiting';
                        }}
                    }} else {{
                        dayPct = Math.round(((currClose / lastClose) - 1.0) * 1000) / 10;
                        const curFloatPct = Math.round(((currClose / entryPrice) - 1.0) * 1000) / 10;

                        if (heldShares === 2) {{
                            if (currClose < (entryPrice * slMult)) {{
                                heldShares = 0;
                                realizedRetSum = curFloatPct * 2.0;
                                stateTag = 'stopout_15';
                                statusDesc = `D${{dNum}} 破${{rule.sl}}%全出`;
                            }} else if (currHigh >= (entryPrice * tp1Mult)) {{
                                heldShares = 1;
                                realizedRetSum += rule.tp1;
                                stateTag = 'tp_10';
                                statusDesc = `D${{dNum}} 達+${{rule.tp1}}%停利1張`;
                            }} else {{
                                stateTag = 'holding_2';
                            }}
                        }} else if (heldShares === 1) {{
                            if (currClose < (entryPrice * sl2Mult)) {{
                                heldShares = 0;
                                realizedRetSum += Math.max(curFloatPct, rule.sl2);
                                stateTag = 'stopout_7';
                                statusDesc = rule.sl2 > 0 ? `D${{dNum}} 鎖利+${{rule.sl2}}%出餘單` : `D${{dNum}} 保本出餘單`;
                            }} else if (currHigh >= (entryPrice * tp2Mult)) {{
                                heldShares = 0;
                                realizedRetSum += rule.tp2;
                                stateTag = 'tp_20';
                                statusDesc = `D${{dNum}} 達+${{rule.tp2}}%全停利`;
                            }} else {{
                                stateTag = 'holding_1';
                            }}
                        }} else {{
                            stateTag = 'all_exited';
                        }}

                        if (heldShares === 2) {{
                            cumPct = curFloatPct;
                        }} else if (heldShares === 1) {{
                            cumPct = Math.round(((realizedRetSum + curFloatPct) / 2.0) * 10) / 10;
                        }} else {{
                            cumPct = Math.round((realizedRetSum / 2.0) * 10) / 10;
                        }}

                        lastClose = currClose;
                    }}

                    dailyList.push({{
                        day: dNum,
                        date: currDate,
                        close: currClose,
                        day_pct: dayPct,
                        cum_pct: cumPct,
                        state: stateTag,
                        shares: heldShares,
                        status_so_far: hasEntered ? statusDesc : '尚未觸發'
                    }});
                }} else {{
                    dailyList.push({{
                        day: dNum,
                        date: '--',
                        close: null,
                        day_pct: null,
                        cum_pct: null,
                        state: 'nodata',
                        shares: heldShares,
                        status_so_far: hasEntered ? statusDesc : '尚未觸發'
                    }});
                }}
            }}

            if (!hasEntered) statusDesc = '尚未觸發';

            r.has_entered = hasEntered;
            r.entry_day_num = entryDayNum;
            r.entry_price = entryPrice ? Math.round(entryPrice * 100) / 100 : null;
            r.stop_loss_init = entryPrice ? Math.round(entryPrice * slMult * 100) / 100 : null;
            r.stop_loss_lock = entryPrice ? Math.round(entryPrice * sl2Mult * 100) / 100 : null;
            r.tp_price_1 = entryPrice ? Math.round(entryPrice * tp1Mult * 100) / 100 : null;
            r.tp_price_2 = entryPrice ? Math.round(entryPrice * tp2Mult * 100) / 100 : null;
            r.daily_list = dailyList;
            r.break_stop = statusDesc;
        }});
    }});
}}

function updateLegendDisplay() {{
    const schemeCfg = SCHEMES[currentScheme] || SCHEMES['scheme_a'];
    const activeStage = currentCategory === 'ALL' ? 'LEADERS' : currentCategory;
    const rule = schemeCfg.rules[activeStage] || schemeCfg.rules['LEADERS'];
    const lockTxt = rule.sl2 > 0 ? `鎖利+${{rule.sl2}}%出餘單` : `保本0%出餘單`;

    let prefixNote = '';
    if (currentScheme === 'scheme_c') {{
        const stName = activeStage === 'LEADERS' ? '領頭' : (activeStage === 'BREAKOUT' ? '突破' : '蓄勢');
        prefixNote = `<span style="font-weight:800; color:var(--accent-brand);">【${{stName}}專屬最佳 ${{rule.bestD}}D】</span>`;
    }}

    document.getElementById('dynamicLegendBar').innerHTML = `
        ${{prefixNote}}
        <span><span class="badge-entry-day">🎯進場(2張)</span></span>
        <span><span class="badge-tp-10">💰+${{rule.tp1}}%出1張</span></span>
        <span><span class="badge-tp-20">🎉+${{rule.tp2}}%全出</span></span>
        <span><span class="badge-stop-15">🛑破${{rule.sl}}%全出</span></span>
        <span><span class="badge-stop-7">🔒${{lockTxt}}</span></span>
        <span><span class="bonus-tag-trend-ideal">📈帶量破趨勢(理想)</span></span>
    `;

    ['LEADERS', 'BREAKOUT', 'READY'].forEach(st => {{
        const r = schemeCfg.rules[st];
        const el = document.getElementById(`card-${{st.toLowerCase()}}-rule`);
        if (el && r) {{
            el.innerText = `${{r.sl}}% / +${{r.tp1}}% / +${{r.tp2}}%`;
        }}
    }});
}}

function selectScheme(schemeKey, jumpDays) {{
    if (!SCHEMES[schemeKey]) schemeKey = 'scheme_a';
    currentScheme = schemeKey;
    localStorage.setItem('vcp_scheme_pref', schemeKey);

    document.querySelectorAll('[id^="btnScheme-"]').forEach(btn => btn.classList.remove('active'));
    const activeBtn = document.getElementById('btnScheme-' + schemeKey);
    if (activeBtn) activeBtn.classList.add('active');

    recalculateAllTrades();

    if (jumpDays) {{
        const cfg = SCHEMES[schemeKey];
        if (cfg.autoDayByTab) {{
            const st = currentCategory === 'ALL' ? 'LEADERS' : currentCategory;
            setHoldingDays(cfg.rules[st].bestD);
            return;
        }} else {{
            setHoldingDays(cfg.defaultDays);
            return;
        }}
    }}

    updateLegendDisplay();
    applyFilters();
}}

function initDaysButtons() {{
    const container = document.getElementById('daysBtnContainer');
    const dropdown = document.getElementById('daysDropdown');
    
    container.innerHTML = '';
    dropdown.innerHTML = '';

    for (let d = 1; d <= 30; d++) {{
        const btn = document.createElement('button');
        btn.id = 'btn-days-' + d;
        btn.className = 'pill-day-btn' + (d === currentDays ? ' active-days' : '');
        btn.innerText = d + 'D';
        btn.onclick = () => setHoldingDays(d);
        container.appendChild(btn);

        const opt = document.createElement('option');
        opt.value = d;
        opt.innerText = d + '天';
        if (d === currentDays) opt.selected = true;
        dropdown.appendChild(opt);
    }}
    updateDayNavArrows();
}}

function onDaysDropdownChange() {{
    const val = parseInt(document.getElementById('daysDropdown').value, 10);
    setHoldingDays(val);
}}

function shiftDayStep(step) {{
    const target = currentDays + step;
    if (target >= 1 && target <= 30) {{
        setHoldingDays(target);
    }}
}}

function updateDayNavArrows() {{
    const prevBtn = document.getElementById('btnPrevDayStep');
    const nextBtn = document.getElementById('btnNextDayStep');
    if (prevBtn) prevBtn.disabled = (currentDays <= 1);
    if (nextBtn) nextBtn.disabled = (currentDays >= 30);
}}

function onCheckboxToggle() {{
    const chkStage = document.getElementById('chkShowStage');
    const chkStops = document.getElementById('chkShowStops');
    const chkOnlyD1 = document.getElementById('chkOnlyD1');
    const chkTrendBreak = document.getElementById('chkTrendBreak');
    showStageCol = chkStage ? chkStage.checked : false;
    showStopsCol = chkStops ? chkStops.checked : false;
    onlyD1Filter = chkOnlyD1 ? chkOnlyD1.checked : false;
    onlyTrendBreakFilter = chkTrendBreak ? chkTrendBreak.checked : false;
    applyFilters();
}}

function getAllSortedDates() {{
    return Object.keys(RAW_DATA).sort().reverse();
}}

function initDatePickers() {{
    const allDates = getAllSortedDates();
    if (allDates.length === 0) return;

    const monthMap = {{}};
    allDates.forEach(d => {{
        const m = d.substring(0, 7);
        if (!monthMap[m]) monthMap[m] = [];
        monthMap[m].push(d);
    }});

    const monthSelector = document.getElementById('monthSelector');
    monthSelector.innerHTML = '';
    
    Object.keys(monthMap).sort().reverse().forEach(m => {{
        const opt = document.createElement('option');
        opt.value = m;
        opt.innerText = m.replace('-', '/') + '月';
        monthSelector.appendChild(opt);
    }});

    onMonthChange();
}}

function onMonthChange() {{
    const selectedMonth = document.getElementById('monthSelector').value;
    const allDates = getAllSortedDates();
    const daySelector = document.getElementById('daySelector');
    daySelector.innerHTML = '';

    const daysInMonth = allDates.filter(d => d.startsWith(selectedMonth));
    daysInMonth.forEach(d => {{
        const opt = document.createElement('option');
        opt.value = d;
        opt.innerText = d.substring(5);
        daySelector.appendChild(opt);
    }});

    updateNavArrowState();
    applyFilters();
}}

function onSearchInput() {{
    const val = document.getElementById('stockSearchInput').value.trim();
    searchQuery = val.toLowerCase();
    const clearBtn = document.getElementById('searchClearBtn');
    if (clearBtn) {{
        clearBtn.style.display = searchQuery ? 'block' : 'none';
    }}
    applyFilters();
}}

function clearSearch() {{
    const input = document.getElementById('stockSearchInput');
    input.value = '';
    searchQuery = '';
    const clearBtn = document.getElementById('searchClearBtn');
    if (clearBtn) clearBtn.style.display = 'none';
    applyFilters();
}}

function jumpToDate(targetDate) {{
    const targetMonth = targetDate.substring(0, 7);
    const monthSelector = document.getElementById('monthSelector');
    if (monthSelector.value !== targetMonth) {{
        monthSelector.value = targetMonth;
        onMonthChange();
    }}
    document.getElementById('daySelector').value = targetDate;
    clearSearch();
}}

function setStatScope(scope) {{
    statScope = scope;
    document.getElementById('btnScopeMonth').classList.toggle('active', scope === 'month');
    document.getElementById('btnScopeAll').classList.toggle('active', scope === 'all');
    applyFilters();
}}

function shiftDate(offset) {{
    const allDates = getAllSortedDates();
    const currentSelected = document.getElementById('daySelector').value;
    const curIdx = allDates.indexOf(currentSelected);
    if (curIdx === -1) return;

    const targetIdx = curIdx - offset;
    if (targetIdx >= 0 && targetIdx < allDates.length) {{
        const targetDate = allDates[targetIdx];
        const targetMonth = targetDate.substring(0, 7);
        
        const monthSelector = document.getElementById('monthSelector');
        if (monthSelector.value !== targetMonth) {{
            monthSelector.value = targetMonth;
            onMonthChange();
        }}
        
        document.getElementById('daySelector').value = targetDate;
        updateNavArrowState();
        applyFilters();
    }}
}}

function updateNavArrowState() {{
    const allDates = getAllSortedDates();
    const currentSelected = document.getElementById('daySelector').value;
    const curIdx = allDates.indexOf(currentSelected);
    
    const prevBtn = document.getElementById('btnPrevDate');
    const nextBtn = document.getElementById('btnNextDate');
    
    if (prevBtn) prevBtn.disabled = (curIdx >= allDates.length - 1);
    if (nextBtn) nextBtn.disabled = (curIdx <= 0);
}}

function setHoldingDays(days) {{
    currentDays = days;
    
    const dropdown = document.getElementById('daysDropdown');
    if (dropdown && dropdown.value != days) {{
        dropdown.value = days;
    }}

    for (let d = 1; d <= 30; d++) {{
        const btn = document.getElementById('btn-days-' + d);
        if (btn) btn.classList.remove('active-days');
    }}
    const curBtn = document.getElementById('btn-days-' + days);
    if (curBtn) {{
        curBtn.classList.add('active-days');
        curBtn.scrollIntoView({{ behavior: 'smooth', block: 'nearest', inline: 'center' }});
    }}
    
    updateDayNavArrows();
    updateLegendDisplay();
    applyFilters();
}}

function setGradeFilter(grade) {{
    currentGrade = grade;
    document.querySelectorAll('[id^="btn-grade-"]').forEach(btn => {{
        btn.classList.remove('active-grade', 'active-a', 'active-b', 'active-c');
    }});
    
    const activeBtn = document.getElementById('btn-grade-' + grade);
    if (activeBtn) {{
        if (grade === 'A') activeBtn.classList.add('active-a');
        else if (grade === 'B') activeBtn.classList.add('active-b');
        else if (grade === 'C') activeBtn.classList.add('active-c');
        else activeBtn.classList.add('active-grade');
    }}
    applyFilters();
}}

function switchCategory(cat) {{
    currentCategory = cat;
    document.querySelectorAll('.tab-pill').forEach(btn => btn.classList.remove('active'));
    document.getElementById('tab-btn-' + cat).classList.add('active');

    const cfg = SCHEMES[currentScheme];
    if (cfg && cfg.autoDayByTab && cat !== 'ALL' && cfg.rules[cat]) {{
        setHoldingDays(cfg.rules[cat].bestD);
        return;
    }}

    updateLegendDisplay();
    applyFilters();
}}

function toggleSortField(field) {{
    if (sortField !== field) {{
        sortField = field;
        sortDirection = 'desc';
    }} else {{
        if (sortDirection === 'desc') sortDirection = 'asc';
        else if (sortDirection === 'asc') {{
            sortDirection = 'none';
            sortField = 'none';
        }}
    }}
    applyFilters();
}}

function updateOverallMetrics() {{
    const selectedMonth = document.getElementById('monthSelector').value;
    const titleEl = document.getElementById('stratScopeTitle');
    const schemeName = (SCHEMES[currentScheme] || SCHEMES['scheme_a']).name;
    const filterSuffix = onlyTrendBreakFilter ? '｜📈突破下降趨勢' : '';
    
    if (statScope === 'month') {{
        const mParts = selectedMonth.split('-');
        const monthTxt = mParts.length === 2 ? `${{parseInt(mParts[1])}} 月份` : selectedMonth;
        titleEl.innerText = `📅 【${{monthTxt}}】各策略累積績效 (${{currentDays}}天期｜${{schemeName}}${{filterSuffix}})`;
    }} else {{
        titleEl.innerText = `🏆 【全歷史總計】各策略累積績效 (${{currentDays}}天期｜${{schemeName}}${{filterSuffix}})`;
    }}

    const stratStats = {{
        'LEADERS': {{ returns: [], hit8Count: 0, maxRetSum: 0 }},
        'BREAKOUT': {{ returns: [], hit8Count: 0, maxRetSum: 0 }},
        'READY': {{ returns: [], hit8Count: 0, maxRetSum: 0 }}
    }};

    Object.keys(RAW_DATA).forEach(d => {{
        if (statScope === 'month' && !d.startsWith(selectedMonth)) {{
            return;
        }}

        const dayList = RAW_DATA[d] || [];
        dayList.forEach(r => {{
            if (!r.has_entered) return;
            if (onlyD1Filter && r.entry_day_num !== 1) return;
            if (onlyTrendBreakFilter && !r.trend_break) return;

            let subDays = r.daily_list.slice(0, currentDays);
            let cumActual = subDays.filter(x => x.cum_pct !== null);

            if (cumActual.length > 0) {{
                const latestCumObj = cumActual[cumActual.length - 1];
                const exactRet = latestCumObj.cum_pct;

                stratStats[r.stage].returns.push(exactRet);

                let actualVals = cumActual.map(x => x.cum_pct);
                let maxP = Math.max(...actualVals);
                stratStats[r.stage].maxRetSum += maxP;
                if (maxP >= 8.0) {{
                    stratStats[r.stage].hit8Count++;
                }}
            }}
        }});
    }});

    ['LEADERS', 'BREAKOUT', 'READY'].forEach(st => {{
        const lowerKey = st.toLowerCase();
        const data = stratStats[st];
        const cntEl = document.getElementById(`card-${{lowerKey}}-cnt`);
        const maxEl = document.getElementById(`card-${{lowerKey}}-max`);
        const hitEl = document.getElementById(`card-${{lowerKey}}-hit`);
        const retEl = document.getElementById(`card-${{lowerKey}}-ret`);
        const wrEl = document.getElementById(`card-${{lowerKey}}-wr`);
        const plrEl = document.getElementById(`card-${{lowerKey}}-plr`);
        const srEl = document.getElementById(`card-${{lowerKey}}-sr`);

        const totalTrades = data.returns.length;

        if (totalTrades === 0) {{
            cntEl.innerText = '0檔';
            maxEl.innerText = '最高: --';
            hitEl.innerText = '--';
            retEl.innerText = '--';
            wrEl.innerText = '';
            plrEl.innerText = '--';
            srEl.innerText = '--';
        }} else {{
            const winTrades = data.returns.filter(r => r > 0);
            const lossTrades = data.returns.filter(r => r <= 0);

            const wr = (winTrades.length / totalTrades * 100).toFixed(1);
            const hitRate = (data.hit8Count / totalTrades * 100).toFixed(1);
            
            const totalRetSum = data.returns.reduce((a, b) => a + b, 0);
            const avgRet = (totalRetSum / totalTrades);
            const avgMax = (data.maxRetSum / totalTrades).toFixed(1);
            const cumSign = totalRetSum >= 0 ? '+' : '';
            const avgSign = avgRet >= 0 ? '+' : '';

            let profitLossRatioStr = '--';
            if (winTrades.length > 0 && lossTrades.length > 0) {{
                const avgWin = winTrades.reduce((a, b) => a + b, 0) / winTrades.length;
                const avgLoss = Math.abs(lossTrades.reduce((a, b) => a + b, 0) / lossTrades.length);
                if (avgLoss > 0) {{
                    profitLossRatioStr = (avgWin / avgLoss).toFixed(2);
                }}
            }} else if (winTrades.length > 0 && lossTrades.length === 0) {{
                profitLossRatioStr = '∞';
            }}

            let sharpeRatioStr = '--';
            if (totalTrades > 1) {{
                const mean = avgRet;
                const variance = data.returns.reduce((sum, r) => sum + Math.pow(r - mean, 2), 0) / (totalTrades - 1);
                const stdDev = Math.sqrt(variance);
                const periodsPerYear = 250.0 / currentDays;
                const annualFactor = Math.sqrt(periodsPerYear);
                const rf = 0.05;
                
                if (stdDev > 0) {{
                    const rawSharpe = (mean - rf) / stdDev;
                    const annualizedSharpe = rawSharpe * annualFactor;
                    sharpeRatioStr = annualizedSharpe.toFixed(2);
                }}
            }}

            cntEl.innerText = `${{totalTrades}}檔`;
            maxEl.innerText = `均最高 +${{avgMax}}%`;
            hitEl.innerText = `${{hitRate}}%`;
            
            retEl.innerText = `累積 ${{cumSign}}${{totalRetSum.toFixed(1)}}%`;
            retEl.className = totalRetSum >= 0 ? 'c-red mini-stat-ret' : 'c-green mini-stat-ret';
            wrEl.innerText = `均${{avgSign}}${{avgRet.toFixed(1)}}% (勝${{wr}}%)`;
            plrEl.innerText = profitLossRatioStr;
            srEl.innerText = sharpeRatioStr;
        }}
    }});
}}

function applyFilters() {{
    updateOverallMetrics();
    updateNavArrowState();

    const selectedDate = document.getElementById('daySelector').value;
    if (!selectedDate) return;

    let sourceRecords = [];
    const isGlobalSearch = Boolean(searchQuery);

    if (isGlobalSearch) {{
        const allSortedDates = getAllSortedDates();
        allSortedDates.forEach(d => {{
            const list = RAW_DATA[d] || [];
            list.forEach(r => {{
                if (r.stock_id.toLowerCase().includes(searchQuery) || r.name.toLowerCase().includes(searchQuery)) {{
                    sourceRecords.push(r);
                }}
            }});
        }});
    }} else {{
        sourceRecords = RAW_DATA[selectedDate] || [];
    }}

    let baseListForCounts = currentGrade === 'ALL' 
        ? sourceRecords 
        : sourceRecords.filter(r => r.grade === currentGrade);

    if (onlyTrendBreakFilter) {{
        baseListForCounts = baseListForCounts.filter(r => r.trend_break);
    }}
    if (onlyD1Filter) {{
        baseListForCounts = baseListForCounts.filter(r => r.has_entered && r.entry_day_num === 1);
    }}

    const counts = {{ 'ALL': baseListForCounts.length, 'BREAKOUT': 0, 'READY': 0, 'LEADERS': 0 }};
    baseListForCounts.forEach(r => {{
        if (counts[r.stage] !== undefined) counts[r.stage]++;
    }});
    
    document.getElementById('count-ALL').innerText = counts['ALL'];
    document.getElementById('count-LEADERS').innerText = counts['LEADERS'];
    document.getElementById('count-BREAKOUT').innerText = counts['BREAKOUT'];
    document.getElementById('count-READY').innerText = counts['READY'];

    let filteredList = [...baseListForCounts];
    if (currentCategory !== 'ALL') {{
        filteredList = filteredList.filter(r => r.stage === currentCategory);
    }}

    const totalCount = filteredList.length;
    let validSampleCount = 0;
    let winCount = 0;
    let hit8Count = 0;
    let totalRetSum = 0;
    let maxDaysAvailable = 0;

    filteredList.forEach(r => {{
        let subDays = r.daily_list.slice(0, currentDays);
        let actual = subDays.filter(x => x.close !== null);
        if (actual.length > maxDaysAvailable) {{
            maxDaysAvailable = actual.length;
        }}
        
        if (r.has_entered) {{
            let cumActual = subDays.filter(x => x.cum_pct !== null);
            if (cumActual.length > 0) {{
                const latestCumObj = cumActual[cumActual.length - 1];
                r._calcRet = latestCumObj.cum_pct;
                r._calcDay = latestCumObj.day;
                r._isPartial = (latestCumObj.day < currentDays);
                r._curStatus = latestCumObj.status_so_far || r.break_stop;
                r._hasData = true;
                validSampleCount++;
                if (r._calcRet > 0) winCount++;
                totalRetSum += r._calcRet;
            }} else {{
                r._calcRet = null;
                r._calcDay = null;
                r._isPartial = false;
                r._curStatus = '尚未觸發';
                r._hasData = false;
            }}

            let cumActualForMax = subDays.filter(x => x.cum_pct !== null);
            if (cumActualForMax.length > 0) {{
                let highestVal = -999.0;
                let highestDay = 1;
                cumActualForMax.forEach(dItem => {{
                    if (dItem.cum_pct > highestVal) {{
                        highestVal = dItem.cum_pct;
                        highestDay = dItem.day;
                    }}
                }});
                r._maxRet = highestVal;
                r._maxDay = highestDay;
                if (r._maxRet >= 8.0) hit8Count++;
            }} else {{
                r._maxRet = null;
            }}
        }} else {{
            r._calcRet = null;
            r._calcDay = null;
            r._isPartial = false;
            r._curStatus = '尚未觸發';
            r._maxRet = null;
            r._hasData = false;
        }}
    }});

    if (sortField !== 'none' && sortDirection !== 'none') {{
        filteredList.sort((a, b) => {{
            const valA = sortField === 'max' ? a._maxRet : a._calcRet;
            const valB = sortField === 'max' ? b._maxRet : b._calcRet;
            if (valA === null) return 1;
            if (valB === null) return -1;
            return sortDirection === 'desc' ? (valB - valA) : (valA - valB);
        }});
    }}
    
    const hitRate = validSampleCount > 0 ? (hit8Count / validSampleCount * 100).toFixed(1) : '0.0';
    const avgRet = validSampleCount > 0 ? (totalRetSum / validSampleCount).toFixed(2) : '0.00';
    const sign = totalRetSum >= 0 ? '+' : '';
    const retColor = totalRetSum >= 0 ? 'var(--red-profit)' : 'var(--green-loss)';

    if (isGlobalSearch) {{
        document.getElementById('dayInlineStat').innerHTML = `
            <span>🔍 跨全歷史搜尋: 「<b>${{searchQuery}}</b>」 (共 <b>${{totalCount}}</b> 筆入選紀錄)</span>
            <span>已進場: <b>${{validSampleCount}}</b> 檔 | 總累積: <b style="color:${{retColor}};">${{validSampleCount > 0 ? (sign + totalRetSum.toFixed(1) + '%') : '--'}}</b> (均${{avgRet}}%)</span>
        `;
    }} else {{
        const dayTag = maxDaysAvailable > 0 ? ` (D${{maxDaysAvailable}})` : '';
        document.getElementById('dayInlineStat').innerHTML = `
            <span>📅 <b>${{selectedDate}}</b> (<b>${{validSampleCount}}</b>/${{totalCount}}檔已進場)${{dayTag}}</span>
            <span>達標: <b style="color:var(--accent-gold);">${{hitRate}}%</b> | 總累積: <b style="color:${{retColor}};">${{validSampleCount > 0 ? (sign + totalRetSum.toFixed(1) + '%') : '--'}}</b></span>
        `;
    }}

    if (totalCount === 0) {{
        document.getElementById('tableContainer').innerHTML = `<div class="table-wrapper"><table style="width:100%;"><tr><td style="color:var(--text-muted); padding:30px; text-align:center;">${{isGlobalSearch ? `全歷史未找到符合「${{searchQuery}}」的股票紀錄` : '此日期與過濾條件下無符合的入選股票'}}</td></tr></table></div>`;
        return;
    }}

    let retIcon = (sortField === 'ret') ? (sortDirection === 'desc' ? '⬇️' : '⬆️') : '⬍';
    let maxIcon = (sortField === 'max') ? (sortDirection === 'desc' ? '⬇️' : '⬆️') : '⬍';

    let html = `<div class="table-wrapper"><table><thead><tr>
        <th class="sticky-stock">標的 / 入選日</th>`;
    
    if (showStageCol) {{
        html += `<th>分級評分</th>`;
    }}
    
    html += `<th>買進點 (2張)</th>`;
    
    if (showStopsCol) {{
        html += `<th>停損 / 停利點</th>`;
    }}
    
    html += `<th class="sortable" onclick="toggleSortField('max')">最高衝刺 ${{maxIcon}}</th>
        <th class="sortable" onclick="toggleSortField('ret')">${{currentDays}}天累計 ${{retIcon}}</th>`;
        
    for (let d = 1; d <= currentDays; d++) {{
        html += `<th>第${{d}}天</th>`;
    }}
    html += `<th>狀態標記</th></tr></thead><tbody>`;

    filteredList.forEach(r => {{
        const subDays = r.daily_list.slice(0, currentDays);
        const curTotalRet = r._calcRet;
        const rule = r._rule || {{ sl: -5.0, tp1: 15.0, tp2: 30.0, sl2: 2.0 }};

        let retCellContent = '--';
        let totCls = 'c-gray';
        if (curTotalRet !== null) {{
            totCls = curTotalRet > 0 ? 'c-red' : (curTotalRet < 0 ? 'c-green' : 'c-gray');
            const signRet = curTotalRet > 0 ? '+' : '';
            const partialTag = r._isPartial ? ` <span style="font-size:9.5px; font-weight:normal; color:var(--text-muted);">(D${{r._calcDay}})</span>` : '';
            retCellContent = `${{signRet}}${{curTotalRet}}%${{partialTag}}`;
        }}

        let maxCellContent = '--';
        let maxCls = 'c-gray';
        if (r._maxRet !== null) {{
            const signMax = r._maxRet > 0 ? '+' : '';
            if (r._maxRet >= 15.0) {{
                maxCls = 'max-super';
            }} else if (r._maxRet >= 8.0) {{
                maxCls = 'max-good';
            }} else if (r._maxRet > 0) {{
                maxCls = 'c-red';
            }} else {{
                maxCls = 'c-green';
            }}
            maxCellContent = `${{signMax}}${{r._maxRet}}% <span style="font-size:9.5px; font-weight:normal; color:var(--text-muted);">(D${{r._maxDay}})</span>`;
        }}

        const gradeCls = r.grade === 'A' ? 'badge-grade-a' : (r.grade === 'B' ? 'badge-grade-b' : 'badge-grade-c');
        
        const dispStatus = r._curStatus || r.break_stop;
        let statusBadgeCls = 'st-wait';
        if (dispStatus.includes('全停利')) statusBadgeCls = 'st-winall';
        else if (dispStatus.includes('停利1張')) statusBadgeCls = 'st-tp';
        else if (dispStatus.includes('持有中')) statusBadgeCls = 'st-holding';
        else if (dispStatus.includes('破') || dispStatus.includes('出餘單')) statusBadgeCls = 'st-stop';

        let tagElements = [];
        if (r.trend_ideal) {{
            tagElements.push(`<span class="bonus-tag-trend-ideal" title="帶量突破下降趨勢線(${{r.tl_price || ''}})且站穩支撐">📈帶量破趨勢</span>`);
        }} else if (r.trend_break) {{
            tagElements.push(`<span class="bonus-tag-trend" title="突破下降趨勢線(${{r.tl_price || ''}})">📈破下降趨勢</span>`);
        }}
        if (r.bonus && r.bonus.length > 0) {{
            r.bonus.forEach(b => {{
                const isGold = b.includes('基本面') || b.includes('營收');
                const tagCls = isGold ? 'bonus-tag bonus-tag-gold' : 'bonus-tag';
                tagElements.push(`<span class="${{tagCls}}">${{b}}</span>`);
            }});
        }}
        let bonusHtml = tagElements.length > 0 ? `<div class="bonus-box">${{tagElements.join('')}}</div>` : '';

        let entryHtml = '';
        if (r.has_entered && r.entry_day_num <= currentDays) {{
            entryHtml = `
                <div class="entry-box">
                    <div style="display:flex; align-items:center; gap:3px;">
                        <span class="badge-trigger-ok">🎯D${{r.entry_day_num}}進場</span>
                        <b style="color:var(--text-main); font-size:13.5px;">${{r.entry_price}}</b>
                    </div>
                </div>
            `;
        }} else {{
            entryHtml = `
                <div class="entry-box">
                    <div style="display:flex; align-items:center; gap:2px;">
                        <span class="badge-trigger-no">⏳觀察</span>
                        <span style="color:var(--text-muted); font-size:12px;">${{r.target_pivot !== null ? r.target_pivot : '--'}}</span>
                    </div>
                    <div style="font-size:9.5px; color:var(--text-muted);">待突破</div>
                </div>
            `;
        }}

        const lockLabel = rule.sl2 > 0 ? `+${{rule.sl2}}%鎖利` : '保本';
        let slTpHtml = `
            <div style="font-size:9.5px; line-height:1.3; text-align:center;">
                <span style="color:var(--red-profit); font-weight:bold;">+${{rule.tp2}}%:${{r.tp_price_2 || '--'}}</span> | <span style="color:var(--accent-gold); font-weight:bold;">+${{rule.tp1}}%:${{r.tp_price_1 || '--'}}</span><br>
                <span style="color:#ea580c; font-weight:bold;">${{lockLabel}}:${{r.stop_loss_lock || '--'}}</span> | <span style="color:#dc2626; font-weight:bold;">${{rule.sl}}%:${{r.stop_loss_init || '--'}}</span>
            </div>
        `;

        const dateTagHtml = `<div class="date-badge-box" onclick="jumpToDate('${{r.date_str}}')" title="點擊跳轉至該交易日">📅 ${{r.date_str.substring(5)}}</div>`;
        
        html += `<tr>
            <td class="sticky-stock">
                <div class="stock-combo">
                    ${{dateTagHtml}}
                    <span class="stock-id-txt">${{r.stock_id}}</span>
                    <span class="stock-name-txt">${{r.name}}</span>
                    ${{bonusHtml}}
                </div>
            </td>`;
            
        if (showStageCol) {{
            html += `<td>
                <div class="stage-grade-combo">
                    <span class="badge-tag">${{r.stage}}</span>
                    <span class="badge-grade ${{gradeCls}}">${{r.grade}}</span>
                </div>
            </td>`;
        }}
        
        html += `<td>${{entryHtml}}</td>`;
        
        if (showStopsCol) {{
            html += `<td>${{slTpHtml}}</td>`;
        }}
        
        html += `<td class="max-highlight ${{maxCls}}">${{maxCellContent}}</td>
            <td class="ret-highlight ${{totCls}}">${{retCellContent}}</td>`;

        subDays.forEach(dInfo => {{
            if (dInfo.close === null) {{
                html += `<td><div class="cell-box"><span class="date-label">--</span><span class="c-gray">--</span></div></td>`;
            }} else if (dInfo.state === 'waiting') {{
                html += `<td>
                    <div class="cell-box waiting-box">
                        <span class="date-label">${{dInfo.date}}</span>
                        <span class="close-price" style="color:var(--text-muted);">${{dInfo.close}}</span>
                        <span style="font-size:9.5px; color:var(--text-muted);">⏳觀察</span>
                    </div>
                </td>`;
            }} else {{
                const isLimitUp = (dInfo.day_pct !== null && dInfo.day_pct >= 9.5);
                const dayCls = isLimitUp ? 'limit-up' : ((dInfo.day_pct > 0) ? 'c-red' : ((dInfo.day_pct < 0) ? 'c-green' : 'c-gray'));
                const daySign = (dInfo.day_pct > 0) ? '+' : '';
                const cumBg = (dInfo.cum_pct > 0) ? 'bg-red' : ((dInfo.cum_pct < 0) ? 'bg-green' : 'bg-gray');
                const cumSign = (dInfo.cum_pct > 0) ? '+' : '';

                let cellExtraTag = '';
                let boxClass = 'cell-box';

                if (dInfo.state === 'entry_first') {{
                    cellExtraTag = `<span class="badge-entry-day">🎯進場2張</span>`;
                }} else if (dInfo.state === 'tp_10') {{
                    cellExtraTag = `<span class="badge-tp-10">💰+${{rule.tp1}}%出1張</span>`;
                }} else if (dInfo.state === 'tp_20') {{
                    cellExtraTag = `<span class="badge-tp-20">🎉+${{rule.tp2}}%全出</span>`;
                }} else if (dInfo.state === 'stopout_15') {{
                    cellExtraTag = `<span class="badge-stop-15">🛑破${{rule.sl}}%全出</span>`;
                }} else if (dInfo.state === 'stopout_7') {{
                    const lockBadgeTxt = rule.sl2 > 0 ? `🔒鎖+${{rule.sl2}}%出餘單` : `🛑保本出餘單`;
                    cellExtraTag = `<span class="badge-stop-7">${{lockBadgeTxt}}</span>`;
                }} else if (dInfo.state === 'holding_1') {{
                    cellExtraTag = `<span class="badge-share-1">剩1張</span>`;
                }} else if (dInfo.state === 'all_exited') {{
                    boxClass = 'cell-box exited-box';
                }}

                html += `<td>
                    <div class="${{boxClass}}">
                        <span class="date-label">${{dInfo.date}}</span>
                        <span class="close-price">${{dInfo.close}}</span>
                        <span class="day-val ${{dayCls}}">${{daySign}}${{dInfo.day_pct}}%</span>
                        ${{cellExtraTag ? cellExtraTag : `<span class="cum-box ${{cumBg}}">${{cumSign}}${{dInfo.cum_pct}}%</span>`}}
                    </div>
                </td>`;
            }}
        }});

        html += `<td class="status-highlight ${{statusBadgeCls}}">${{dispStatus}}</td>
        </tr>`;
    }});

    html += `</tbody></table></div>`;
    document.getElementById('tableContainer').innerHTML = html;
}}

window.onload = function() {{
    initTheme();
    const savedScheme = localStorage.getItem('vcp_scheme_pref') || 'scheme_a';
    currentScheme = SCHEMES[savedScheme] ? savedScheme : 'scheme_a';
    currentDays = SCHEMES[currentScheme].defaultDays;

    document.querySelectorAll('[id^="btnScheme-"]').forEach(btn => btn.classList.remove('active'));
    const activeSchemeBtn = document.getElementById('btnScheme-' + currentScheme);
    if (activeSchemeBtn) activeSchemeBtn.classList.add('active');

    recalculateAllTrades();
    initDaysButtons();
    updateLegendDisplay();
    initDatePickers();
}};
</script>
</body>
</html>
"""

    html_path = "history_20d_color_report.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n✨ 已完整更新報表：{html_path}")
    print("👉 成功新增【📈 突破下降趨勢】選股過濾器與【📈帶量破趨勢】標籤！")

if __name__ == "__main__":
    analyze_and_build_dual_report()