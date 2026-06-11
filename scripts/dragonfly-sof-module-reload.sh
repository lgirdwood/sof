#!/usr/bin/env bash
set -euo pipefail

# Reload SOF kernel modules on Dragon Fly DUT.
# Run this script on the DUT as root.

DRIVER_MODULE="${SOF_DRIVER_MODULE:-snd_sof_pci_intel_mtl}"
ENABLE_PROBES=0
DRY_RUN=0
FAILED_UNLOADS=()

usage() {
    cat <<'EOF'
Usage: dragonfly-sof-module-reload.sh [options]

Reload SOF modules on the DUT.

Options:
  --driver <module>   Driver module to load (default: snd_sof_pci_intel_mtl)
  --enable-probes     Reload snd_sof_probes with enable=1 after driver reload
  -n, --dry-run       Print commands without executing
  -h, --help          Show this help

Environment:
  SOF_DRIVER_MODULE   Same as --driver
EOF
}

log() {
    printf '[sof-reload] %s\n' "$*"
}

run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '+ %s\n' "$*"
        return 0
    fi
    "$@"
}

run_status() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '+ %s\n' "$*"
        return 0
    fi

    set +e
    "$@"
    local status=$?
    set -e

    return "$status"
}

is_loaded() {
    [[ -d "/sys/module/$1" ]]
}

unload_if_loaded() {
    local mod="$1"
    if is_loaded "$mod"; then
        log "unloading $mod"
        # modprobe -r handles dependencies and aliases better than rmmod.
        if ! run_status modprobe -r "$mod"; then
            log "could not unload $mod (still in use)"
            FAILED_UNLOADS+=("$mod")
            return 1
        fi
    else
        log "skip $mod (not loaded)"
    fi

    return 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --driver)
            shift
            [[ $# -gt 0 ]] || { log "missing value for --driver"; exit 2; }
            DRIVER_MODULE="$1"
            ;;
        --enable-probes)
            ENABLE_PROBES=1
            ;;
        -n|--dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log "unknown option: $1"
            usage
            exit 2
            ;;
    esac
    shift
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    log "must run as root"
    exit 1
fi

# Unload optional/helper modules first, then remove the root PCI driver and let
# modprobe resolve the dependency tree from there.
UNLOAD_ORDER=(
    snd_sof_fw_gdb
    snd_sof_probes
    snd_sof_serial
    snd_sof_pci_intel_mtl
)

log "starting SOF unload"
for mod in "${UNLOAD_ORDER[@]}"; do
    unload_if_loaded "$mod" || true
done

if is_loaded "$DRIVER_MODULE"; then
    log "reload blocked, ${DRIVER_MODULE} is still loaded"
    if [[ "${#FAILED_UNLOADS[@]}" -gt 0 ]]; then
        log "modules still in use: ${FAILED_UNLOADS[*]}"
    fi
    log "manual recovery may require unloading the remaining dependent audio modules or rebooting the DUT"
    exit 1
fi

log "loading ${DRIVER_MODULE}"
run modprobe "$DRIVER_MODULE"

if [[ "$ENABLE_PROBES" -eq 1 ]]; then
    if is_loaded snd_sof_probes; then
        log "snd_sof_probes already loaded, reloading with enable=1"
        run modprobe -r snd_sof_probes
    fi
    log "loading snd_sof_probes enable=1"
    run modprobe snd_sof_probes enable=1
fi

log "reload complete"
