"""
predictor.py – Road Network Completion Engine
=============================================

Detects dangling endpoints in a cleaned centerline network and proposes
smooth cubic-Bézier connectors scored by geometric compatibility.

Algorithm overview
------------------
1.  Scan every LineString endpoint.  If no *other* line ends within
    `snap_tol` of that point, it is "dangling" (degree-1 node).
2.  For every ordered pair of dangling endpoints compute:
        dist_score  = 1 - dist/max_gap           (closer → higher)
        angle_score = (cos θ₁ + cos θ₂) / 2      (more aligned → higher)
        score       = dist_score × angle_score
    where θ₁ is the angle between ep₁'s outward tangent and the gap
    direction, and θ₂ is the same for ep₂ pointing back.
3.  Accept only pairs where *both* tangents are within max_angle_deg
    of the gap direction and score ≥ min_confidence.
4.  Rank by score (descending), pick greedily so no endpoint is
    connected twice.
5.  For each accepted pair, generate a cubic Bézier connector that
    exits ep₁ and ep₂ along their respective tangent directions.
    This produces roads that curve smoothly into intersections rather
    than meeting at a sharp kink.

Usage
-----
    engine = CompletionEngine(lines, max_gap=0.005, max_angle_deg=50)
    proposals = engine.run()   # → list[Proposal], best-first

Each Proposal
-------------
    .line     – connecting LineString (Bézier curve)
    .score    – float 0–1 confidence
    .ep1/.ep2 – Endpoint describing each dangling end
    .accepted – bool; user may toggle to False before applying
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Endpoint:
    line_idx: int     # index into the source lines list
    at_start: bool    # True = line start, False = line end
    pos: np.ndarray   # shape (2,)  –  (x, y)
    tang: np.ndarray  # shape (2,)  –  unit outward tangent


@dataclass
class Proposal:
    ep1: Endpoint
    ep2: Endpoint
    score: float          # 0.0 – 1.0
    line: LineString      # proposed connector geometry
    accepted: bool = field(default=True)


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _outward_tangent(line: LineString, at_start: bool, n_pts: int = 5) -> np.ndarray:
    """
    Compute the unit tangent at an endpoint, pointing **away** from the line.

    Uses up to *n_pts* coordinates near the endpoint to average out
    digitising noise – a single-vertex estimate is sensitive to jagged geometry.
    """
    coords = np.array(line.coords)
    n = min(n_pts, len(coords))
    if at_start:
        pts = coords[:n]
        vec = pts[0] - pts[-1]   # outward ← away from interior
    else:
        pts = coords[-n:]
        vec = pts[-1] - pts[0]   # outward → away from interior
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 1e-12 else np.array([1.0, 0.0])


def _bezier(P0, P1, P2, P3, n_pts: int = 24) -> LineString:
    """Sample n_pts points along the cubic Bézier defined by four control points."""
    t = np.linspace(0.0, 1.0, n_pts)[:, None]
    pts = (
        (1.0 - t) ** 3 * P0
        + 3.0 * (1.0 - t) ** 2 * t * P1
        + 3.0 * (1.0 - t) * t ** 2 * P2
        + t ** 3 * P3
    )
    return LineString(pts)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CompletionEngine:
    """Detect gaps and propose smooth connectors for a road centerline network."""

    def __init__(
        self,
        lines: list[LineString],
        max_gap: float,
        max_angle_deg: float = 60.0,
        min_confidence: float = 0.15,
        snap_tol: float | None = None,
    ) -> None:
        """
        Parameters
        ----------
        lines          : current road centerline segments
        max_gap        : maximum allowable gap distance to bridge
        max_angle_deg  : how far each tangent may deviate from the gap direction
        min_confidence : discard proposals below this score
        snap_tol       : endpoints within this distance are considered connected
                         (auto-derived as 0.5 % of mean line length when None)
        """
        self.lines         = lines
        self.max_gap       = max_gap
        self.max_angle_deg = max_angle_deg
        self.min_confidence = min_confidence

        lengths = [l.length for l in lines if l.length > 0]
        self.snap_tol = snap_tol or (np.mean(lengths) * 0.005 if lengths else 1e-6)

    # ── internal ─────────────────────────────────────────────────────────────

    def _find_dangling(self) -> list[Endpoint]:
        """Return every endpoint that is not connected to any other line."""
        raw: list[tuple[int, bool, np.ndarray]] = []
        for i, line in enumerate(self.lines):
            coords = np.array(line.coords)
            raw.append((i, True,  coords[0]))
            raw.append((i, False, coords[-1]))

        dangling: list[Endpoint] = []
        for idx, (i, at_start, pos) in enumerate(raw):
            connected = any(
                jdx != idx
                and j != i
                and float(np.linalg.norm(pos - pos_j)) <= self.snap_tol
                for jdx, (j, _, pos_j) in enumerate(raw)
            )
            if not connected:
                tang = _outward_tangent(self.lines[i], at_start)
                dangling.append(Endpoint(i, at_start, pos.copy(), tang))

        return dangling

    def _score_pair(self, ep1: Endpoint, ep2: Endpoint) -> float | None:
        """Return a compatibility score [0,1] or None if the pair is unsuitable."""
        gap_vec = ep2.pos - ep1.pos
        dist    = float(np.linalg.norm(gap_vec))
        if dist < 1e-9 or dist > self.max_gap:
            return None

        gap_dir = gap_vec / dist
        max_cos = np.cos(np.radians(self.max_angle_deg))

        cos1 = float(np.dot(ep1.tang,  gap_dir))   # ep1 pointing toward ep2
        cos2 = float(np.dot(ep2.tang, -gap_dir))   # ep2 pointing toward ep1
        if cos1 < max_cos or cos2 < max_cos:
            return None

        dist_score  = 1.0 - dist / self.max_gap
        angle_score = (cos1 + cos2) / 2.0
        return float(dist_score * angle_score)

    def _make_connector(self, ep1: Endpoint, ep2: Endpoint) -> LineString:
        """Build a smooth cubic Bézier that exits each endpoint along its tangent."""
        dist   = float(np.linalg.norm(ep2.pos - ep1.pos))
        handle = dist * 0.4   # length of the Bézier "handles"
        P0 = ep1.pos
        P1 = ep1.pos + ep1.tang * handle
        P2 = ep2.pos + ep2.tang * handle
        P3 = ep2.pos
        # More sample points → smoother curve for longer connectors
        n_pts = max(6, int(dist / self.max_gap * 40))
        return _bezier(P0, P1, P2, P3, n_pts)

    # ── public API ───────────────────────────────────────────────────────────

    def run(self) -> list[Proposal]:
        """
        Find, score, and return a de-conflicted list of Proposals (best first).

        "De-conflicted" means each dangling endpoint appears in at most one
        proposal – we pick greedily by score so the best match always wins.
        """
        dangling   = self._find_dangling()
        candidates: list[Proposal] = []

        for i in range(len(dangling)):
            for j in range(i + 1, len(dangling)):
                ep1, ep2 = dangling[i], dangling[j]
                if ep1.line_idx == ep2.line_idx:
                    continue   # don't bridge an endpoint to itself
                score = self._score_pair(ep1, ep2)
                if score is not None and score >= self.min_confidence:
                    candidates.append(
                        Proposal(ep1, ep2, score, self._make_connector(ep1, ep2))
                    )

        # Greedy selection – best first, no endpoint reused
        candidates.sort(key=lambda p: p.score, reverse=True)
        used: set[tuple] = set()
        selected: list[Proposal] = []
        for prop in candidates:
            k1 = (prop.ep1.line_idx, prop.ep1.at_start)
            k2 = (prop.ep2.line_idx, prop.ep2.at_start)
            if k1 in used or k2 in used:
                continue
            selected.append(prop)
            used.add(k1)
            used.add(k2)

        return selected

    @staticmethod
    def dangling_count(lines: list[LineString], snap_tol: float = 1e-6) -> int:
        """Quick helper: how many dangling endpoints does the network have?"""
        raw = []
        for i, line in enumerate(lines):
            coords = np.array(line.coords)
            raw.append((i, coords[0]))
            raw.append((i, coords[-1]))
        count = 0
        for idx, (i, pos) in enumerate(raw):
            if not any(
                jdx != idx and j != i
                and float(np.linalg.norm(pos - p)) <= snap_tol
                for jdx, (j, p) in enumerate(raw)
            ):
                count += 1
        return count
