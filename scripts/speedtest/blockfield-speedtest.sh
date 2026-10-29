#!/bin/sh
# Blockfield download speed test for Linux and macOS: same passes and output as blockfield-speedtest.cmd.
set -u
slice=0-33554431
gcore=https://cdn-test.blockfield.pro # Gcore test resource; empty skips it
home_url=https://blockfield.pro/files/mods/blockfield-music-6.jar
r2_url=https://assets.blockfield.pro/music/music-v6/blockfield-music-6.jar
github_url=https://github.com/Blockfield/blockfield-releases/releases/download/v2.71.0/Blockfield-client.zip
out="$(cd "$(dirname "$0")" && pwd)/blockfield-speedtest.txt"

command -v curl >/dev/null || {
    echo "curl is required."
    exit 1
}

# pick PROMPT VALUE...: prints the VALUE whose 1-based number the user types.
pick() {
    prompt=$1
    shift
    while :; do
        printf '%s ' "$prompt" >&2
        read -r answer || exit 1
        i=1
        for value; do
            [ "$answer" = "$i" ] && echo "$value" && return
            i=$((i + 1))
        done
    done
}

measure() {
    echo "Testing $1, pass $2 ..."
    curl -s -L -r "$slice" -o /dev/null --connect-timeout 15 --max-time 45 \
        -w "BF1;$1;$2;%{http_code};%{size_download};%{speed_download};%{time_connect};%{time_starttransfer};%{time_total};%{remote_ip};" \
        "$3" >>"$out"
    echo "exit=$?" >>"$out"
}

echo "Blockfield download speed test."
echo "Turn off VPN and stop other downloads first."
echo "It downloads up to 270 MB and takes up to 6 minutes."
country=$(pick "Country: 1 Russia, 2 Ukraine, 3 Belarus, 4 Kazakhstan, 5 other:" RU UA BY KZ other) || exit 1
conn=$(pick "Connection: 1 home internet, 2 mobile:" home mobile) || exit 1
vpn=$(pick "VPN or proxy on right now? 1 no, 2 yes:" no yes) || exit 1
printf 'Internet provider, for example Rostelecom or Kyivstar: '
read -r isp
printf 'BF1;meta;country=%s;conn=%s;vpn=%s;isp=%s;time=%s;os=%s\n' \
    "$country" "$conn" "$vpn" "${isp:-unknown}" "$(date -u +%Y-%m-%dT%H:%MZ)" "$(uname -s)" >"$out"

# The second pass runs in reverse order and finds the CDN caches warm.
measure home 1 "$home_url"
measure r2 1 "$r2_url"
measure github 1 "$github_url"
if [ -n "$gcore" ]; then
    measure gcore 1 "$gcore/files/mods/blockfield-music-6.jar"
    measure gcore 2 "$gcore/files/mods/blockfield-music-6.jar"
fi
measure github 2 "$github_url"
measure r2 2 "$r2_url"
measure home 2 "$home_url"

echo "Done. The result is saved in $out: paste its contents into Discord."
