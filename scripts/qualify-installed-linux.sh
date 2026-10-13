#!/usr/bin/env bash
# Read-only host evidence and actual installed WebView/IPC; no network repairs,
# camera requests, vehicle commands, elevation, or uploads.
set -eu
umask 077
if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
  printf '%s\n' 'Usage: qualify-installed-linux.sh /absolute/path/q22-flight-lab /new/absolute/report-directory [MissionPlanner-version] [QGroundControl-version]' >&2
  exit 2
fi
q22_app=$(readlink -f -- "$1")
q22_output=$2
case "$q22_output" in /*) ;; *) printf '%s\n' 'Use an absolute new report-directory path.' >&2; exit 2;; esac
test -x "$q22_app"
command -v timeout >/dev/null
if [ -e "$q22_output" ] || [ -e "$q22_output.tar.gz" ]; then
  printf '%s\n' 'Report path already exists; choose a new path to preserve prior evidence.' >&2
  exit 2
fi
mkdir -p -- "$q22_output/native-gui"
q22_output=$(readlink -f -- "$q22_output")
q22_nonce=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
printf 'UTC: %s\nKernel: ' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$q22_output/host.txt"
uname -srm >> "$q22_output/host.txt"
if [ -r /etc/os-release ]; then cat /etc/os-release >> "$q22_output/host.txt"; fi
printf 'Mission Planner (operator declared): %s\nQGroundControl (operator declared): %s\nRadio (operator declared): DigiQ SkyPulse\nCamera (operator declared): SIYI A8 Mini\n' "${3:-not supplied}" "${4:-not supplied}" > "$q22_output/bench-context.txt"
sha256sum -- "$q22_app" > "$q22_output/application.sha256"
if command -v ip >/dev/null 2>&1; then
  ip -json address show > "$q22_output/interface-addresses.json"
  ip -json route show table main > "$q22_output/main-routes.json"
fi
q22_status=0
timeout --signal=TERM --kill-after=5s 90s "$q22_app" --verify-gui --data-dir "$q22_output/native-gui" --report "$q22_output/native-gui/result.json" --nonce "$q22_nonce" > "$q22_output/native-stdout.txt" 2> "$q22_output/native-stderr.txt" || q22_status=$?
if [ "$q22_status" -eq 0 ]; then
  if ! test -f "$q22_output/native-gui/result.json" ||
      ! grep -Fq '"native_gui_rendering": "verified"' "$q22_output/native-gui/result.json" ||
      ! grep -Fq "\"nonce\": \"$q22_nonce\"" "$q22_output/native-gui/result.json" ||
      ! grep -Fq '"owned_engine_shutdown_completed": true' "$q22_output/native-gui/result.json"; then
    q22_status=1
  fi
fi
printf 'Native GUI process exit code: %s\n' "$q22_status" >> "$q22_output/host.txt"
cat > "$q22_output/READ-ME.txt" <<'NOTE'
This local package proves only what result.json reports for this exact installed
binary and host. It does not validate physical telemetry/video streams, GCS
coexistence, automatic repair, sleep/wake, or minimum OS compatibility.
Network addresses/interface names and local paths may be present. Review the
package before sharing. This command performs no upload or network repair.
Record actual GCS versions and separate physical bench results before qualification.
NOTE
tar -C "$q22_output" -czf "$q22_output.tar.gz" .
printf 'Local qualification package: %s.tar.gz\n' "$q22_output"
exit "$q22_status"
