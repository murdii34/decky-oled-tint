"""Generate the dither's blue-noise threshold masks (void and cluster,
Ulichney 1993) and print them as the base64 constants main.py embeds.

Run: python3 scripts/make_bluenoise.py CELL  (CELL = dither cell height).
The dither works in cells one pixel wide and CELL rows tall, so the mask is
64 cells across and 64/CELL down (a 64x64-pixel tile), and distances are
measured on screen: one cell down is CELL pixels. A mask made for square
cells and then stretched clumps vertically (horizontal lines at mid shares).
Each value is its cell's rank, so thresholding at any share lights exactly
that share, spread without clumps or direction.
"""
import base64
import sys
import zlib

import numpy as np

W = 64
CELL = int(sys.argv[1]) if len(sys.argv) > 1 else 1
H = W // CELL
SIGMA = 1.5            # in pixels, for square cells


def kernel():
    """Isotropic on screen, in pixels."""
    d = np.minimum(np.arange(W), W - np.arange(W))
    sig = SIGMA * np.sqrt(CELL)        # cells are CELL times sparser than pixels
    g = np.exp(-(d[None, :] ** 2 + d[:, None] ** 2) / (2 * sig ** 2))
    return np.fft.rfft2(g)


K = kernel()


def energy(pattern):
    """Each cell's energy as the screen sees it: the cell pattern drawn as
    1xCELL segments, blurred isotropically, summed over the cell's pixels.
    (Measuring between cell centres let cells stack into long streaks.)"""
    px = np.repeat(pattern, CELL, axis=0)
    e = np.fft.irfft2(np.fft.rfft2(px) * K, s=(W, W))
    return e.reshape(H, CELL, W).sum(axis=1)


def tightest_cluster(p):
    e = np.where(p == 1, energy(p), -np.inf)
    return np.unravel_index(np.argmax(e), p.shape)


def largest_void(p):
    e = np.where(p == 0, energy(p), np.inf)
    return np.unravel_index(np.argmin(e), p.shape)


def make(seed=1):
    rng = np.random.default_rng(seed)
    n = W * H
    ones = n // 10
    p = np.zeros((H, W))
    p.flat[rng.choice(n, ones, replace=False)] = 1
    # Spread the initial points evenly: swap the tightest cluster into the
    # largest void until that's a no-op.
    while True:
        c = tightest_cluster(p)
        p[c] = 0
        v = largest_void(p)
        if v == c:
            p[c] = 1
            break
        p[v] = 1
    rank = np.zeros((H, W), dtype=np.int64)
    q = p.copy()
    for r in range(ones - 1, -1, -1):          # ranks below: remove clusters
        c = tightest_cluster(q)
        q[c] = 0
        rank[c] = r
    q = p.copy()
    for r in range(ones, n // 2):          # up to half: fill voids
        v = largest_void(q)
        q[v] = 1
        rank[v] = r
    inv = 1 - q                                # past half: clusters of the empty ones
    for r in range(n // 2, n):
        c = tightest_cluster(inv)
        inv[c] = 0
        rank[c] = r
    return rank


def main():
    rank = make()
    n = W * H
    assert sorted(rank.flat) == list(range(n))
    mask = (rank * 256 // n).astype(np.uint8)               # n/256 cells per 8-bit level
    print(base64.b64encode(zlib.compress(mask.tobytes(), 9)).decode())
    print(f"{W}x{H} cells of 1x{CELL}, levels {mask.min()}..{mask.max()}", file=sys.stderr)


if __name__ == "__main__":
    main()
