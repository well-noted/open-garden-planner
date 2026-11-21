"""Foliage micro-leaf geometry and 2D-palette colour synthesis for Plant Forge (Phase 17 L3.1, #387).

All geometry is plain NumPy in the SCENE frame (x = East, y = North, z = up, cm).
No alpha cards: leaves are 4-vertex / 2-triangle (or 6-vertex / 4-triangle lobed/cupped)
geometric micro-leaves with normals spherized toward the crown centre so crowns shade as
cohesive volumes, and ``uv.u`` carrying wind sway weight ``0..1`` while ``uv.v`` carries
the per-leaf wind phase ``0..1``.

Qt-free: standard library + NumPy only.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..plant_art import DEFAULT_PALETTE, PALETTES, SpriteRef, has_recipe, recipe_for
from ..scene3d.builders.plants import FOLIAGE_TIP_WEIGHT
from ..scene3d.mesh import Mesh


def crown_leaf_budget(
    height_cm: float, radius_cm: float, trunk_share: float,
    leaf_length: float, width_ratio: float, max_leaves: int,
) -> tuple[int, float]:
    """Preserve 1.6 crown-surface areas of leaves within a triangle budget.

    The ellipsoid area uses Knud Thomsen's approximation (p=1.6075).
    A diamond leaf has area length*width/2. Prefer more leaves, then enlarge
    them when a distant LOD cannot afford the count; never thin the crown.
    """
    rz = max(height_cm * (1.0 - trunk_share) * 0.5, 1.0)
    p = 1.6075
    surface = 4.0 * math.pi * ((radius_cm**(2*p) + 2*(radius_cm*rz)**p) / 3)**(1/p)
    target = 1.6 * surface
    leaf_area = 0.5 * leaf_length**2 * width_ratio
    wanted = max(1, math.ceil(target / max(leaf_area, 1e-6)))
    count = min(wanted, max(1, max_leaves))
    length = max(leaf_length, math.sqrt(2.0 * target / (count * width_ratio)))
    return count, length


@dataclass
class RawGeo:
    """Mutable scene-frame triangle mesh buffers before final ``MeshPart`` assembly."""

    pos: np.ndarray  # (V, 3) float32
    nrm: np.ndarray  # (V, 3) float32
    col: np.ndarray  # (V, 4) float32 linear RGBA
    uv: np.ndarray   # (V, 2) float32
    tri: np.ndarray  # (T, 3) uint32

    @property
    def vertex_count(self) -> int:
        return int(len(self.pos))

    @property
    def triangle_count(self) -> int:
        return int(len(self.tri))

    def copy(self) -> RawGeo:
        return RawGeo(
            self.pos.copy(),
            self.nrm.copy(),
            self.col.copy(),
            self.uv.copy(),
            self.tri.copy(),
        )

    def to_mesh(self) -> Mesh:
        return Mesh.from_arrays(self.pos, self.nrm, self.col, self.uv, self.tri)


def empty_geo() -> RawGeo:
    """Return an empty ``RawGeo``."""
    return RawGeo(
        np.zeros((0, 3), dtype=np.float32),
        np.zeros((0, 3), dtype=np.float32),
        np.zeros((0, 4), dtype=np.float32),
        np.zeros((0, 2), dtype=np.float32),
        np.zeros((0, 3), dtype=np.uint32),
    )


def concat_geo(parts: Sequence[RawGeo]) -> RawGeo:
    """Concatenate non-empty ``RawGeo`` parts into a single ``RawGeo``."""
    non_empty = [p for p in parts if p.vertex_count > 0 and p.triangle_count > 0]
    if not non_empty:
        return empty_geo()
    if len(non_empty) == 1:
        return non_empty[0].copy()
    offsets = np.cumsum([0] + [p.vertex_count for p in non_empty[:-1]], dtype=np.uint32)
    return RawGeo(
        pos=np.concatenate([p.pos for p in non_empty], axis=0).astype(np.float32, copy=False),
        nrm=np.concatenate([p.nrm for p in non_empty], axis=0).astype(np.float32, copy=False),
        col=np.concatenate([p.col for p in non_empty], axis=0).astype(np.float32, copy=False),
        uv=np.concatenate([p.uv for p in non_empty], axis=0).astype(np.float32, copy=False),
        tri=np.concatenate(
            [p.tri + offset for p, offset in zip(non_empty, offsets, strict=True)],
            axis=0,
        ).astype(np.uint32, copy=False),
    )


def normalize_vec(v: np.ndarray) -> np.ndarray:
    """Normalise row vectors ``(..., 3)`` safely."""
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    return np.asarray(v / np.maximum(norm, 1e-9), dtype=np.float64)


def _srgb_hex_to_rgb01(hex_str: str) -> np.ndarray:
    h = hex_str.lstrip("#")
    return np.array([int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)], dtype=np.float64) / 255.0


def _srgb01_to_linear(srgb: np.ndarray) -> np.ndarray:
    return np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)


def palette_leaf_colors(
    palette_name: str,
    sprite: SpriteRef | None,
    height_frac: np.ndarray,
    depth_frac: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Synthesise linear RGBA colours ``(N, 4)`` anchored to the 2D sprite palette.

    Mixes palette anchors in sRGB (matching SVG linear/radial gradients) with
    mean tip weight ``FOLIAGE_TIP_WEIGHT[archetype]``, then converts to linear
    light so the mean sRGB foliage hue stays within 10° of the 2D sprite across
    all 123 recipes.
    """
    pal = PALETTES.get(palette_name, PALETTES[DEFAULT_PALETTE])
    t0 = _srgb_hex_to_rgb01(pal["t0"])
    t1 = _srgb_hex_to_rgb01(pal["t1"])
    b0 = _srgb_hex_to_rgb01(pal["b0"])
    b1 = _srgb_hex_to_rgb01(pal["b1"])

    n = len(height_frac)
    if n == 0:
        return np.zeros((0, 4), dtype=np.float32)

    archetype = (
        str(recipe_for(sprite).get("a", "canopy"))
        if sprite is not None and has_recipe(sprite)
        else "canopy"
    )
    target_tip = float(FOLIAGE_TIP_WEIGHT.get(archetype, 0.43))

    hf = np.clip(np.asarray(height_frac, dtype=np.float64), 0.0, 1.0)
    df = np.clip(np.asarray(depth_frac, dtype=np.float64), 0.0, 1.0)
    exposure = 0.5 * hf + 0.5 * df
    tip = np.clip(target_tip + 0.65 * (exposure - float(exposure.mean())), 0.0, 1.0)
    tip = np.clip(tip + (target_tip - float(tip.mean())), 0.0, 1.0)[:, None]

    tone = rng.uniform(0.15, 0.85, size=(n, 1))
    base_srgb = b0[None, :] + (b1 - b0)[None, :] * tone
    tip_srgb = t0[None, :] + (t1 - t0)[None, :] * tone
    srgb = base_srgb + (tip_srgb - base_srgb) * tip

    # Hue-neutral value shade in sRGB (stays within [40..240]/255 albedo range and preserves hue)
    shade = (0.86 + 0.14 * df)[:, None] * rng.uniform(0.96, 1.04, size=(n, 1))
    shade /= max(float(shade.mean()), 1e-6)
    srgb = np.clip(srgb * np.clip(shade, 0.82, 1.08), 42.0 / 255.0, 235.0 / 255.0)
    lin = np.clip(_srgb01_to_linear(srgb), 0.0, 1.0)

    out = np.ones((n, 4), dtype=np.float32)
    out[:, :3] = lin.astype(np.float32)
    return out


def micro_leaves(
    positions: np.ndarray,
    outward: np.ndarray,
    length: np.ndarray | float,
    width: np.ndarray | float,
    colors: np.ndarray,
    rng: np.random.Generator,
    *,
    center: np.ndarray | None = None,
    spherize: float = 0.72,
    droop: float = 0.15,
    cup: float = 0.0,
) -> RawGeo:
    """Build 4-vertex / 2-triangle diamond/ovate micro-leaves at ``positions``.

    Normals are blended toward ``positions - center`` (normal spherization) so
    canopies shade smoothly while keeping individual leaf silhouettes.
    """
    pts = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    k = len(pts)
    if k == 0:
        return empty_geo()

    out = np.asarray(outward, dtype=np.float64).reshape(k, 3)
    n0 = normalize_vec(out + rng.normal(0.0, 0.28, size=(k, 3)))
    rnd = normalize_vec(rng.normal(0.0, 1.0, size=(k, 3)))
    along = normalize_vec(rnd - (rnd * n0).sum(axis=1, keepdims=True) * n0)
    along = normalize_vec(along - np.array([0.0, 0.0, float(droop)], dtype=np.float64))
    side = normalize_vec(np.cross(along, n0))

    ln = np.broadcast_to(np.asarray(length, dtype=np.float64).reshape(-1), (k,))[:, None]
    wd = np.broadcast_to(np.asarray(width, dtype=np.float64).reshape(-1), (k,))[:, None]

    v0 = pts
    mid = pts + along * (ln * 0.46) + n0 * (ln * float(cup))
    v1 = mid + side * (wd * 0.5)
    v2 = pts + along * ln
    v3 = mid - side * (wd * 0.5)

    # Ensure every leaf triangle's winding matches its outward normal n0
    cross = np.cross(v1 - v0, v2 - v0)
    flip = (cross * n0).sum(axis=1) < 0.0
    if np.any(flip):
        v1_f = v1[flip].copy()
        v1[flip] = v3[flip]
        v3[flip] = v1_f

    pos = np.stack([v0, v1, v2, v3], axis=1).reshape(-1, 3).astype(np.float32)

    if center is not None and spherize > 0.0:
        c = np.asarray(center, dtype=np.float64).reshape(1, 3)
        radial = normalize_vec(pts - c)
        # Keep spherized normal in the same hemisphere as the triangle winding normal
        same_hemi = (radial * n0).sum(axis=1, keepdims=True) >= 0.0
        radial = np.where(same_hemi, radial, n0)
        n = normalize_vec(n0 * (1.0 - float(spherize)) + radial * float(spherize))
    else:
        n = n0
    nrm = np.repeat(n.astype(np.float32), 4, axis=0)

    cols = np.repeat(np.asarray(colors, dtype=np.float32).reshape(k, 4), 4, axis=0)
    tip_mask = (np.arange(len(cols)) % 4) == 2
    cols[tip_mask, :3] = np.minimum(cols[tip_mask, :3] * 1.05, 1.0)

    z_max = max(float(pts[:, 2].max()), 1.0)
    wind_weight = np.clip(pts[:, 2:3] / z_max, 0.0, 1.0).astype(np.float32)
    phase = rng.random((k, 1), dtype=np.float32)
    uv = np.concatenate([np.repeat(wind_weight, 4, axis=0), np.repeat(phase, 4, axis=0)], axis=1)

    quad = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    tri = (quad[None, :, :] + (np.arange(k, dtype=np.uint32) * 4)[:, None, None]).reshape(-1, 3)
    return RawGeo(pos=pos, nrm=nrm, col=cols, uv=uv, tri=tri)


def _bilobed_template() -> tuple[np.ndarray, np.ndarray]:
    """Butterfly leaf: a broad round base and two lobes joined at a notch (6 triangles)."""
    right = [(0.22, 0.38), (0.62, 0.50), (0.96, 0.30)]
    verts = [(0.0, 0.0, 0.0)] + [(u, v, 1.0 if u > 0.9 else 0.0) for u, v in right]
    verts.append((0.74, 0.0, 0.0))  # the notch between the lobes
    verts += [(u, -v, 1.0 if u > 0.9 else 0.0) for u, v in reversed(right)]
    tris = [(0, k, k + 1) for k in range(1, 7)]
    return np.asarray(verts, dtype=np.float64), np.asarray(tris, dtype=np.int64)


def _compound_template(pairs: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """A pinnately compound leaf: ``pairs`` wedge leaflets either side of the rachis
    plus a terminal one (``2 * pairs + 1`` triangles). Mid-leaf leaflets are the broadest."""
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    for i in range(pairs):
        u0 = 0.10 + 0.15 * i
        reach = 0.5 * (0.62 + 0.38 * math.sin(math.pi * (i + 0.6) / (pairs + 0.4)))
        for side in (1.0, -1.0):
            base = len(verts)
            verts += [(u0, 0.0, 0.0), (u0 + 0.10, 0.0, 0.0), (u0 + 0.24, side * reach, 1.0)]
            tris.append((base, base + 1, base + 2) if side > 0 else (base, base + 2, base + 1))
    base = len(verts)
    verts += [(0.84, -0.08, 0.0), (1.0, 0.0, 1.0), (0.84, 0.08, 0.0)]
    tris.append((base, base + 1, base + 2))
    return np.asarray(verts, dtype=np.float64), np.asarray(tris, dtype=np.int64)


#: Leaf shapes drawn as a template (vertices ``(u, v, tip)``, triangles, fold) instead
#: of the diamond ``micro_leaves`` builds: ``u`` runs 0..1 along the leaf, ``v`` is
#: the side in units of the leaf's width, ``fold`` how far the blade's edge rises
#: above its midrib (a V-section: the leaf catches light).
LEAF_TEMPLATES: dict[str, tuple[np.ndarray, np.ndarray, float]] = {
    "bilobed": (*_bilobed_template(), 0.30),
    "compound": (*_compound_template(), 0.22),
}


def leaf_triangles(shape: str) -> int:
    """Triangles one leaf of ``shape`` costs (2 for the diamond every other shape draws)."""
    template = LEAF_TEMPLATES.get(shape)
    return 2 if template is None else int(len(template[1]))


def template_leaves(
    shape: str,
    positions: np.ndarray,
    outward: np.ndarray,
    length: np.ndarray | float,
    width: np.ndarray | float,
    colors: np.ndarray,
    rng: np.random.Generator,
    *,
    center: np.ndarray | None = None,
    spherize: float = 0.72,
    droop: float = 0.15,
) -> RawGeo:
    """Leaves of a ``LEAF_TEMPLATES`` shape at ``positions``; the frame, the normal
    spherization and the wind weight are ``micro_leaves``'."""
    verts, tris, fold = LEAF_TEMPLATES[shape]
    pts = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    k = len(pts)
    if k == 0:
        return empty_geo()
    out = np.asarray(outward, dtype=np.float64).reshape(k, 3)
    n0 = normalize_vec(out + rng.normal(0.0, 0.28, size=(k, 3)))
    rnd = normalize_vec(rng.normal(0.0, 1.0, size=(k, 3)))
    along = normalize_vec(rnd - (rnd * n0).sum(axis=1, keepdims=True) * n0)
    along = normalize_vec(along - np.array([0.0, 0.0, float(droop)], dtype=np.float64))
    side = normalize_vec(np.cross(along, n0))
    ln = np.broadcast_to(np.asarray(length, dtype=np.float64).reshape(-1), (k,))[:, None, None]
    wd = np.broadcast_to(np.asarray(width, dtype=np.float64).reshape(-1), (k,))[:, None, None]

    u = verts[None, :, 0:1]
    v = verts[None, :, 1:2]
    # the template's own winding decides which way ``side`` must point for the blade's
    # front to face ``n0`` (the same rule as the diamond's flip)
    a, b, c = verts[tris[0], :2]
    template_sign = float(np.sign((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])))
    sgn = -template_sign
    pos = (
        pts[:, None, :]
        + along[:, None, :] * (u * ln)
        + side[:, None, :] * (sgn * v * wd)
        + n0[:, None, :] * (fold * np.abs(v) * wd)
    )
    nv = len(verts)
    pos = pos.reshape(-1, 3).astype(np.float32)

    if center is not None and spherize > 0.0:
        c0 = np.asarray(center, dtype=np.float64).reshape(1, 3)
        radial = normalize_vec(pts - c0)
        same_hemi = (radial * n0).sum(axis=1, keepdims=True) >= 0.0
        radial = np.where(same_hemi, radial, n0)
        n = normalize_vec(n0 * (1.0 - float(spherize)) + radial * float(spherize))
    else:
        n = n0
    nrm = np.repeat(n.astype(np.float32), nv, axis=0)

    cols = np.repeat(np.asarray(colors, dtype=np.float32).reshape(k, 4), nv, axis=0)
    tip = np.tile(verts[:, 2], k)
    cols[:, :3] = np.minimum(cols[:, :3] * (1.0 + 0.05 * tip[:, None]), 1.0)

    z_max = max(float(pts[:, 2].max()), 1.0)
    wind_weight = np.clip(pts[:, 2:3] / z_max, 0.0, 1.0).astype(np.float32)
    phase = rng.random((k, 1), dtype=np.float32)
    uv = np.concatenate([np.repeat(wind_weight, nv, axis=0), np.repeat(phase, nv, axis=0)], axis=1)

    index = (tris[None, :, :] + (np.arange(k, dtype=np.int64) * nv)[:, None, None]).reshape(-1, 3)
    return RawGeo(pos=pos, nrm=nrm, col=cols, uv=uv, tri=index.astype(np.uint32))


__all__ = [
    "LEAF_TEMPLATES",
    "RawGeo",
    "concat_geo",
    "empty_geo",
    "leaf_triangles",
    "micro_leaves",
    "normalize_vec",
    "palette_leaf_colors",
    "template_leaves",
]
