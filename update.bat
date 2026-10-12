@echo off
echo === [1/4] Git Pull Upstream ===
git pull upstream main --no-edit
echo === [2/4] Backfill Prices ===
.\venv\Scripts\python.exe backfill_history.py
echo === [3/4] Analyze and Build Report ===
.\venv\Scripts\python.exe analyze_history_returns.py
copy /Y history_20d_color_report.html index.html
echo === [4/4] Push to GitHub ===
git add .
git commit -m "Auto update"
git push myorigin main
echo === Done! All updated successfully. ===
