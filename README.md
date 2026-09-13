# telarchy-chess-reference-bot

A trading bot for futarchy chess on [Telarchy](https://telarchy.com), in one
readable file.

TelarchyBot plays chess on Lichess, and a market picks every move. On each of
its turns one Telarchy proposal goes up with one option per legal move. Each
option has its own market on the game's score: 100 a win, 50 a draw, 0 a loss.
Two seconds before the deadline the option priced highest is played, and the
rest are voided and refunded. With nobody trading, every option has the same
price and the move is random. This bot asks Stockfish instead.

```
read the feed  ->  ask Stockfish about every legal move  ->  trade the gaps
```

That is the whole loop. [`bot.py`](bot.py) is one file, mostly comments.

## Try it

```bash
sudo apt-get install stockfish        # or: brew install stockfish
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

No account, no key, no credits. It reads the live feed and, when TelarchyBot
is to move, says what it would trade. When the game is between moves it waits.

On game 2, move 8 (every one of the 30 options priced 0.1, White already
worse after the market's random moves), it said:

```
game 2 move 8: Stockfish likes e3 at 25.7, 30 moves rated, 30s to decide
  e3 (e2e3): Stockfish 25.7, price 0.1 -> would buy higher 12.8 cr (dry run, no key)
  Nc3 (b1c3): Stockfish 11.2, price 0.1 -> would buy higher 5.53 cr (dry run, no key)
  h3 (h2h3): Stockfish 10.9, price 0.1 -> would buy higher 5.42 cr (dry run, no key)
```

The market played Kd1, which Stockfish scores at 0.

Without Stockfish it stops and says so. It never falls back to guessing.

## Then with a key

```bash
export TELARCHY_KEY=...
.venv/bin/python bot.py           # asks Telarchy for a quote on each trade, spends nothing
.venv/bin/python bot.py --live    # actually trades
```

With a key and no `--live`, each trade is sent as a dry run (`dryRun: true`),
so you see the fill you would really get. **`--live` is the only thing that
spends credits.**

How to get an agent key is in the `authentication` section of
[`GET /api/help`](https://telarchy.com/api/help).

The feed says where to trade (`trade.base`). While the floor runs on
Telarchy's beta store, that store refuses agent keys, so quotes and trades
come back refused until it moves to production. The dry run without a key
works either way.

## The strategy, and what it ignores

For each legal move Stockfish gives a win, draw and loss estimate (or a
centipawn score, turned into one with the logistic Lichess uses). A mate is
100 or 0. That becomes an expected score from TelarchyBot's side.

Then, per proposal:

- where Stockfish's score for a move is more than 5 points above its price,
  buy higher;
- where the move that would be played right now is priced more than 5 points
  above Stockfish's score, buy lower;
- 5 credits per 10 points of gap, at most 20 on one option, at most 3 options
  per proposal, never the same option twice, never with 3 seconds or less to
  go;
- every trade carries `limit` at Stockfish's score, so it never pushes the
  price past its own opinion.

Be honest about what that ignores:

- **The market also plays the rest of the game.** Stockfish scores a move as
  if both sides then play well. Every later move is picked by a market too, so
  when the market plays badly the real expected score is lower than
  Stockfish's.
- The opponent's strength and both clocks.
- That lifting the second best move can make it the one that gets played.
- How deep the book is (100 credits per option), and that the chosen move's
  stake stays out until the game ends.
- One second of thinking is not much. Stockfish on a slow machine at one
  second is a strong club player, not a perfect one.

It is a floor to beat. Replace `plan()` with something better and keep the
rest.

## Settings

All by environment, all optional:

- `CHESS_FEED`: the feed, default `https://chess.167-233-147-90.nip.io/state`
- `STOCKFISH`: the engine, default `stockfish` on PATH (then `/usr/games/stockfish`)
- `THINK_SECONDS`: think time per position, default `1.0`
- `MARGIN`: score points a price must be off before trading, default `5`
- `STAKE`: credits per 10 points of gap, default `5`
- `MAX_STAKE`: most credits on one option, default `20`
- `MAX_TRADES`: most options traded per proposal, default `3`
- `BUY_LOWER`: `0` to never bet against the leading move, default on
- `POLL_SECONDS`: seconds between feed reads, default `2`
- `TELARCHY_KEY`: your agent key; without it nothing is sent to Telarchy

`--once` reads the feed once and exits.

## The feed

`GET /state` is public JSON. The parts the bot reads:

- `phase`: `our-move` while a proposal is open
- `game`: `color` (TelarchyBot's side) and `fen`
- `open`: `proposal.id`, `decideAt`, `tradeable`, and `options`, each with
  `id` (the move in UCI), `san`, `price` and `marketId`; a missing price is
  `null`
- `trade`: `base` and `workspaceId`

A trade is `POST {base}/predictions/trade` with `X-Agent-Key` and
`X-Workspace-Id`, body `{ marketId, direction, amount, limit }`.

## Tests

```bash
.venv/bin/python -m pytest -q
```

No network and no Stockfish: the engine is a fake and the HTTP goes to a
recording function or a local stub server. What is tested is what costs money
when it is wrong: how an engine score becomes a Game score, which options get
traded, that none is traded twice, that nothing trades too close to the
decision, and that a dry run places nothing.

## Licence

MIT.
