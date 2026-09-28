#!/usr/bin/env bash
# End-to-end test of scan_moov.py + rebuild_mp4.py on synthetic FAT32 images
# (partition-only and whole disk with MBR). Needs Linux with mkfs.fat and sfdisk,
# Python 3 and ffprobe, about 4.5 GB of free disk space (sparse files) and a real
# GoPro GH*.MP4 of up to ~40 MB.
#
# usage: tests/run_test.sh <GoPro video.MP4>
set -euo pipefail

video=$(realpath "${1:?usage: tests/run_test.sh <GoPro video.MP4>}")
here=$(cd "$(dirname "$0")" && pwd)
scripts="$here/../scripts"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"

truncate -s 2200M part.img
mkfs.fat -F 32 -s 64 part.img >/dev/null
python3 "$here/make_test_image.py" part.img 0 "$video" bad_part.MP4

truncate -s 2201M disk.img
printf 'start=2048, type=c\n' | sfdisk -q disk.img
mkfs.fat -F 32 -s 64 --offset 2048 disk.img >/dev/null
python3 "$here/make_test_image.py" disk.img $((2048 * 512)) "$video" bad_disk.MP4

status=0
for t in part disk; do
  echo "== $t image"
  read -r offset size _ < <(python3 "$scripts/scan_moov.py" "$t.img" | head -1)
  python3 "$scripts/rebuild_mp4.py" "$t.img" "$offset" "$size" "bad_$t.MP4" "out_$t.MP4"
  if cmp -s "out_$t.MP4" "$video"; then
    echo "PASS: rebuilt file is byte-identical to the original"
  else
    echo "FAIL: rebuilt file differs from the original"
    status=1
  fi
done
exit $status
