#!/usr/bin/env bash
# Prints the current date/time in the selected zone, UTC and US East.
set -euo pipefail
LOCAL_TZ="${LOCAL_TZ:-UTC}"
now_utc=$(date -u +%s)
fmt='%a %d %b %Y %H:%M %Z'
echo "now: $(TZ=$LOCAL_TZ date -d @"$now_utc" +"$fmt") (local, $LOCAL_TZ)"
echo "     $(TZ=UTC date -d @"$now_utc" +"$fmt")"
echo "     $(TZ=America/New_York date -d @"$now_utc" +"$fmt") (US East / Toronto)"
h=$(TZ=$LOCAL_TZ date -d @"$now_utc" +%H)
if   [ "$h" -lt 5 ];  then part="late night"
elif [ "$h" -lt 12 ]; then part="morning"
elif [ "$h" -lt 17 ]; then part="afternoon"
elif [ "$h" -lt 21 ]; then part="evening"
else part="night"; fi
echo "     part of day: $part"
