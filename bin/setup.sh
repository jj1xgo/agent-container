#!/bin/sh
set -eu

usage() {
    echo "usage: bin/setup.sh OWNER/REPOSITORY [PROJECT]" >&2
    exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage

repository=$1
case "$repository" in
    */*) ;;
    *) usage ;;
esac

owner=${repository%/*}
repository_name=${repository#*/}
case "$owner" in
    ""|.|..|*[!A-Za-z0-9._-]*) usage ;;
esac
case "$repository_name" in
    ""|.|..|*[!A-Za-z0-9._-]*|*/*) usage ;;
esac
[ "${#owner}" -le 100 ] && [ "${#repository_name}" -le 100 ] || usage

project=${2:-$repository_name}
case "$project" in
    ""|.|..|*[!A-Za-z0-9._-]*) usage ;;
esac
[ "${#project}" -le 100 ] || usage

for command in git gh podman python3; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "error: required command not found: $command" >&2
        exit 1
    fi
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
agentctl=$script_dir/agentctl
state_root=${AGENT_CONTAINER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/agent-container}
handover_root=${AGENT_HANDOVER_ROOT:-$state_root/handovers}
codex_auth_file=$state_root/shared-auth/codex/auth.json

case "$state_root:$handover_root" in
    /*:/*) ;;
    *)
        echo "error: state and handover roots must be absolute paths" >&2
        exit 1
        ;;
esac

umask 077
for directory in "$state_root" "$state_root/gh" "$handover_root" "$handover_root/$project"; do
    if [ -L "$directory" ]; then
        echo "error: setup directory must not be a symlink: $directory" >&2
        exit 1
    fi
    if [ -e "$directory" ] && [ ! -d "$directory" ]; then
        echo "error: setup path must be a directory: $directory" >&2
        exit 1
    fi
    mkdir -p -m 700 -- "$directory"
    chmod 700 -- "$directory"
done

echo "[1/5] Checking dedicated GitHub authentication"
if ! GH_CONFIG_DIR=$state_root/gh gh auth status >/dev/null 2>&1; then
    GH_CONFIG_DIR=$state_root/gh gh auth login --git-protocol https
fi

echo "[2/5] Checking runtime image"
if ! podman image exists localhost/agent-container:dev; then
    "$agentctl" build
fi

echo "[3/5] Checking Codex authentication"
if [ -L "$codex_auth_file" ] || [ ! -f "$codex_auth_file" ]; then
    "$agentctl" auth codex
fi

echo "[4/5] Registering project"
if [ "$project" = "$repository_name" ]; then
    "$agentctl" project add "$repository" --handover-root "$handover_root"
else
    "$agentctl" project add "$repository" --project "$project" --handover-root "$handover_root"
fi

echo "[5/5] Running diagnostics"
"$agentctl" doctor "$project"

echo
echo "Setup complete. Start Codex with:"
echo "  $agentctl run $project"
