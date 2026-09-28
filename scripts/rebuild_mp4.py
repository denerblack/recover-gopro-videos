#!/usr/bin/env python3
"""Rebuild a deleted, fragmented GoPro MP4 from a raw FAT32 card image.

The video's own index (the moov atom at the end of the file) says where every
frame sits inside the file and how big it is. A GoPro records the MP4 and its
low-res .LRV at the same time, so on the card the two are interleaved, and the
start of the file may sit somewhere else entirely. Tools that read a deleted
file as one contiguous run (TestDisk, PhotoRec) glue the wrong blocks together.

This script walks the file cluster by cluster and checks every H.264 frame: the
NAL unit lengths inside a frame must add up exactly to the frame size given by
the index. When a frame does not add up, it searches the image for the cluster
where it does and continues from there. It does one pass forward (from the
header) and one backward (from the end of the moov) and joins them; a gap left
in the middle is filled with zeros and the rest of the video still plays.

It relies on the frames, not on the FAT: on the card this was written for, the
FAT marked some of the video's clusters as belonging to another file.

Made for FAT32 + GoPro H.264 (GH*.MP4). Other cameras or file systems need changes.
Read only: the image is never modified.

usage: rebuild_mp4.py <image> <moov offset> <moov size> <testdisk copy> <output.MP4>

  image          raw image of the whole card (with MBR) or of the partition only
  moov offset    first column of scan_moov.py for this video
  moov size      second column of scan_moov.py
  testdisk copy  the broken file TestDisk recovered: its first cluster is the real
                 header and marks where the video starts
  output.MP4     where to write the rebuilt video
"""
import bisect
import json
import mmap
import os
import struct
import subprocess
import sys
import tempfile

if len(sys.argv) != 6:
    sys.exit(__doc__.strip())

img, moov_start, moov_size, head_file, out = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])

fh = open(img, 'rb')
mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
# whole-disk image (MBR first) or partition-only image (TestDisk "Image Creation")
part = 0 if mm[82:90] == b'FAT32   ' else struct.unpack('<I', mm[454:458])[0] * 512
if mm[part + 82:part + 90] != b'FAT32   ':
    sys.exit("no FAT32 file system found in this image")
bps = struct.unpack('<H', mm[part + 11:part + 13])[0]
cs = bps * mm[part + 13]
fat = part + struct.unpack('<H', mm[part + 14:part + 16])[0] * bps
data = fat + mm[part + 16] * struct.unpack('<I', mm[part + 36:part + 40])[0] * bps
ncl = (len(mm) - data) // cs + 2


def cstart(c):
    return data + (c - 2) * cs


# The real file size comes from the header atoms (ftyp, free, mdat) plus the moov:
# the directory entry may hold a wrong size.
head = open(head_file, 'rb').read(cs)
p = 0
while True:
    s = struct.unpack('>I', head[p:p + 4])[0]
    t = head[p + 4:p + 8]
    if s == 1:
        s = struct.unpack('>Q', head[p + 8:p + 16])[0]
    if t == b'mdat':
        size = p + s + moov_size
        break
    if s < 8:
        sys.exit("could not read the MP4 header in the TestDisk copy")
    p += s

moov_end = moov_start + moov_size
last = (moov_end - 1 - data) // cs + 2
K = (size + cs - 1) // cs
if (size - 1) % cs != (moov_end - 1 - cstart(last)):
    sys.exit("the end of this moov does not line up with the file size; "
             "it is probably the .LRV's moov, try the other one")

# ffprobe reads the frame table from a stand-in file: real header + moov in place
tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
tmp.write(head)
tmp.truncate(size - moov_size)
tmp.seek(size - moov_size)
tmp.write(mm[moov_start:moov_end])
tmp.close()
packets = json.loads(subprocess.run(
    ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0', '-show_entries', 'packet=pos,size',
     '-of', 'json', tmp.name], capture_output=True).stdout)['packets']
os.unlink(tmp.name)
samples = sorted((int(q['pos']), int(q['size'])) for q in packets)
N = len(samples)
if N == 0:
    sys.exit("ffprobe found no video frames in this moov")

start = mm.find(head)
if start < 0 or (start - data) % cs:
    sys.exit("the header of the TestDisk copy was not found in the image")
first = (start - data) // cs + 2


class Mapping:
    """Sorted points (k, c): from logical cluster k on, the file runs contiguously
    in the image from cluster c."""

    def __init__(self, ks, cs_):
        self.k, self.c = list(ks), list(cs_)

    def at(self, k):
        i = bisect.bisect_right(self.k, k) - 1
        if i < 0:
            return None
        return self.c[i] + (k - self.k[i])

    def cut(self, k, c):
        """Same mapping, with a new segment starting at k -> c."""
        j = bisect.bisect_left(self.k, k)
        return Mapping(self.k[:j] + [k], self.c[:j] + [c])


class RMapping:
    """Mirror of Mapping for the backward pass: point (k, c) means logical cluster k
    is image cluster c, and going back (k-1, k-2...) it runs contiguously until the
    next point. k is kept in decreasing order."""

    def __init__(self, ks, cs_):
        self.k, self.c = list(ks), list(cs_)

    def at(self, k):
        best = None
        for kk, cc in zip(self.k, self.c):
            if kk >= k:
                best = (kk, cc)
            else:
                break
        if best is None:
            return None
        return best[1] - (best[0] - k)

    def cut(self, k, c):
        j = 0
        while j < len(self.k) and self.k[j] > k:
            j += 1
        return RMapping(self.k[:j] + [k], self.c[:j] + [c])


def read(pos, n, m):
    buf = bytearray()
    while n > 0:
        k, o = divmod(pos, cs)
        c = m(k)
        if c is None or not 2 <= c < ncl:
            return None
        take = min(n, cs - o)
        a = cstart(c) + o
        buf += mm[a:a + take]
        pos += take
        n -= take
    return bytes(buf)


def nal_chain(b):
    """True when the 4-byte NAL lengths inside the frame add up to its exact size."""
    q, sz = 0, len(b)
    while q < sz:
        if q + 5 > sz:
            return False
        v = struct.unpack('>I', b[q:q + 4])[0]
        if v == 0 or b[q + 4] & 0x80:
            return False
        q += 4 + v
    return q == sz


def ok(i, m):
    pos, sz = samples[i]
    b = read(pos, sz, m)
    return b is not None and nal_chain(b)


def prefilter(c, o, sz):
    a = cstart(c) + o
    h = mm[a:a + 5]
    v = struct.unpack('>I', h[:4])[0]
    return not (v == 0 or v > sz or h[4] & 0x80)


NEAR = 20000  # clusters searched after the current spot before scanning the whole image


def forward():
    m = Mapping([0], [first])
    i = 0
    while i < N:
        if ok(i, m.at):
            i += 1
            continue
        pos, sz = samples[i]
        ks = pos // cs
        here = m.at(ks)
        found = None
        for rng in (range(here + 1, min(ncl, here + NEAR)), range(2, ncl)):
            # 1) the next piece starts exactly where this frame starts
            for c in rng:
                if not prefilter(c, pos - ks * cs, sz):
                    continue
                t = m.cut(ks, c)
                if ok(i, t.at) and (i + 1 >= N or ok(i + 1, t.at)):
                    found = t
                    break
            # 2) the jump is inside this frame: find the next frame, then the cut point
            if found is None and i + 1 < N:
                p2, s2 = samples[i + 1]
                k2 = p2 // cs
                for c in rng:
                    if not prefilter(c, p2 - k2 * cs, s2):
                        continue
                    t = m.cut(k2, c)
                    if ok(i + 1, t.at):
                        for j in range(ks + 1, k2 + 1):
                            tj = m.cut(j, c - (k2 - j))
                            if ok(i, tj.at):
                                t = tj
                                break
                        found = t
                        break
            if found:
                break
        if found is None:
            return m, i
        m = found
        i += 1
    return m, N


def backward(stop_at):
    m = RMapping([K - 1], [last])
    i = N - 1
    while i >= stop_at:
        if ok(i, m.at):
            i -= 1
            continue
        pos, sz = samples[i]
        ke = (pos + sz - 1) // cs
        here = m.at(ke)
        found = None
        for rng in (range(here - 1, max(1, here - NEAR), -1), range(ncl - 1, 1, -1)):
            for c in rng:
                t = m.cut(ke, c)
                c0 = t.at(pos // cs)
                if c0 is None or not 2 <= c0 < ncl or not prefilter(c0, pos % cs, sz):
                    continue
                if ok(i, t.at) and (i == 0 or ok(i - 1, t.at)):
                    found = t
                    break
            if found:
                break
        if found is None:
            return m, i
        m = found
        i -= 1
    return m, stop_at - 1


fm, fstop = forward()
print(f"forward pass: {fstop}/{N} frames")
if fstop < N:
    bm, bstop = backward(fstop)
    print(f"backward pass: frames {bstop + 1}..{N - 1}")
    split = samples[bstop + 1][0] // cs if bstop + 1 < N else K
    final = lambda k: fm.at(k) if k < split else bm.at(k)  # noqa: E731
else:
    final = fm.at

with open(out, 'wb') as o:
    for k in range(K):
        n = cs if k < K - 1 else size - k * cs
        c = final(k)
        if c is None or not 2 <= c < ncl:
            o.write(b'\0' * n)
            continue
        a = cstart(c)
        o.write(mm[a:a + n])
bad = sum(1 for n in range(N) if not ok(n, final))
print(f"valid frames: {N - bad}/{N} ({(N - bad) / N:.0%})  size={size}")
if fstop < N:
    print(f"clean part: the first {fstop} frames (at 59.94 fps, about {fstop / 59.94:.1f} s)")
