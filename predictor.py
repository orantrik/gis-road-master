"""
predictor.py – Road Network Completion Engine
=============================================

Detects dangling endpoints in a cleaned centerline network and proposes
smooth connectors scored by geometric compatibility.

Two connection modes
--------------------
1.  **Endpoint ↔ Endpoint**  (gap-bridging)
    Both ends of the gap are dangling.  A cubic Bézier is generated that
    exits each endpoint along its road's outward tangent direction.

2.  **Endpoint → Interior**  (T-junction snap)
    A dangling endpoint is near the *middle* of another line (not its
    endpoint).  A short straight connector is generated perpendicular to
    the target line.  This handles roads that end at a crossing road.

Scoring
-------
Previous versions used a hard `cos θ > threshold` cutoff on BOTH tangents,
which rejected valid T-junctions and curved approaches.  The new scoring is:

    dist_score  = 1 − dist / max_gap         (closer → higher)
    align_score = (clamp(cos θ₁, 0, 1)       (soft: each tangent that
                 + clamp(cos θ₂, 0, 1)) / 2   points away costs score,
                                               but doesn't hard-reject)
    score       = dist_score × align_score

A proposal is discarded only if `align_score == 0` (both tangents point
completely away from each other) or `score < min_confidence`.

For T-junctions `cos θ₂` is omitted (there is no "endpoint" on the target
line to compare) and the score uses only `cos θ₁` for the source tangent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString, Point


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Endpoint:
    line_idx: int     # index into the source lines list; -1 = interior point
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
    kind: str = field(default="gap")  # "gap" | "tjunction"


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _outward_tangent(line: LineString, at_start: bool, n_pts: int = 8) -> np.ndarray:
    """
    Unit tangent at an endpoint, pointing **away** from the line interior.

    Uses up to *n_pts* coordinates for a more stable direction estimate —
    important after Chaikin smoothing which can rotate the very last segment.
    """
    coords = np.array(line.coords)
    n = min(n_pts, len(coords))
    if at_start:
        pts = coords[:n]
        vec = pts[0] - pts[-1]
    else:
        pts = coords[-n:]
        vec = pts[-1] - pts[0]
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
    """Detect gaps and T-junctions; propose smooth connectors for a road network."""

    def __init__(
        self,
        lines: list[LineString],
        max_gap: float,
        max_angle_deg: float = 70.0,
        min_confidence: float = 0.05,
        snap_tol: float | None = None,
    ) -> None:
        """
        Parameters
        ----------
        lines          : current road centerline segments
        max_gap        : maximum gap distance to bridge (same CRS as lines)
        max_angle_deg  : soft angle limit – tangents beyond this still score
                         but receive a heavy penalty (unlike the old hard cutoff)
        min_confidence : discard proposals below this score (0–1)
        snap_tol       : endpoints closer than this are considered already
                         connected.  Auto-derived as 2 % of mean line length
                         (minimum 1e-5) when None.
        """
        self.lines          = lines
        self.max_gap        = max_gap
        self.max_angle_deg  = max_angle_deg
        self.min_confidence = min_confidence

        lengths = [l.length for l in lines if l.length > 0]
        mean_len = float(np.mean(lengths)) if lengths else 0.001
        self.snap_tol = snap_tol or max(mean_len * 0.02, 1e-5)

    # ── step 1: find dangling endpoints ──────────────────────────────────────

    def _find_dangling(self) -> list[Endpoint]:
        """Return every line endpoint not connected to any other line endpoint."""
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

    # ── step 2a: score endpoint↔endpoint pair ────────────────────────────────

    def _score_pair(self, ep1: Endpoint, ep2: Endpoint) -> float | None:
        """
        Soft scoring for two dangling endpoints.

        Unlike the old version, there is NO hard angle cutoff.  Both cosines
        are clamped to [0, 1] so tangents pointing away from the gap subtract
        nothing (they simply don't contribute positively).  A proposal is only
        rejected if both tangents point completely away (align_score == 0) or
        the distance exceeds max_gap.
        """
        gap_vec = ep2.pos - ep1.pos
        dist    = float(np.linalg.norm(gap_vec))
        if dist < 1e-9 or dist > self.max_gap:
            return None

        gap_dir = gap_vec / dist
        cos1 = float(np.dot(ep1.tang,  gap_dir))
        cos2 = float(np.dot(ep2.tang, -gap_dir))

        # Soft scoring: clamp negatives to 0 (don't hard-reject, just penalise)
        align_score = (max(0.0, cos1) + max(0.0, cos2)) / 2.0
        if align_score < 0.01:
            return None   # both tangents pointing away – genuinely wrong match

        dist_score = 1.0 - dist / self.max_gap
        return float(dist_score * align_score)

    def _make_gap_connector(self, ep1: Endpoint, ep2: Endpoint) -> LineString:
        """Cubic Bézier that exits each endpoint along its outward tangent."""
        dist   = float(np.linalg.norm(ep2.pos - ep1.pos))
        handle = dist * 0.4
        P0, P1 = ep1.pos, ep1.pos + ep1.tang * handle
        P2, P3 = ep2.pos + ep2.tang * handle, ep2.pos
        n_pts  = max(6, int(dist / self.max_gap * 40))
        return _bezier(P0, P1, P2, P3, n_pts)

    # ── step 2b: find T-junction connections ─────────────────────────────────

    def _find_t_junctions(self, dangling: list[Endpoint]) -> list[Proposal]:
        """
        For each dangling endpoint, check if it lies close to the *interior*
        of another line (not near its endpoints).  If so, propose a short
        straight connector perpendicular to that line.

        This handles the very common case of a road ending at a crossing road —
        the crossing road has no dangling endpoint at that location, so the
        standard endpoint↔endpoint search misses it completely.
        """
        proposals: list[Proposal] = []

        for ep in dangling:
            pt = Point(ep.pos)
            best_score = self.min_confidence
            best_prop: Proposal | None = None

            for line_idx, line in enumerate(self.lines):
                if line_idx == ep.line_idx:
                    continue

                # Quick bounding-box pre-filter
                bx1, by1, bx2, by2 = line.bounds
                px, py = ep.pos
                if (px < bx1 - self.max_gap or px > bx2 + self.max_gap
                        or py < by1 - self.max_gap or py > by2 + self.max_gap):
                    continue

                dist = float(pt.distance(line))
                if dist > self.max_gap or dist < 1e-9:
                    continue

                # Nearest point on the target line
                proj_frac = line.project(pt, normalized=True)
                # Skip if near an endpoint of the target line
                # (endpoint↔endpoint search handles that case)
                endpoint_frac = self.snap_tol / max(line.length, 1e-12)
                if proj_frac <= endpoint_frac or proj_frac >= 1.0 - endpoint_frac:
                    continue

                nearest = line.interpolate(proj_frac, normalized=True)
                gap_vec = np.array([nearest.x - ep.pos[0],
                                    nearest.y - ep.pos[1]])
                actual_dist = float(np.linalg.norm(gap_vec))
                if actual_dist < 1e-9:
                    continue

                gap_dir     = gap_vec / actual_dist
                cos1        = float(np.dot(ep.tang, gap_dir))
                align_score = max(0.0, cos1)
                if align_score < 0.01:
                    continue   # tangent points directly away from crossing

                dist_score = 1.0 - actual_dist / self.max_gap
                score      = float(dist_score * align_score)
                if score < best_score:
                    continue

                # Build a straight connector from dangling end to the crossing
                connector = LineString([ep.pos, [nearest.x, nearest.y]])
                # ep2 uses line_idx=-1 as sentinel (interior point, not endpoint)
                ep2 = Endpoint(-1, True,
                               np.array([nearest.x, nearest.y]), -ep.tang)
                best_score = score
                best_prop  = Proposal(ep, ep2, score, connector,
                                      kind="tjunction")

            if best_prop is not None:
                proposals.append(best_prop)

        return proposals

    # ── public API ───────────────────────────────────────────────────────────

    def run(self) -> list[Proposal]:
        """
        Find, score, and return a de-conflicted list of Proposals (best first).

        Combines endpoint↔endpoint gap-bridging with endpoint→interior
        T-junction snapping.  Each dangling endpoint is used at most once
        across both categories (greedy selection, highest score wins).
        """
        dangling = self._find_dangling()

        # ── endpoint↔endpoint ─────────────────────────────────────────────
        gap_candidates: list[Proposal] = []
        for i in range(len(dangling)):
            for j in range(i + 1, len(dangling)):
                ep1, ep2 = dangling[i], dangling[j]
                if ep1.line_idx == ep2.line_idx:
                    continue
                score = self._score_pair(ep1, ep2)
                if score is not None and score >= self.min_confidence:
                    gap_candidates.append(
                        Proposal(ep1, ep2, score,
                                 self._make_gap_connector(ep1, ep2),
                                 kind="gap")
                    )

        # ── endpoint→interior (T-junctions) ──────────────────────────────
        tj_candidates = self._find_t_junctions(dangling)

        # ── greedy de-conflicted selection ────────────────────────────────
        all_candidates = gap_candidates + tj_candidates
        all_candidates.sort(key=lambda p: p.score, reverse=True)

        used: set[tuple] = set()
        selected: list[Proposal] = []

        for prop in all_candidates:
            k1 = (prop.ep1.line_idx, prop.ep1.at_start)

            if prop.ep2.line_idx == -1:
                # T-junction: only the *source* endpoint is "used up";
                # the target line keeps all its own connections.
                if k1 in used:
                    continue
                selected.append(prop)
                used.add(k1)
            else:
                k2 = (prop.ep2.line_idx, prop.ep2.at_start)
                if k1 in used or k2 in used:
                    continue
                selected.append(prop)
                used.add(k1)
                used.add(k2)

        return selected

    @staticmethod
    def dangling_count(lines: list[LineString], snap_tol: float = 1e-5) -> int:
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
