#!/usr/bin/env bash
# Claude Code status line (2 lines)
# 1: model / effort / repo:branch [dirty] / PR
# 2: context bar / cost / duration / 5h & 7d rate limits
set -u
input=$(cat)

j() {
  printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
query = sys.argv[1]
alts = [a.strip() for a in query.split("//")]

def get_path(path):
    if not path.startswith("."):
        return None, False
    if path == ".":
        return data, True
    cur = data
    for p in path[1:].split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None, False
    return cur, True

result, found = None, False
for alt in alts:
    if alt.startswith("."):
        val, ok = get_path(alt)
        if ok and val is not None and val is not False:
            result, found = val, True
            break
    elif alt == "empty":
        found = True
        break
    elif len(alt) >= 2 and alt.startswith("\"") and alt.endswith("\""):
        result, found = alt[1:-1], True
        break
    elif alt in ("true", "false"):
        result, found = alt, True
        break
    else:
        result, found = alt, True
        break

if not found or result is None:
    sys.exit(0)
if isinstance(result, bool):
    print("true" if result else "false")
elif isinstance(result, float) and result.is_integer():
    print(int(result))
else:
    print(result)
' "$1"
}

# ---- colors ----
RST=$'\033[0m'; DIM=$'\033[2m'; BOLD=$'\033[1m'
CYAN=$'\033[36m'; MAG=$'\033[35m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RED=$'\033[31m'; BLU=$'\033[34m'

# ---- line 1 ----
model=$(j '.model.display_name // "?"')
effort=$(j '.effort.level // empty')
fast=$(j '.fast_mode // false')
cwd=$(j '.workspace.current_dir // .cwd // ""')
repo=$(j '.workspace.repo.name // empty')

line1="${BOLD}${MAG}${model}${RST}"
[ -n "$effort" ] && line1+=" ${DIM}${effort}${RST}"
[ "$fast" = "true" ] && line1+=" ${YEL}⚡${RST}"

if [ -n "$cwd" ] && git -C "$cwd" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null || git -C "$cwd" rev-parse --short HEAD 2>/dev/null)
  name=${repo:-$(basename "$(git -C "$cwd" rev-parse --show-toplevel)")}
  line1+=" ${CYAN}${name}${RST}:${GRN}${branch}${RST}"
  # dirty / staged counts
  porcelain=$(git -C "$cwd" status --porcelain 2>/dev/null)
  if [ -n "$porcelain" ]; then
    staged=$(grep -c '^[MADRC]' <<<"$porcelain")
    unstaged=$(grep -c '^.[MD]' <<<"$porcelain")
    untracked=$(grep -c '^??' <<<"$porcelain")
    st=""
    [ "$staged" -gt 0 ] && st+="${GRN}+${staged}${RST}"
    [ "$unstaged" -gt 0 ] && st+="${YEL}~${unstaged}${RST}"
    [ "$untracked" -gt 0 ] && st+="${RED}?${untracked}${RST}"
    line1+=" ${st}"
  fi
  # ahead / behind
  ab=$(git -C "$cwd" rev-list --left-right --count '@{upstream}...HEAD' 2>/dev/null)
  if [ -n "$ab" ]; then
    behind=${ab%%	*}; ahead=${ab##*	}
    [ "$ahead" -gt 0 ] && line1+=" ${DIM}↑${ahead}${RST}"
    [ "$behind" -gt 0 ] && line1+=" ${DIM}↓${behind}${RST}"
  fi
  wt=$(j '.workspace.git_worktree // empty')
  [ -n "$wt" ] && line1+=" ${DIM}[wt:${wt}]${RST}"
else
  line1+=" ${CYAN}$(basename "${cwd:-?}")${RST}"
fi

pr=$(j '.pr.number // empty')
if [ -n "$pr" ]; then
  state=$(j '.pr.review_state // empty')
  case "$state" in
    approved) pc=$GRN;; changes_requested) pc=$RED;; draft) pc=$DIM;; *) pc=$YEL;;
  esac
  line1+=" ${pc}PR#${pr}${state:+ ${state}}${RST}"
fi

# ---- line 2 ----
used=$(j '.context_window.used_percentage // 0')
used=${used%.*}
size=$(j '.context_window.context_window_size // 0')
width=10
filled=$(( used * width / 100 )); [ "$filled" -gt "$width" ] && filled=$width
bar=""
for ((i=0;i<width;i++)); do
  if [ "$i" -lt "$filled" ]; then bar+="█"; else bar+="░"; fi
done
if   [ "$used" -ge 80 ]; then cc=$RED
elif [ "$used" -ge 50 ]; then cc=$YEL
else cc=$GRN; fi
size_k=$(( size / 1000 ))
line2="${cc}${bar} ${used}%${RST}"
[ "$size_k" -gt 0 ] && line2+="${DIM}/${size_k}k${RST}"

cost=$(j '.cost.total_cost_usd // 0')
line2+="  ${DIM}\$$(printf '%.2f' "$cost")${RST}"

ms=$(j '.cost.total_duration_ms // 0')
s=$(( ms / 1000 )); h=$(( s / 3600 )); m=$(( (s % 3600) / 60 ))
if [ "$h" -gt 0 ]; then dur="${h}h${m}m"; else dur="${m}m"; fi
line2+=" ${DIM}${dur}${RST}"

rl() { # $1 = key, $2 = label
  local p r rem lbl col
  p=$(j ".rate_limits.$1.used_percentage // empty")
  [ -z "$p" ] && return
  p=${p%.*}
  if   [ "$p" -ge 80 ]; then col=$RED
  elif [ "$p" -ge 50 ]; then col=$YEL
  else col=$GRN; fi
  r=$(j ".rate_limits.$1.resets_at // empty")
  lbl=""
  if [ -n "$r" ]; then
    rem=$(( r - $(date +%s) )); [ "$rem" -lt 0 ] && rem=0
    if   [ "$rem" -ge 86400 ]; then lbl="$(( rem / 86400 ))d$(( (rem % 86400) / 3600 ))h"
    elif [ "$rem" -ge 3600 ];  then lbl="$(( rem / 3600 ))h$(( (rem % 3600) / 60 ))m"
    else lbl="$(( rem / 60 ))m"; fi
  fi
  printf '%s' "  ${DIM}$2${RST} ${col}${p}%${RST}${lbl:+${DIM}(${lbl})${RST}}"
}
line2+="$(rl five_hour 5h)$(rl seven_day 7d)"

printf '%s\n%s\n' "$line1" "$line2"
