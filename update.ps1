Set-Content -Path update.bat -Value @"
@echo off
chcp 65001 >nul
echo [1/4] 同步原作者更新...
git pull upstream main --no-edit

echo [2/4] 補齊最新日K行情...
.\venv\Scripts\python.exe backfill_history.py

echo [3/4] 重新計算策略績效...
.\venv\Scripts\python.exe analyze_history_returns.py
copy /Y history_20d_color_report.html index.html

echo [4/4] 發布到 GitHub...
git add .
git commit -m "Auto update"
git push myorigin main

echo [OK] 全部更新完成！
"@ -Encoding UTF8