import os
import subprocess
import glob
from pathlib import Path

print('=== 步驟 1: 強制從作者倉庫獲取最新檔案 ===')
subprocess.run(['git', 'fetch', 'upstream', 'main'], check=False)
subprocess.run(['git', 'checkout', 'upstream/main', '--', 'docs/history'], check=False)
subprocess.run(['git', 'checkout', 'upstream/main', '--', 'data/prices'], check=False)

print('=== 步驟 2: 重新計算策略績效 ===')
subprocess.run([r'.\venv\Scripts\python.exe', 'analyze_history_returns.py'], check=True)

print('=== 步驟 3: 覆蓋 index.html ===')
if os.path.exists('history_20d_color_report.html'):
    import shutil
    shutil.copyfile('history_20d_color_report.html', 'index.html')

print('=== 步驟 4: 推送至你的 GitHub 網站 ===')
subprocess.run(['git', 'add', '.'], check=False)
subprocess.run(['git', 'commit', '-m', 'Auto sync from upstream and publish'], check=False)
subprocess.run(['git', 'push', 'myorigin', 'main'], check=False)

print('=== 全部完成！ ===')
