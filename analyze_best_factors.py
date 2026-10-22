import json
import glob
from pathlib import Path
import pandas as pd

DOCS_HISTORY_DIR = Path("docs/history")
PRICES_DIR = Path("data/prices")
START_DATE = "2026-09-01"
HOLD_DAYS = 7          # 以 7 天全能王為基準測試
SL_PCT = -5.0          # 停損 -5%
TP1_PCT = 15.0         # 停利1 +15%
TP2_PCT = 30.0         # 停利2 +30%
SL2_PCT = 2.0          # 鎖利 +2%

price_cache = {}

def get_stock_prices(stock_id: str):
    if stock_id in price_cache:
        return price_cache[stock_id]
    for ext in ["", ".TW", ".TWO"]:
        p = PRICES_DIR / f"{stock_id}{ext}.parquet"
        if p.exists():
            try:
                df = pd.read_parquet(p)
                d_col = next((c for c in df.columns if str(c).lower() in ["date", "datetime", "time"]), None)
                c_col = next((c for c in df.columns if str(c).lower() in ["close", "price"]), None)
                h_col = next((c for c in df.columns if str(c).lower() in ["high", "max_price"]), None)
                if d_col and c_col:
                    df["date_str"] = pd.to_datetime(df[d_col]).dt.strftime("%Y-%m-%d")
                    df["close_p"] = df[c_col].astype(float)
                    df["high_p"] = df[h_col].astype(float) if h_col else df["close_p"]
                    res = df.sort_values("date_str").reset_index(drop=True)
                    price_cache[stock_id] = res
                    return res
            except Exception:
                pass
    price_cache[stock_id] = None
    return None

def simulate_trade(entry_price, bars):
    has_entered = False
    entry_day = None
    held_shares = 0
    realized_sum = 0.0
    last_cum = None
    max_cum = -999.0

    for d, c_val, h_val in bars:
        if c_val is None:
            continue
        if not has_entered:
            if h_val >= entry_price:
                has_entered = True
                entry_day = d
                held_shares = 2
                cur_ret = ((c_val / entry_price) - 1.0) * 100.0
                last_cum = cur_ret
                if last_cum > max_cum:
                    max_cum = last_cum
        else:
            cur_ret = ((c_val / entry_price) - 1.0) * 100.0
            if held_shares == 2:
                if c_val < entry_price * (1.0 + SL_PCT / 100.0):
                    held_shares = 0
                    realized_sum = cur_ret * 2.0
                elif h_val >= entry_price * (1.0 + TP1_PCT / 100.0):
                    held_shares = 1
                    realized_sum += TP1_PCT
            elif held_shares == 1:
                if c_val < entry_price * (1.0 + SL2_PCT / 100.0):
                    held_shares = 0
                    realized_sum += max(cur_ret, SL2_PCT)
                elif h_val >= entry_price * (1.0 + TP2_PCT / 100.0):
                    held_shares = 0
                    realized_sum += TP2_PCT

            if held_shares == 2:
                last_cum = cur_ret
            elif held_shares == 1:
                last_cum = (realized_sum + cur_ret) / 2.0
            else:
                last_cum = realized_sum / 2.0

            if last_cum > max_cum:
                max_cum = last_cum

    return has_entered, entry_day, last_cum, max_cum

def main():
    json_files = sorted(glob.glob(str(DOCS_HISTORY_DIR / "*.json")))
    recent_files = [f for f in json_files if Path(f).stem >= START_DATE]
    records = []

    for fp in recent_files:
        try:
            data = json.load(open(fp, "r", encoding="utf-8"))
        except Exception:
            continue
        date_str = data.get("as_of", Path(fp).stem)

        for stage in ["LEADERS", "BREAKOUT", "READY"]:
            seen = set()
            for item in data.get(stage, []):
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("stock_id") or item.get("symbol") or item.get("code") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)

                df_p = get_stock_prices(sid)
                if df_p is None or df_p.empty:
                    continue
                m = df_p[df_p["date_str"] == date_str]
                if m.empty:
                    continue
                idx = m.index[0]

                pv = item.get("pivot_high") or item.get("pivot")
                target_pivot = round(float(pv), 2) if pv is not None else df_p.loc[idx, "close_p"]
                if not target_pivot or target_pivot <= 0:
                    continue

                bars = []
                for d in range(1, HOLD_DAYS + 1):
                    if (idx + d) < len(df_p):
                        r = df_p.loc[idx + d]
                        bars.append((d, float(r["close_p"]), float(r["high_p"])))
                    else:
                        bars.append((d, None, None))

                entered, e_day, final_ret, max_ret = simulate_trade(target_pivot, bars)
                if not entered or final_ret is None:
                    continue

                grade = str(item.get("grade") or item.get("score_grade") or "").strip().upper()
                if not grade:
                    rs = float(item.get("rs_rating") or 0)
                    grade = "A" if rs >= 90 else ("B" if rs >= 80 else "C")

                trust_buy = int(item.get("trust_streak") or 0) >= 1
                inst_buy = bool(item.get("inst_net_buy")) or trust_buy
                vol_contract = bool(item.get("vol_contraction"))
                near_52w = (float(item.get("dist_52w_high") or 1.0) <= 0.05)
                rs_high = bool(item.get("rs_line_new_high"))
                fund_ok = bool(item.get("fundamental_ok")) or (float(item.get("rev_3m_yoy") or item.get("eps_yoy") or 0) > 0.15)

                records.append({
                    "stage": stage,
                    "grade": grade,
                    "entry_day": e_day,
                    "trust_buy": trust_buy,
                    "inst_buy": inst_buy,
                    "vol_contract": vol_contract,
                    "near_52w": near_52w,
                    "rs_high": rs_high,
                    "fund_ok": fund_ok,
                    "ret": final_ret,
                    "max_ret": max_ret
                })

    df = pd.DataFrame(records)
    if df.empty:
        print("無足夠交易數據。")
        return

    print("\n" + "=" * 80)
    print(f" 📊 實戰選股因子勝率剖析 (持有 {HOLD_DAYS} 天 | 停損{SL_PCT}% / 停利+{TP1_PCT}%、+{TP2_PCT}%)")
    print("=" * 80)

    conditions = [
        ("【全樣本基準】所有進場標的", df),
        ("👑 【策略】領頭 (LEADERS)", df[df["stage"] == "LEADERS"]),
        ("🚀 【策略】突破 (BREAKOUT)", df[df["stage"] == "BREAKOUT"]),
        ("⏳ 【策略】蓄勢 (READY)", df[df["stage"] == "READY"]),
        ("🏆 【評級】A 級標的", df[df["grade"] == "A"]),
        ("⭐ 【評級】B 級標的", df[df["grade"] == "B"]),
        ("🔹 【評級】C 級標的", df[df["grade"] == "C"]),
        ("🎯 【進場時機】D1 首日立刻突破進場", df[df["entry_day"] == 1]),
        ("🐢 【進場時機】D2 之後才突破進場", df[df["entry_day"] > 1]),
        ("🔥 【籌碼】投信連買 (trust_streak >= 1)", df[df["trust_buy"]]),
        ("🏦 【籌碼】三大法人買超", df[df["inst_buy"]]),
        ("📐 【技術】量縮緊湊 (vol_contraction)", df[df["vol_contract"]]),
        ("🏔️ 【技術】近 52 週高點 (<=5%)", df[df["near_52w"]]),
        ("📈 【技術】RS 創高 (rs_line_new_high)", df[df["rs_high"]]),
        ("💰 【基本面】基本面佳 / 營收亮眼", df[df["fund_ok"]]),
        ("💎 【王牌組合 1】領頭 + A級", df[(df["stage"] == "LEADERS") & (df["grade"] == "A")]),
        ("💎 【王牌組合 2】領頭 + 投信連買", df[(df["stage"] == "LEADERS") & (df["trust_buy"])]),
        ("💎 【王牌組合 3】領頭 + 量縮緊湊 + 近52w高", df[(df["stage"] == "LEADERS") & (df["vol_contract"]) & (df["near_52w"])]),
        ("💎 【終極滿貫】領頭 + A級 + 法人買超 + 近52w高", df[(df["stage"] == "LEADERS") & (df["grade"] == "A") & (df["inst_buy"]) & (df["near_52w"])]),
    ]

    for title, sub in conditions:
        n = len(sub)
        if n == 0:
            continue
        wins = sub[sub["ret"] > 0]
        losses = sub[sub["ret"] <= 0]
        wr = (len(wins) / n) * 100.0
        avg_r = sub["ret"].mean()
        tot_r = sub["ret"].sum()
        hit8 = (len(sub[sub["max_ret"] >= 8.0]) / n) * 100.0
        avg_w = wins["ret"].mean() if len(wins) > 0 else 0
        avg_l = abs(losses["ret"].mean()) if len(losses) > 0 else 1
        plr = (avg_w / avg_l) if avg_l > 0 else 99.0

        print(f"{title:<36} | 樣本:{n:4d}檔 | 勝率:{wr:5.1f}% | 均報:{avg_r:+6.2f}% | 總累積:{tot_r:+7.1f}% | 衝8%率:{hit8:4.1f}% | 風暴比:{plr:4.2f}")

if __name__ == "__main__":
    main()