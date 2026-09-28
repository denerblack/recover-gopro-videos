#!/usr/bin/env python3
"""Scatter a real MP4 inside an already formatted FAT32 image, the way a GoPro
leaves it on the card: the header in one place, the data interleaved with junk
(standing in for the .LRV) and a jump halfway through. Also writes the "TestDisk
copy": the first cluster right, followed by junk.

usage: make_test_image.py <image> <partition offset in bytes> <video.MP4> <testdisk copy out>
"""
import os
import struct
import sys

if len(sys.argv) != 5:
    sys.exit(__doc__.strip())

img, part, video, bad_copy = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
mp4 = open(video, 'rb').read()

with open(img, 'r+b') as f:
    f.seek(part)
    bs = f.read(512)
    bps = struct.unpack('<H', bs[11:13])[0]
    cs = bps * bs[13]
    reserved = struct.unpack('<H', bs[14:16])[0]
    fatsz = struct.unpack('<I', bs[36:40])[0]
    data = part + (reserved + bs[16] * fatsz) * bps
    ncl = (os.path.getsize(img) - data) // cs + 2

    def cstart(c):
        return data + (c - 2) * cs

    chunks = [mp4[i:i + cs] for i in range(0, len(mp4), cs)]
    places = [1000]              # the header sits far from the rest
    c, run, half = 20000, 0, len(chunks) // 2
    for k in range(1, len(chunks)):
        if k == half:
            c = 60000            # jump to another region
        if run == 160:           # every 160 MP4 clusters, 16 of "LRV"
            for _ in range(16):
                f.seek(cstart(c))
                f.write(os.urandom(cs))
                c += 1
            run = 0
        places.append(c)
        c += 1
        run += 1
    if c >= ncl:
        sys.exit("image too small for this video")
    for k, cl in enumerate(places):
        f.seek(cstart(cl))
        f.write(chunks[k])

with open(bad_copy, 'wb') as o:
    o.write(chunks[0])
    o.write(os.urandom(len(mp4) - len(chunks[0])))
print(f"cluster={cs} bytes, {len(chunks)} clusters of video")
