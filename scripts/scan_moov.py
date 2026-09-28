#!/usr/bin/env python3
"""List every MP4 index (moov atom) found in a raw card image.

Each output line is: <moov offset> <moov size> <creation time, UTC> <duration in seconds>

Read only: the image is never modified.

usage: scan_moov.py <image>
"""
import datetime
import mmap
import struct
import sys

if len(sys.argv) != 2:
    sys.exit(__doc__.strip())

EPOCH_1904 = datetime.datetime(1904, 1, 1)

with open(sys.argv[1], 'rb') as fh:
    mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    pos = 0
    while True:
        i = mm.find(b'mvhd', pos)
        if i < 0:
            break
        pos = i + 4
        # expected layout: [moov size]['moov'][mvhd size]['mvhd']
        if mm[i - 8:i - 4] != b'moov':
            continue
        moov_start = i - 12
        moov_size = struct.unpack('>I', mm[moov_start:moov_start + 4])[0]
        version = mm[i + 4]
        if version == 0:
            ctime, _mtime, timescale, duration = struct.unpack('>IIII', mm[i + 8:i + 24])
        else:
            ctime, _mtime, timescale, duration = struct.unpack('>QQIQ', mm[i + 8:i + 36])
        if timescale == 0 or moov_size < 100 or moov_size > 50_000_000:
            continue
        created = EPOCH_1904 + datetime.timedelta(seconds=ctime)
        print(f"{moov_start} {moov_size} {created:%Y-%m-%dT%H:%M:%S} {duration / timescale:.3f}")
