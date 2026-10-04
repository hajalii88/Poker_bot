"""موتور بازی پوکر Texas Hold'em (بدون وابستگی به تلگرام)."""
import asyncio
import itertools
import random
from collections import Counter

SUITS = "shdc"
SUIT_SYM = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
RANK_CH = {11: "J", 12: "Q", 13: "K", 14: "A"}
STAGES = ["preflop", "flop", "turn", "river", "showdown"]


def rank_str(r):
    return RANK_CH.get(r, str(r))


def card_str(c):
    return f"{rank_str(c[0])}{SUIT_SYM[c[1]]}"


def new_deck():
    return [(r, s) for r in range(2, 15) for s in SUITS]


def rank5(cards):
    ranks = sorted((c[0] for c in cards), reverse=True)
    flush = len({c[1] for c in cards}) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            straight_high = 5
    groups = sorted(Counter(ranks).items(), key=lambda x: (x[1], x[0]), reverse=True)
    shape = [g[1] for g in groups]
    order = [g[0] for g in groups]
    if straight_high and flush:
        return (8, straight_high)
    if shape == [4, 1]:
        return (7, *order)
    if shape == [3, 2]:
        return (6, *order)
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if shape == [3, 1, 1]:
        return (3, *order)
    if shape == [2, 2, 1]:
        return (2, *order)
    if shape == [2, 1, 1, 1]:
        return (1, *order)
    return (0, *ranks)


def best_hand(cards):
    return max(rank5(c) for c in itertools.combinations(cards, 5))


class Player:
    def __init__(self, uid, name, chips):
        self.uid = uid
        self.name = name
        self.chips = chips
        self.rank = None
        self.reset()

    def reset(self):
        self.hole = []
        self.bet = 0
        self.total = 0
        self.folded = False
        self.allin = False
        self.acted = False
        self.leaving = False
        self.rank = None


class Table:
    SB = 10
    BB = 20

    def __init__(self, chat_id, owner_id, start_chips=1000):
        self.chat_id = chat_id
        self.owner_id = owner_id
        self.start_chips = start_chips
        self.players = []
        self.queue = []  # کسانی که وسط دست آمدند
        self.state = "lobby"  # lobby / hand / between
        self.dealer = -1
        self.hand_no = 0
        self.deck = []
        self.board = []
        self.stage = "preflop"
        self.current_bet = 0
        self.min_raise = self.BB
        self.to_act = None
        self.result = None
        # تنظیمات تلگرام
        self.image_mode = True
        self.msg_id = None
        self.lobby_msg_id = None
        self.job = None
        self.lock = asyncio.Lock()

    # ---------- بازیکن‌ها ----------
    def find(self, uid):
        for p in self.players + self.queue:
            if p.uid == uid:
                return p
        return None

    def add_player(self, uid, name):
        if self.find(uid):
            return "exists"
        if len(self.players) + len(self.queue) >= 9:
            return "full"
        p = Player(uid, name, self.start_chips)
        if self.state == "hand":
            self.queue.append(p)
            return "queued"
        self.players.append(p)
        return "ok"

    def leave(self, uid):
        p = self.find(uid)
        if not p:
            return False
        if p in self.queue:
            self.queue.remove(p)
            return True
        if self.state != "hand":
            self.players.remove(p)
            return True
        p.leaving = True
        if not p.folded:
            p.folded = True
            p.acted = True
            self._progress()
        return True

    def pot(self):
        return sum(p.total for p in self.players)

    # ---------- شروع دست ----------
    def start_hand(self):
        self.players = [p for p in self.players if p.chips > 0 and not p.leaving]
        self.players += self.queue
        self.queue = []
        if len(self.players) < 2:
            self.state = "lobby"
            return False
        for p in self.players:
            p.reset()
        n = len(self.players)
        self.hand_no += 1
        self.state = "hand"
        self.result = None
        self.dealer = (self.dealer + 1) % n
        self.deck = new_deck()
        random.shuffle(self.deck)
        self.board = []
        self.stage = "preflop"
        if n == 2:
            sb, bb = self.dealer, (self.dealer + 1) % n
        else:
            sb, bb = (self.dealer + 1) % n, (self.dealer + 2) % n
        self._post(sb, self.SB)
        self._post(bb, self.BB)
        self.current_bet = self.BB
        self.min_raise = self.BB
        for p in self.players:
            p.hole = [self.deck.pop(), self.deck.pop()]
        self.to_act = sb if n == 2 else (bb + 1) % n
        self._progress()
        return True

    def _post(self, idx, amt):
        p = self.players[idx]
        a = min(amt, p.chips)
        p.chips -= a
        p.bet += a
        p.total += a
        if p.chips == 0:
            p.allin = True
        return a

    # ---------- منطق دور شرط‌بندی ----------
    def _needs(self, p):
        return (not p.folded and not p.allin
                and (not p.acted or p.bet < self.current_bet))

    def _next_actor(self, start):
        n = len(self.players)
        for i in range(1, n + 1):
            idx = (start + i) % n
            if self._needs(self.players[idx]):
                return idx
        return None

    def _progress(self):
        while True:
            live = [p for p in self.players if not p.folded]
            if len(live) == 1:
                self._win_by_fold(live[0])
                return
            cur = self.players[self.to_act] if self.to_act is not None else None
            if cur is None or not self._needs(cur):
                start = self.to_act if self.to_act is not None else self.dealer
                nxt = self._next_actor(start)
                if nxt is None:
                    if not self._advance():
                        return
                    continue
                self.to_act = nxt
            return

    def _advance(self):
        """رفتن به دور بعد. اگر دست تمام شد False برمی‌گرداند."""
        while True:
            for p in self.players:
                p.bet = 0
                p.acted = False
            self.current_bet = 0
            self.min_raise = self.BB
            self.stage = STAGES[STAGES.index(self.stage) + 1]
            if self.stage == "flop":
                self.board += [self.deck.pop() for _ in range(3)]
            elif self.stage in ("turn", "river"):
                self.board.append(self.deck.pop())
            else:
                self._showdown()
                return False
            active = [p for p in self.players if not p.folded and not p.allin]
            if len(active) >= 2:
                n = len(self.players)
                for k in range(1, n + 1):
                    idx = (self.dealer + k) % n
                    q = self.players[idx]
                    if not q.folded and not q.allin:
                        self.to_act = idx
                        break
                return True

    # ---------- پایان دست ----------
    def _win_by_fold(self, winner):
        amt = self.pot()
        winner.chips += amt
        self.state = "between"
        self.to_act = None
        self.result = {"fold": True, "won": [(winner, amt)], "reveal": []}

    def _showdown(self):
        live = [p for p in self.players if not p.folded]
        for p in live:
            p.rank = best_hand(p.hole + self.board)
        won = {}
        levels = sorted({p.total for p in self.players if p.total > 0})
        prev = 0
        for L in levels:
            amt = sum(min(p.total, L) - min(p.total, prev) for p in self.players)
            elig = [p for p in live if p.total >= L] or live
            best = max(p.rank for p in elig)
            winners = [p for p in elig if p.rank == best]
            share, rem = divmod(amt, len(winners))
            for k, w in enumerate(winners):
                got = share + (1 if k < rem else 0)
                w.chips += got
                won[w.uid] = (w, won.get(w.uid, (w, 0))[1] + got)
            prev = L
        self.state = "between"
        self.to_act = None
        self.result = {
            "fold": False,
            "won": list(won.values()),
            "reveal": [(p, p.hole, p.rank[0]) for p in live],
        }

    # ---------- اکشن‌ها ----------
    def current_player(self):
        if self.state == "hand" and self.to_act is not None:
            return self.players[self.to_act]
        return None

    def can_act(self, uid):
        p = self.current_player()
        return p is not None and p.uid == uid

    def call_amount(self, p=None):
        p = p or self.current_player()
        return min(self.current_bet - p.bet, p.chips)

    def fold(self):
        p = self.current_player()
        p.folded = True
        p.acted = True
        self._progress()

    def call(self):
        p = self.current_player()
        self._post(self.to_act, self.call_amount(p))
        p.acted = True
        self._progress()

    def raise_options(self):
        """لیست (برچسب، مقدار) گزینه‌های ریز برای بازیکن فعلی."""
        p = self.current_player()
        max_to = p.bet + p.chips
        if max_to <= self.current_bet:
            return []
        min_to = self.current_bet + self.min_raise
        opts = []
        if min_to < max_to:
            pot = self.pot()
            need = self.current_bet - p.bet
            cands = [("min", min_to),
                     ("half", self.current_bet + (pot + need) // 2),
                     ("pot", self.current_bet + pot + need)]
            seen = set()
            for label, v in cands:
                v = max(min_to, min(v, max_to - 1))
                if v not in seen and v < max_to:
                    seen.add(v)
                    opts.append((label, v))
        opts.append(("allin", max_to))
        return opts

    def raise_to(self, total):
        p = self.current_player()
        max_to = p.bet + p.chips
        min_to = self.current_bet + self.min_raise
        total = min(total, max_to)
        if total < min_to and total < max_to:
            total = min(min_to, max_to)
        if total <= self.current_bet:
            return self.call()
        self._post(self.to_act, total - p.bet)
        size = total - self.current_bet
        full = size >= self.min_raise
        if full:
            self.min_raise = size
        self.current_bet = total
        if full:
            for q in self.players:
                if q is not p and not q.folded and not q.allin:
                    q.acted = False
        p.acted = True
        self._progress()
