"""A trading bot for futarchy chess on Telarchy, in one file.

TelarchyBot plays chess on Lichess, and every one of its moves is a Telarchy
proposal: one option per legal move, each option its own market on the
player's Lichess classical rating at a half-hour mark 30 to 60 minutes on
(range 1200 to 2000). Two seconds before the deadline the option priced highest
is played. A price is the market's guess at the rating if that move is played.
A chess engine knows what a move does to this game, a game is worth about 16
rating points between a loss and a win, so an engine that disagrees with a
price has an edge.

    read the feed  ->  ask Stockfish about every legal move
                   ->  turn each score into a rating  ->  trade the gaps

That is the whole loop. Start here, replace `plan()` with your own opinion,
keep the rest.

Run it:

    python3 bot.py              # dry run: says what it would do, spends nothing
    python3 bot.py --live       # actually trades, needs TELARCHY_KEY
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    import chess
    import chess.engine
except ImportError:  # pragma: no cover
    raise SystemExit("python-chess is missing: pip install -r requirements.txt")

FEED_DEFAULT = "https://chess.167-233-147-90.nip.io/state"

# Where Debian and Ubuntu put Stockfish (`apt install stockfish`), which is not
# on every PATH. Only looked at when STOCKFISH is left at its default name.
STOCKFISH_FALLBACKS = ["/usr/games/stockfish"]

# No trade with this many seconds or fewer left before the decision. A trade
# that lands after the operator has read the prices changes nothing and still
# costs the spread.
MIN_SECONDS = 3.0

# Rating points between losing and winning one rated game against an opponent
# of about the same rating: 8 up for a win, 8 down for a loss, 0 for a draw.
GAME_SWING = 16.0


@dataclass
class Config:
    feed: str = FEED_DEFAULT
    stockfish: str = "stockfish"
    think: float = 1.0        # seconds Stockfish gets per position
    margin: float = 0.5       # rating points a price must be off the forecast before trading
    stake: float = 5.0        # credits per 1.6 rating points of gap (10 points of game score)
    max_stake: float = 20.0   # never more than this on one option
    max_trades: int = 3       # never more than this many options per proposal
    buy_lower: bool = True    # also bet against an overpriced leading move
    poll: float = 2.0         # seconds between feed reads

    @classmethod
    def from_env(cls, env) -> "Config":
        d = cls()
        return cls(
            feed=env.get("CHESS_FEED") or d.feed,
            stockfish=env.get("STOCKFISH") or d.stockfish,
            think=float(env.get("THINK_SECONDS") or d.think),
            margin=float(env.get("MARGIN") or d.margin),
            stake=float(env.get("STAKE") or d.stake),
            max_stake=float(env.get("MAX_STAKE") or d.max_stake),
            max_trades=int(env.get("MAX_TRADES") or d.max_trades),
            buy_lower=str(env.get("BUY_LOWER", "1")).strip().lower() not in ("0", "false", "no", "off"),
            poll=float(env.get("POLL_SECONDS") or d.poll),
        )


@dataclass
class Trade:
    option: str      # the move in UCI, which is the option id
    san: str
    market_id: str
    direction: str   # "higher" or "lower"
    amount: float    # credits
    expected: float  # the rating forecast if this move is played
    price: float     # what the market thinks that rating is
    score: float | None = None  # the engine's game score behind it, 0..100


# ------------------------------------------------ an engine opinion as a score


def cp_to_expected(cp: float) -> float:
    """Centipawns to an expected score, 0..100, when the engine gives no WDL.

    The logistic Lichess uses for its win percentage. It was fitted on human
    games, not on this floor, so treat it as a shape rather than a truth.
    """
    cp = max(-10_000.0, min(10_000.0, float(cp)))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def expected_score(score: chess.engine.PovScore, color: chess.Color,
                   wdl: chess.engine.PovWdl | None = None) -> float:
    """TelarchyBot's expected Game score from one engine line.

    Always from TelarchyBot's side (`color`), whoever is to move. A mate is
    100 or 0 whatever the WDL says. Otherwise the engine's own win/draw/loss
    estimate when it gives one, and the centipawn logistic when it does not.
    """
    s = score.pov(color)
    if s.is_mate():
        return 100.0 if s > chess.engine.Cp(0) else 0.0
    if wdl is not None:
        return 100.0 * wdl.pov(color).expectation()
    return cp_to_expected(s.score())


def evaluate(engine, fen: str, color: chess.Color, think: float) -> dict[str, float]:
    """Every legal move of the position with its expected score, keyed by UCI.

    One search with MultiPV set to the number of legal moves, so every move
    gets a line of its own from the same think time.
    """
    board = chess.Board(fen)
    n = board.legal_moves.count()
    if n == 0:
        return {}
    infos = engine.analyse(board, chess.engine.Limit(time=think), multipv=n)
    if isinstance(infos, dict):
        infos = [infos]
    out: dict[str, float] = {}
    for info in infos:
        pv, score = info.get("pv"), info.get("score")
        if not pv or score is None:
            continue
        out[pv[0].uci()] = expected_score(score, color, info.get("wdl"))
    return out


# ------------------------------------------------- a game score as a rating


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def anchor(state: dict) -> float | None:
    """The rating every forecast starts from.

    `player.rating` from the feed; without a numeric one, the main book's
    price (`call.value`); without either, None, and the bot does not trade.
    """
    for block, key in (("player", "rating"), ("call", "value")):
        b = state.get(block)
        if isinstance(b, dict) and _number(b.get(key)):
            return float(b[key])
    return None


def forecast(expected: float, rating: float) -> float:
    """The rating at the mark if a move with game score `expected` is played.

    forecast = rating + GAME_SWING * (E / 100 - 0.5). The games played after
    this one before the mark add noise to every option alike, so they are
    left out.
    """
    return rating + GAME_SWING * (expected / 100.0 - 0.5)


def forecasts(evals: dict[str, float], rating: float) -> dict[str, float]:
    return {uci: forecast(e, rating) for uci, e in evals.items()}


def limit_for(value: float, trade: dict) -> float | None:
    """A limit price strictly inside the market's range, as the feed gives it.

    Clamped to rangeMin + 1 .. rangeMax - 1. None when the feed has no usable
    range: a limit that might sit outside the book is not sent.
    """
    lo, hi = trade.get("rangeMin"), trade.get("rangeMax")
    if not (_number(lo) and _number(hi)) or lo + 1 > hi - 1:
        return None
    return max(lo + 1, min(hi - 1, round(value, 2)))


# ---------------------------------------------------------------- the opinion


def stake_for(gap: float, cfg: Config) -> float:
    """Credits for a gap in rating points, capped at `max_stake`.

    `stake` per tenth of a game's swing (1.6 rating points, which is 10 points
    of game score). Small on purpose. An option book holds 100 credits, and on a thin book the
    price you pay is the average across the move you make.
    """
    return round(min(cfg.max_stake, cfg.stake * gap / (GAME_SWING / 10.0)), 2)


def plan(open_: dict, evals: dict[str, float], traded: set[str], cfg: Config,
         scores: dict[str, float] | None = None) -> list[Trade]:
    """Which options to trade now, in order.

    `evals` are rating forecasts keyed by UCI (see `forecast()`), in the same
    unit as the prices; `scores` are the game scores behind them, for the log.

    THE STRATEGY, and it is the simplest defensible one: Stockfish is a better
    chess player than an empty book, so where its forecast for a move is more
    than `margin` above the price, buy higher; where the move that would be
    played right now is priced more than `margin` above its forecast, buy lower.

    Lower first (it is the trade that stops a bad move being played), then
    higher from Stockfish's favourite down, at most `max_trades` options per
    proposal counting the ones already traded, never the same option twice.

    Be honest about what this ignores. Stockfish scores a move as if both
    sides then play well, but every later TelarchyBot move is also picked by a
    market, so the true expected score is lower whenever the market plays
    badly. It ignores the opponent's strength, both clocks, what your own
    trades do to the price beyond the limit, and that lifting the second best
    move can make it the one that gets played. It is a floor to beat.
    """
    room = cfg.max_trades - len(traded)
    if room <= 0:
        return []

    def priced(o):
        p = o.get("price")
        return isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p)

    options = [o for o in (open_.get("options") or []) if isinstance(o, dict) and priced(o)]
    top = max((o["price"] for o in options), default=None)
    usable = [
        o for o in options
        if o.get("marketId") and o.get("id") in evals and o["id"] not in traded
    ]

    def trade(o, direction, gap):
        return Trade(
            option=o["id"], san=o.get("san") or o["id"], market_id=o["marketId"],
            direction=direction, amount=stake_for(gap, cfg),
            expected=evals[o["id"]], price=float(o["price"]),
            score=(scores or {}).get(o["id"]),
        )

    lower = []
    if cfg.buy_lower and top is not None:
        for o in usable:
            leading = top - o["price"] <= 1e-9  # the operator's own tie tolerance
            gap = o["price"] - evals[o["id"]]
            if leading and gap > cfg.margin:
                lower.append(trade(o, "lower", gap))

    higher = []
    for o in sorted(usable, key=lambda o: -evals[o["id"]]):
        gap = evals[o["id"]] - o["price"]
        if gap > cfg.margin:
            higher.append(trade(o, "higher", gap))

    return (lower + higher)[:room]


# ------------------------------------------------------------------- the wire


def _json(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    try:
        value = json.loads(text)
    except ValueError:
        return {"error": text[:200]}
    return value if isinstance(value, dict) else {"value": value}


def http_post(url: str, headers: dict, body: dict, timeout: float = 20.0) -> tuple[int, dict]:
    """POST JSON. A 4xx or 5xx comes back as (status, body), never raised."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())


def fetch_feed(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "telarchy-chess-reference-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _n(x) -> str:
    return f"{x:.6g}" if isinstance(x, (int, float)) and not isinstance(x, bool) else "?"


# -------------------------------------------------------------------- the bot


class Bot:
    """Remembers one thing per proposal: which options it already traded."""

    def __init__(self, cfg: Config, engine, key: str | None = None, live: bool = False,
                 post=None, clock=None, out=None):
        self.cfg = cfg
        self.engine = engine
        self.key = key
        self.live = live
        self.post = post or http_post
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.out = out or (lambda line: print(line, flush=True))
        self.proposal: str | None = None
        self.traded: set[str] = set()
        self.fen: str | None = None
        self.evals: dict[str, float] = {}
        self.warned: tuple | None = None

    def seconds_left(self, open_: dict) -> float | None:
        at = parse_time(open_.get("decideAt"))
        return None if at is None else (at - self.clock()).total_seconds()

    def cycle(self, state: dict) -> None:
        """One feed read's worth of work."""
        if not isinstance(state, dict) or state.get("phase") != "our-move":
            return
        open_, game, trade = state.get("open"), state.get("game"), state.get("trade")
        if not (isinstance(open_, dict) and isinstance(game, dict) and isinstance(trade, dict)):
            return
        if not open_.get("tradeable"):
            return
        proposal = (open_.get("proposal") or {}).get("id")
        fen, color = game.get("fen"), game.get("color")
        if not proposal or not fen or color not in ("white", "black"):
            return
        if not trade.get("base") or not trade.get("workspaceId"):
            return
        left = self.seconds_left(open_)
        if left is None or left <= MIN_SECONDS:
            return

        if proposal != self.proposal:
            self.proposal, self.traded = proposal, set()

        rating = anchor(state)
        why = None
        if rating is None:
            why = "no rating on the feed (neither player.rating nor call.value)"
        elif limit_for(rating, trade) is None:
            why = "no range on the feed (trade.rangeMin, trade.rangeMax)"
        if why:
            if self.warned != (proposal, why):
                self.warned = (proposal, why)
                self.out(f"game {game.get('number')} move {open_.get('move')}: not trading, {why}")
            return

        if fen != self.fen:
            side = chess.WHITE if color == "white" else chess.BLACK
            self.evals = evaluate(self.engine, fen, side, self.cfg.think)
            self.fen = fen
            sans = {o.get("id"): o.get("san") for o in open_.get("options") or [] if isinstance(o, dict)}
            if self.evals:
                best = max(self.evals, key=self.evals.get)
                self.out(
                    f"game {game.get('number')} move {open_.get('move')}: rating {rating:g}, "
                    f"Stockfish likes {sans.get(best) or best} at {self.evals[best]:.1f} "
                    f"(rating {forecast(self.evals[best], rating):.1f}), "
                    f"{len(self.evals)} moves rated, {left:.0f}s to decide"
                )

        # Thinking takes time; the window may have closed meanwhile.
        left = self.seconds_left(open_)
        if left is None or left <= MIN_SECONDS:
            return

        for t in plan(open_, forecasts(self.evals, rating), self.traded, self.cfg, self.evals):
            self.traded.add(t.option)  # an attempt counts: never the same option twice
            self.out(self.place(t, proposal, trade))

    def place(self, t: Trade, proposal: str, trade: dict) -> str:
        engine = f"Stockfish {t.score:.1f}, " if t.score is not None else ""
        where = f"  {t.san} ({t.option}): {engine}rating {t.expected:.1f}, price {_n(t.price)}"
        what = f"buy {t.direction} {t.amount:g} cr"
        if not self.key:
            return f"{where} -> would {what} (dry run, no key)"

        url = str(trade["base"]).rstrip("/") + "/predictions/trade"
        headers = {
            "X-Agent-Key": self.key,
            "X-Workspace-Id": str(trade["workspaceId"]),
            "Content-Type": "application/json",
            # A retry of the same trade returns the first result instead of
            # buying twice.
            "Idempotency-Key": f"chess-{proposal}-{t.option}-{t.direction}",
        }
        body = {
            "marketId": t.market_id,
            "direction": t.direction,
            "amount": t.amount,
            # Never push the price past what the engine thinks: the trade
            # fills only as far as this and keeps the rest of the credits.
            # Strictly inside the feed's range.
            "limit": limit_for(t.expected, trade),
        }
        if not self.live:
            body["dryRun"] = True  # a quote: same transaction, rolled back

        try:
            status, answer = self.post(url, headers, body)
        except Exception as e:  # network trouble must not stop the loop
            return f"{where} -> {what} failed: {e}"
        if status >= 400:
            why = answer.get("code") or answer.get("error") or ""
            return f"{where} -> {what} refused: {status} {why}".rstrip()
        fill = f"{_n(answer.get('shares'))} shares for {_n(answer.get('cost'))} cr, price {_n(answer.get('consensus'))}"
        if not self.live:
            return f"{where} -> would {what}: quote {fill}"
        return f"{where} -> traded, {what}: {fill}"


# ------------------------------------------------------------------- the loop


def find_stockfish(path: str) -> str | None:
    if os.sep in path:
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    found = shutil.which(path)
    if found:
        return found
    if path == Config.stockfish:
        for candidate in STOCKFISH_FALLBACKS:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def main(argv: list[str] | None = None, env=None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="actually trade (default: dry run)")
    ap.add_argument("--once", action="store_true", help="read the feed once and exit")
    args = ap.parse_args(argv)
    cfg = Config.from_env(env)

    key = env.get("TELARCHY_KEY") or None
    if args.live and not key:
        print("--live needs TELARCHY_KEY. A dry run works without one.", file=sys.stderr)
        return 2

    path = find_stockfish(cfg.stockfish)
    if not path:
        print(
            f"Stockfish not found (STOCKFISH={cfg.stockfish}). This bot does not trade without an engine.\n"
            "Install it (apt install stockfish, brew install stockfish, or https://stockfishchess.org/download/)\n"
            "and set STOCKFISH to its path if it is not on PATH.",
            file=sys.stderr,
        )
        return 2

    engine = chess.engine.SimpleEngine.popen_uci(path)
    if "UCI_ShowWDL" in engine.options:
        engine.configure({"UCI_ShowWDL": True})

    mode = "trading" if args.live else ("dry run with quotes" if key else "dry run")
    print(f"{mode}: {cfg.feed}, {path}, think {cfg.think:g}s, margin {cfg.margin:g}, "
          f"stake {cfg.stake:g}/1.6 rating points up to {cfg.max_stake:g}, {cfg.max_trades} options per move", flush=True)

    bot = Bot(cfg, engine, key=key, live=args.live)
    last_phase = None
    try:
        while True:
            try:
                state = fetch_feed(cfg.feed)
            except Exception as e:
                print(f"feed unreadable: {e}", flush=True)
                state = None
            if isinstance(state, dict):
                if state.get("phase") != last_phase:
                    last_phase = state.get("phase")
                    print(f"phase: {last_phase}", flush=True)
                bot.cycle(state)
            if args.once:
                break
            time.sleep(cfg.poll)
    except KeyboardInterrupt:
        pass
    except chess.engine.EngineError as e:
        print(f"Stockfish failed: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            engine.quit()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
