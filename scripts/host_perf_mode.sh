#!/usr/bin/env bash
# host_perf_mode.sh on|off|status
# Homogeneous measurement configuration for the i7-12700H host (6P + 8E hybrid):
#   on  : SMT off (cpu1,3,..,11 offline), E-cores (cpu_atom set) offline,
#         turbo off (P-cores fixed at 2.3 GHz base), governor=performance,
#         thermald stopped (it would silently lower max_perf_pct when warm;
#         hardware TCC/PL1/PL2 protection stays active regardless)
#         -> nproc becomes 6, LTTng per-CPU ring buffers 6 x (2M x 4) = 48 MB
#   split : daytime layout (2026-09-05): SMT off, E-cores ONLINE, turbo off,
#         performance governor, thermald stopped. P-cores 0,2,4,6,8,10 +
#         E-cores 12-17 go to the experiment container
#         (docker --cpuset-cpus=0,2,4,6,8,10,12-17; inside: app+nsys on the
#         P set, LTTng daemons/FISH daemon/probes on 12-17 via
#         FISH_APP_CPUS/FISH_OBS_CPUS), E-cores 18-19 stay with the desktop
#         user (user-<uid>.slice AllowedCPUs=18-19, --runtime, reversible).
#         Paper runs stay on `on` (tty, nothing else running).
#   off : restore laptop defaults (E-cores online, SMT on, turbo on, powersave,
#         thermald started, user slice unconfined)
#   status : print config incl. max_perf_pct + throttle counters + TCPU temp;
#            run it before AND after every campaign run (append to run log) so a
#            frequency cap or throttling episode is visible afterwards.
# Everything is sysfs-only: a reboot also restores defaults.
set -euo pipefail
CPU=/sys/devices/system/cpu
ATOM=$(cat /sys/devices/cpu_atom/cpus 2>/dev/null || true)     # "12-19"

expand() { local out=() r a b; IFS=, read -ra parts <<<"$1"
  for r in "${parts[@]}"; do a=${r%-*}; b=${r#*-}; for ((i=a;i<=b;i++)); do out+=("$i"); done; done
  echo "${out[@]}"; }

need_root() { [[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"; }

set_gov() { for g in $CPU/cpu*/cpufreq/scaling_governor; do echo "$1" > "$g" 2>/dev/null || true; done; }

tcpu() { local z; for z in /sys/class/thermal/thermal_zone*; do
  [[ $(cat "$z/type" 2>/dev/null) == TCPU ]] && { awk '{printf "%.1f C", $1/1000}' "$z/temp"; return; }; done; echo "n/a"; }

status() {
  echo "time            : $(date -Is)"
  echo "online cpus     : $(cat $CPU/online)   (nproc=$(nproc))"
  echo "smt             : $(cat $CPU/smt/control)"
  echo "no_turbo        : $(cat $CPU/intel_pstate/no_turbo)"
  echo "max/min_perf_pct: $(cat $CPU/intel_pstate/max_perf_pct) / $(cat $CPU/intel_pstate/min_perf_pct)   (max<100 = something capped the frequency)"
  echo "governor/epp    : $(cat $CPU/cpu0/cpufreq/scaling_governor) / $(cat $CPU/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
  echo "cpu0 min/max/cur: $(cat $CPU/cpu0/cpufreq/scaling_min_freq) / $(cat $CPU/cpu0/cpufreq/scaling_max_freq) / $(cat $CPU/cpu0/cpufreq/scaling_cur_freq) kHz"
  echo "throttle pkg/c0 : $(cat $CPU/cpu0/thermal_throttle/package_throttle_count) / $(cat $CPU/cpu0/thermal_throttle/core_throttle_count)"
  echo "TCPU temp       : $(tcpu)"
  echo "thermald        : $(systemctl is-active thermald 2>/dev/null || true)   power-profile: $(powerprofilesctl get 2>/dev/null || echo n/a)"
  [[ -n $ATOM ]] && echo "E-cores ($ATOM) : $(for c in $(expand "$ATOM"); do cat $CPU/cpu$c/online; done | tr '\n' ' ')"
  echo "user slice cpus : $(systemctl show user-${SUDO_UID:-$(id -u)}.slice -p AllowedCPUs --value 2>/dev/null)   (empty = unconfined)"
}
USER_SLICE=user-${SUDO_UID:-$(id -u)}.slice
HOST_CPUS=18-19
P_CPUS=0,2,4,6,8,10          # primary threads of the 6 P-cores (SMT off)
OBS_CPUS=12-17               # E-cores for the observer inside the container

case "${1:-status}" in
  on)
    need_root "$@"
    systemctl stop thermald 2>/dev/null || true
    echo off > $CPU/smt/control
    for c in $(expand "$ATOM"); do echo 0 > $CPU/cpu$c/online; done
    echo 1 > $CPU/intel_pstate/no_turbo
    set_gov performance
    status ;;
  split)
    need_root "$@"
    systemctl stop thermald 2>/dev/null || true
    echo off > $CPU/smt/control
    for c in $(expand "$ATOM"); do echo 1 > $CPU/cpu$c/online; done
    echo 1 > $CPU/intel_pstate/no_turbo
    set_gov performance
    systemctl set-property --runtime "$USER_SLICE" AllowedCPUs=$HOST_CPUS
    status
    echo
    echo "experiment container : docker run --cpuset-cpus=$P_CPUS,$OBS_CPUS ..."
    echo "inside the container : FISH_APP_CPUS=$P_CPUS FISH_OBS_CPUS=$OBS_CPUS (run_overhead_modes.sh reads these)"
    echo "desktop user         : $USER_SLICE confined to cpus $HOST_CPUS (undo: $0 off, or systemctl set-property --runtime $USER_SLICE AllowedCPUs=)" ;;
  off)
    need_root "$@"
    for c in $(expand "$ATOM"); do echo 1 > $CPU/cpu$c/online; done
    echo on > $CPU/smt/control
    echo 0 > $CPU/intel_pstate/no_turbo
    set_gov powersave
    systemctl set-property --runtime "$USER_SLICE" AllowedCPUs= 2>/dev/null || true
    systemctl start thermald 2>/dev/null || true
    status ;;
  status) status ;;
  *) echo "usage: $0 on|split|off|status" >&2; exit 2 ;;
esac
