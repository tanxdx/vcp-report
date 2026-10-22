import json
import glob
import math
from pathlib import Path
import pandas as pd

DOCS_HISTORY_DIR = Path("docs/history")
PRICES_DIR = Path("data/prices")
START_DATE = "2026-09-01"  # 可改為 "" 測試 6/12 起全歷史
MAX_DAYS = 30

# 網格搜尋參數範圍（可自由增減）
SL_INIT_LIST = [-5.0, -7.0, -8.5, -10.0, -12.0]      # 初始停損 (%)
TP1_LIST = [6.0, 8.0, 10.0, 12.0, 15.0]              # 第一段停利 1 張 (%)
TP2_LIST = [15.0, 20.0, 25.0, 30.0]                  # 第二段停利全出 (%)
SL2_LIST = [0.0, 2.0]                                # 停利 1 張後，剩餘 1 張的保本/鎖利線 (%)

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

def load_all_trades():
    """預先載入所有已觸發進場的交易序列，大幅加速網格運算"""
    json_files = sorted(glob.glob(str(DOCS_HISTORY_DIR / "*.json")))
    recent_files = [f for f in json_files if Path(f).stem >= START_DATE]
    trades_by_stage = {"LEADERS": [], "BREAKOUT": [], "READY": []}

    print(f"正在載入 {START_DATE or '最早'} 至今共 {len(recent_files)} 個交易日資料...")
    for fp in recent_files:
        try:
            data = json.load(open(fp, "r", encoding="utf-8"))
        except Exception:
            continue
        date_str = data.get("as_of", Path(fp).stem)

        for stage in ["LEADERS", "BREAKOUT", "READY"]:
            seen_sids = set()
            for item in data.get(stage, []):
                sid = str(item.get("stock_id") or item.get("symbol") or item.get("code") or "") if isinstance(item, dict) else str(item)
                if not sid or sid in seen_sids:
                    continue
                seen_sids.add(sid)

                pivot_val = item.get("pivot_high") or item.get("pivot") if isinstance(item, dict) else None
                pivot_price = round(float(pivot_val), 2) if pivot_val is not None else None

                df_p = get_stock_prices(sid)
                if df_p is None or df_p.empty:
                    continue
                m = df_p[df_p["date_str"] == date_str]
                if m.empty:
                    continue
                idx = m.index[0]
                target_pivot = pivot_price if pivot_price else df_p.loc[idx, "close_p"]
                if not target_pivot or target_pivot <= 0:
                    continue

                # 擷取未來 30 天 (close, high)
                bars = []
                for d in range(1, MAX_DAYS + 1):
                    if (idx + d) < len(df_p):
                        r = df_p.loc[idx + d]
                        bars.append((d, float(r["close_p"]), float(r["high_p"])))
                    else:
                        bars.append((d, None, None))

                # 檢查 30 天內是否有觸發進場
                if any(b[2] is not None and b[2] >= target_pivot for b in bars):
                    trades_by_stage[stage].append((target_pivot, bars))

    return trades_by_stage

def simulate_one_combo(trades, sl_init, tp1, tp2, sl2):
    """回傳該參數組合在 1~30 天各天期的報酬率清單"""
    returns_by_day = {d: [] for d in range(1, MAX_DAYS + 1)}
    sl_mult = 1.0 + (sl_init / 100.0)
    tp1_mult = 1.0 + (tp1 / 100.0)
    tp2_mult = 1.0 + (tp2 / 100.0)
    sl2_mult = 1.0 + (sl2 / 100.0)

    for entry_price, bars in trades:
        has_entered = False
        held_shares = 0
        realized_sum = 0.0
        cum_pct_history = {}
        last_valid_cum = None

        for d, c_val, h_val in bars:
            if c_val is not None:
                if not has_entered:
                    if h_val >= entry_price:
                        has_entered = True
                        held_shares = 2
                        cur_ret = ((c_val / entry_price) - 1.0) * 100.0
                        last_valid_cum = round(cur_ret, 2)
                else:
                    cur_ret = ((c_val / entry_price) - 1.0) * 100.0
                    if held_shares == 2:
                        if c_val < entry_price * sl_mult:
                            held_shares = 0
                            realized_sum = cur_ret * 2.0
                        elif h_val >= entry_price * tp1_mult:
                            held_shares = 1
                            realized_sum += tp1
                    elif held_shares == 1:
                        if c_val < entry_price * sl2_mult:
                            held_shares = 0
                            realized_sum += max(cur_ret, sl2)
                        elif h_val >= entry_price * tp2_mult:
                            held_shares = 0
                            realized_sum += tp2

                    if held_shares == 2:
                        last_valid_cum = round(cur_ret, 2)
                    elif held_shares == 1:
                        last_valid_cum = round((realized_sum + cur_ret) / 2.0, 2)
                    else:
                        last_valid_cum = round(realized_sum / 2.0, 2)

            if has_entered and last_valid_cum is not None:
                returns_by_day[d].append(last_valid_cum)

    return returns_by_day

def calc_metrics(rets, hold_days):
    n = len(rets)
    if n < 5:
        return None
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    wr = (len(wins) / n) * 100.0
    tot_ret = sum(rets)
    avg_ret = tot_ret / n

    if wins and losses:
        avg_w = sum(wins) / len(wins)
        avg_l = abs(sum(losses) / len(losses))
        plr = (avg_w / avg_l) if avg_l > 0 else 99.0
    elif wins:
        plr = 99.0
    else:
        plr = 0.0

    sharpe = -99.0
    if n > 1:
        var = sum((r - avg_ret) ** 2 for r in rets) / (n - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = ((avg_ret - 0.05) / std) * math.sqrt(250.0 / hold_days)

    return {
        "n": n,
        "tot_ret": round(tot_ret, 1),
        "avg_ret": round(avg_ret, 2),
        "wr": round(wr, 1),
        "plr": round(min(plr, 99.0), 2),
        "sharpe": round(sharpe, 2),
    }

def main():
    trades_by_stage = load_all_trades()
    stage_names = {"LEADERS": "👑 領頭 (LEADERS)", "BREAKOUT": "🚀 突破 (BREAKOUT)", "READY": "⏳ 蓄勢 (READY)"}

    for stage, trades in trades_by_stage.items():
        print("\n" + "=" * 78)
        print(f" {stage_names[stage]} | 總進場樣本數: {len(trades)} 檔")
        print("=" * 78)

        all_results = []
        for sl in SL_INIT_LIST:
            for tp1 in TP1_LIST:
                for tp2 in TP2_LIST:
                    if tp2 <= tp1:
                        continue
                    for sl2 in SL2_LIST:
                        rets_by_d = simulate_one_combo(trades, sl, tp1, tp2, sl2)
                        for d in range(1, MAX_DAYS + 1):
                            m = calc_metrics(rets_by_d[d], d)
                            if m:
                                m.update({"d": d, "sl": sl, "tp1": tp1, "tp2": tp2, "sl2": sl2})
                                all_results.append(m)

        if not all_results:
            print("樣本不足，無法計算。")
            continue

        # 計算綜合評分 (標準化權重：夏普 35% + 總獲利 30% + 勝率 20% + 風暴比 15%)
        df_res = pd.DataFrame(all_results)
        for col in ["tot_ret", "wr", "plr", "sharpe"]:
            std = df_res[col].std()
            df_res[f"z_{col}"] = (df_res[col] - df_res[col].mean()) / (std if std > 0 else 1.0)
        df_res["score"] = (
            df_res["z_sharpe"] * 0.35 +
            df_res["z_tot_ret"] * 0.30 +
            df_res["z_wr"] * 0.20 +
            df_res["z_plr"] * 0.15
        )

        # 1. 顯示現行參數 (-8.5%, +10%, +20%, 保本0%) 在 1~30 天的最佳天數 Top 5
        curr_df = df_res[(df_res["sl"] == -8.5) & (df_res["tp1"] == 10.0) & (df_res["tp2"] == 20.0) & (df_res["sl2"] == 0.0)]
        if not curr_df.empty:
            print("\n📌 【現行參數 (-8.5%停損 / +10%出1張 / +20%全出 / 0%保本)】最佳持有天數 Top 5：")
            top_curr = curr_df.sort_values("score", ascending=False).head(5)
            for _, r in top_curr.iterrows():
                print(f"  👉 第 {int(r['d']):2d} 天 (D{int(r['d'])}) | 總累積: {r['tot_ret']:+7.1f}% (均 {r['avg_ret']:+5.2f}%) | 勝率: {r['wr']:4.1f}% | 風暴比: {r['plr']:4.2f} | 夏普: {r['sharpe']:5.2f}")

        # 2. 顯示全參數網格搜尋【綜合最強 Top 5】
        print("\n🏆 【全參數尋優：獲利 + 勝率 + 夏普 + 風暴比 綜合最強 Top 5】：")
        top_all = df_res.sort_values("score", ascending=False).head(5)
        for idx, (_, r) in enumerate(top_all.iterrows(), 1):
            print(f"  第{idx}名: 持有 {int(r['d']):2d}天 | 停損:{r['sl']:5.1f}% | 停利1:+{r['tp1']:4.1f}% | 停利2:+{r['tp2']:4.1f}% | 餘單保本:+{r['sl2']:3.1f}%")
            print(f"         📈 總累積: {r['tot_ret']:+7.1f}% (均 {r['avg_ret']:+5.2f}%) | 勝率: {r['wr']:4.1f}% | 風暴比: {r['plr']:4.2f} | 夏普: {r['sharpe']:5.2f}")

        # 3. 單項冠軍指標
        best_ret = df_res.sort_values("tot_ret", ascending=False).iloc[0]
        best_sr = df_res.sort_values("sharpe", ascending=False).iloc[0]
        print("\n💡 【單項極致冠軍】：")
        print(f"  💰 總獲利最高: 持有 {int(best_ret['d']):2d}天 (停損{best_ret['sl']}% / 停利+{best_ret['tp1']}%、+{best_ret['tp2']}% / 保本+{best_ret['sl2']}%) -> 累積 {best_ret['tot_ret']:+.1f}% | 勝率 {best_ret['wr']}% | 夏普 {best_ret['sharpe']}")
        print(f"  ⚡ 夏普率最高: 持有 {int(best_sr['d']):2d}天 (停損{best_sr['sl']}% / 停利+{best_sr['tp1']}%、+{best_sr['tp2']}% / 保本+{best_sr['sl2']}%) -> 夏普 {best_sr['sharpe']} | 勝率 {best_sr['wr']}% | 風暴比 {best_sr['plr']} | 累積 {best_sr['tot_ret']:+.1f}%")

if __name__ == "__main__":
    main()