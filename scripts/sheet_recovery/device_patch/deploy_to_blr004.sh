#!/bin/bash
# Installs the verified sheet_update.py fix on EyecompassBLR-004 (DC 004 / CGI3).
# - backs up the live file as sheet_update.py.bak (refuses if one already exists)
# - only proceeds if the live file is still the exact version the fix was built on
# - swaps the new file in with an atomic rename; does NOT restart the app
# Takes effect at the next app restart or device reboot.
set -eu
HOST=nvidia@100.64.145.101
DIR="$(cd "$(dirname "$0")" && pwd)"
NEW="$DIR/sheet_update.py.new"
ORIG_SUM=f274464a022e3bd07ad936465aa3f020f4baefae5777567511e4a55b0bf9814e
NEW_SUM=$(sha256sum "$NEW" | cut -c1-64)
SSH="ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=5 $HOST"

echo "uploading patched file (sha256 $NEW_SUM) ..."
$SSH 'cat > ~/eye_compass/.sheet_update.py.tmp_new' < "$NEW"

$SSH "set -e; cd ~/eye_compass
CUR=\$(sha256sum sheet_update.py | cut -c1-64); TMP=\$(sha256sum .sheet_update.py.tmp_new | cut -c1-64)
[ \"\$CUR\" = $ORIG_SUM ] || { echo 'ABORT: live file changed since it was checked'; rm -f .sheet_update.py.tmp_new; exit 1; }
[ \"\$TMP\" = $NEW_SUM ]  || { echo 'ABORT: upload corrupted'; rm -f .sheet_update.py.tmp_new; exit 1; }
[ ! -e sheet_update.py.bak ] || { echo 'ABORT: sheet_update.py.bak already exists'; rm -f .sheet_update.py.tmp_new; exit 1; }
PID=\$(pgrep -f '^/home/nvidia/.virtualenvs/m38/bin/python /home/nvidia/eye_compass/main.py' || true)
cp -p sheet_update.py sheet_update.py.bak
chmod --reference=sheet_update.py .sheet_update.py.tmp_new
mv -f .sheet_update.py.tmp_new sheet_update.py
echo \"backup   : sheet_update.py.bak  \$(sha256sum sheet_update.py.bak | cut -c1-64)\"
echo \"installed: sheet_update.py      \$(sha256sum sheet_update.py | cut -c1-64)\"
~/.virtualenvs/m38/bin/python -B -c \"import ast; ast.parse(open('sheet_update.py', encoding='utf-8').read()); print('syntax   : OK')\"
~/.virtualenvs/m38/bin/python -B -c \"import sheet_update as s; assert callable(s.write_row_at_end); print('import   : OK, write_row_at_end present')\" 2>/dev/null
echo \"app PID  : \$PID before, \$(pgrep -f '^/home/nvidia/.virtualenvs/m38/bin/python /home/nvidia/eye_compass/main.py' || true) after (same = not restarted)\"
"
echo "done. To undo on the device:  cd ~/eye_compass && cp -p sheet_update.py.bak sheet_update.py"
