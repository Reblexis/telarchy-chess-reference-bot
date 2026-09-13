"""A trading bot for futarchy chess on Telarchy, in one file.

TelarchyBot plays chess on Lichess, and every one of its moves is a Telarchy
proposal: one option per legal move, each option its own market on the game's
score (100 a win, 50 a draw, 0 a loss). Two seconds before the deadline the
option priced highest is played. A price is the market's guess at the score if
that move is played, so a chess engine that disagrees with a price has an edge.

    read the feed  ->  ask Stockfish about every legal move  ->  trade the gaps

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


@dataclass
class Config:
    feed: str = FEED_DEFAULT
    stockfish: str = "stockfish"
    think: float = 1.0        # seconds Stockfish gets per position
    margin: float = 5.0       # score points a price must be off before trading
    stake: float = 5.0        # credits per 10 points of gap
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
    expected: float  # what the engine thinks the score is, 0..100
    price: float     # what the market thinks it is


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


# ---------------------------------------------------------------- the opinion


def stake_for(gap: float, cfg: Config) -> float:
    """Credits for a gap: `stake` per 10 points, capped at `max_stake`.

    Small on purpose. An option book holds 100 credits, and on a thin book the
    price you pay is the average across the move you make.
    """
    return round(min(cfg.max_stake, cfg.stake * gap / 10.0), 2)


def plan(open_: dict, evals: dict[str, float], traded: set[str], cfg: Config) -> list[Trade]:
    """Which options to trade now, in order.

    THE STRATEGY, and it is the simplest defensible one: Stockfish is a better
    chess player than an empty book, so where its score for a move is more than
    `margin` above the price, buy higher; where the move that would be played
    right now is priced more than `margin` above Stockfish's score, buy lower.

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
    return f"{x:.4g}" if isinstance(x, (int, float)) and not isinstance(x, bool) else "?"


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

        if fen != self.fen:
            side = chess.WHITE if color == "white" else chess.BLACK
            self.evals = evaluate(self.engine, fen, side, self.cfg.think)
            self.fen = fen
            sans = {o.get("id"): o.get("san") for o in open_.get("options") or [] if isinstance(o, dict)}
            if self.evals:
                best = max(self.evals, key=self.evals.get)
                self.out(
                    f"game {game.get('number')} move {open_.get('move')}: "
                    f"Stockfish likes {sans.get(best) or best} at {self.evals[best]:.1f}, "
                    f"{len(self.evals)} moves rated, {left:.0f}s to decide"
                )

        # Thinking takes time; the window may have closed meanwhile.
        left = self.seconds_left(open_)
        if left is None or left <= MIN_SECONDS:
            return

        for t in plan(open_, self.evals, self.traded, self.cfg):
            self.traded.add(t.option)  # an attempt counts: never the same option twice
            self.out(self.place(t, proposal, trade))

    def place(self, t: Trade, proposal: str, trade: dict) -> str:
        where = f"  {t.san} ({t.option}): Stockfish {t.expected:.1f}, price {_n(t.price)}"
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
            "limit": round(t.expected, 2),
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
          f"stake {cfg.stake:g}/10 points up to {cfg.max_stake:g}, {cfg.max_trades} options per move", flush=True)

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
