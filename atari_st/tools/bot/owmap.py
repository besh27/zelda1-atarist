"""The overworld as a graph for the bot: the free nodes of every screen (from the tiles the port's decoder builds from
the ROM, which tools/refnes/compare_ow_tiles.py checks against the ROM), split into connected parts, joined across
screen edges. route() gives the screens and the edge to leave each by. Development tooling only."""
from __future__ import annotations

import collections
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "refnes"))
sys.path.insert(0, str(HERE.parent))

OW_WALKABLE = {0x8D, 0x91, 0x9C, 0xAC, 0xAD, 0xCC, 0xD2, 0xD5, 0xDF}
X0, X1, Y0, Y1 = 0x00, 0xF0, 0x3D, 0xDD


def walkable(t: int) -> bool:
    return t < 0x89 or t in OW_WALKABLE


def free(tiles, x: int, y: int) -> bool:
    row = (y + 0x0B - 0x40) >> 3
    if not 0 <= row < 22:
        return False
    c = x >> 3
    return walkable(tiles[row][c]) and walkable(tiles[row][min(c + 1, 31)])


# LadderRoomsOW: the screens where the ladder is put down over the water (CheckLadder; ow_ladder_ok checks $8D-$98)
LADDER_ROOMS = (0x17, 0x18, 0x19, 0x27, 0x4F, 0x5F)


def water(tiles, x: int, y: int) -> bool:
    """The node's feet are on water the ladder spans (tiles $8D-$98) and nothing worse."""
    row = (y + 0x0B - 0x40) >> 3
    if not 0 <= row < 22:
        return False
    c = x >> 3
    pair = (tiles[row][c], tiles[row][min(c + 1, 31)])
    wet = [t for t in pair if not walkable(t)]
    return bool(wet) and all(0x8D <= t <= 0x98 for t in wet)


def ladder_hops(tiles, n):
    """The free nodes the ladder takes Link to from the free node n: one square of water (two or three nodes of it)
    and land at most 32 pixels on, straight ahead (as dungeon moats, brain.Side.path)."""
    for dx, dy in ((8, 0), (-8, 0), (0, 8), (0, -8)):
        if not water(tiles, n[0] + dx, n[1] + dy):
            continue
        for k in (2, 3, 4):
            m = (n[0] + dx * k, n[1] + dy * k)
            if not (X0 <= m[0] <= X1 and Y0 <= m[1] <= Y1):
                break
            if free(tiles, *m):
                yield m, (dx, dy), k
                break
            if not water(tiles, *m):
                break


class OWGraph:
    def __init__(self, quest: int = 1, ladder: bool = False):
        """ladder: Link has it, so in LADDER_ROOMS a square of water joins the land on its two sides."""
        import compare_ow_tiles as C
        self.rooms = C.expected(quest, ({}, []))
        self.comp = {}          # (room, node) -> component id
        for room, tiles in enumerate(self.rooms):
            cid = 0
            hop = ladder and room in LADDER_ROOMS
            for y in range(Y0, Y1 + 1, 8):
                for x in range(X0, X1 + 1, 8):
                    if (room, (x, y)) in self.comp or not free(tiles, x, y):
                        continue
                    q = collections.deque([(x, y)])
                    self.comp[(room, (x, y))] = cid
                    while q:
                        n = q.popleft()
                        nxt = [(n[0] + dx, n[1] + dy) for dx, dy in ((8, 0), (-8, 0), (0, 8), (0, -8))]
                        if hop:
                            nxt += [m for m, _, _ in ladder_hops(tiles, n)]
                        for m in nxt:
                            if X0 <= m[0] <= X1 and Y0 <= m[1] <= Y1 and (room, m) not in self.comp and free(tiles, *m):
                                self.comp[(room, m)] = cid
                                q.append(m)
                    cid += 1
        # edges between (room, comp) across screen borders
        self.edges = collections.defaultdict(set)
        for (room, (x, y)), c in self.comp.items():
            col, row = room % 16, room // 16
            if x == X1 and col < 15:
                self._link(room, c, room + 1, (X0, y), "RIGHT")
            if x == X0 and col > 0:
                self._link(room, c, room - 1, (X1, y), "LEFT")
            if y == Y0 and row > 0:
                self._link(room, c, room - 16, (x, Y1), "UP")
            if y == Y1 and row < 7:
                self._link(room, c, room + 16, (x, Y0), "DOWN")

        # the raft (UpdateDock): from the dock of F6 ($55, column $80) up across the water to $45 and back
        for low, high, x in ((0x55, 0x45, 0x80), (0x3F, 0x2F, 0x60)):
            a = self.comp.get((low, (x, 0x7D)))
            b = self.comp.get((high, (x, Y1)))
            if a is not None and b is not None:
                self.edges[(low, a)].add(((high, b), "RAFT"))
                self.edges[(high, b)].add(((low, a), "RAFT"))

    def _link(self, room, c, other, node, d):
        oc = self.comp.get((other, node))
        if oc is not None:
            self.edges[(room, c)].add(((other, oc), d))

    def component(self, room: int, x: int, y: int):
        x, y = x & ~7, ((y - 5) & ~7) + 5
        c = self.comp.get((room, (x, y)))
        if c is None:      # off the grid or on a doorway: take the nearest free node of the screen
            best = min(((abs(n[0] - x) + abs(n[1] - y), cc) for (r, n), cc in self.comp.items() if r == room),
                       default=(0, 0))
            c = best[1]
        return (room, c)

    def parts_with(self, room: int, nodes) -> set:
        """The parts of the screen that hold any of the nodes."""
        return {self.comp[(room, n)] for n in nodes if (room, n) in self.comp}

    def route(self, room: int, x: int, y: int, target_room: int, avoid=(), allow=lambda room, d: True, parts=None):
        """List of (room, direction, (next room, part)) moves from Link's place to any part of target_room (or to one
        of the given parts of it)."""
        start = self.component(room, x, y)
        prev = {start: None}
        q = collections.deque([start])
        while q:
            n = q.popleft()
            if n[0] == target_room and (parts is None or n[1] in parts):
                out = []
                while prev[n] is not None:
                    m = n
                    n, d = prev[n]
                    out.append((n[0], d, m))
                return out[::-1]
            for m, d in self.edges[n]:
                if m in prev or m[0] in avoid or not allow(n[0], d):
                    continue
                prev[m] = (n, d)
                q.append(m)
        return None
