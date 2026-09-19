# telarchy-chess-reference-bot

A trading bot for futarchy chess on [Telarchy](https://telarchy.com), in one
readable file.

TelarchyBot plays chess on Lichess, and a market picks every move. On each of
its turns one Telarchy proposal goes up with one option per legal move. Each
option has its own market on the player's Lichess classical rating at a
half-hour mark 30 to 60 minutes on (market range 1200 to 2000; the feed names
the mark as `cell`). Two seconds before the deadline the option priced highest is played, and the
rest are voided and refunded. With nobody trading, every option has the same
price and the move is random. This bot asks Stockfish instead.

```
read the feed  ->  ask Stockfish about every legal move  ->  turn each score into a rating  ->  trade the gaps
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

With the player rated 1500 and every option priced 1500, a position where
Stockfish gives e4 a game score of 62 and a3 a score of 38 reads:

```
game 2 move 8: rating 1500, Stockfish likes e4 at 62.0 (rating 1501.9), 20 moves rated, 30s to decide
  a3 (a2a3): Stockfish 38.0, rating 1498.1, price 1500 -> would buy lower 6 cr (dry run, no key)
  e4 (e2e4): Stockfish 62.0, rating 1501.9, price 1500 -> would buy higher 6 cr (dry run, no key)
```

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
100 or 0. That becomes an expected game score E from TelarchyBot's side,
0 a loss, 50 a draw, 100 a win.

The books are priced in rating points, so the score becomes a rating forecast:

```
forecast = rating + 16 * (E / 100 - 0.5)
```

A rated game against an equal opponent is worth about 8 points up for a win,
8 down for a loss and nothing for a draw, so 16 (`GAME_SWING`) is the swing
between a loss and a win. The games played after this one before the mark add
noise to every option alike and are ignored. `rating` is `player.rating` from
the feed; when the feed has no numeric `player.rating` it is `call.value` (the
main book's price); when it has neither, the bot does not trade that read and
says why.

Then, per proposal:

- where the forecast for a move is more than 0.5 rating points above its
  price, buy higher;
- where the move that would be played right now is priced more than 0.5
  rating points above its forecast, buy lower;
- 5 credits per 1.6 rating points of gap (a tenth of a game's swing, which is
  10 points of game score), at most 20 on one option, at most 3 options per
  proposal, never the same option twice, never with 3 seconds or less to go;
- every trade carries `limit` at the forecast, so it never pushes the price
  past its own opinion. The limit always sits strictly inside the market's
  range as the feed gives it: clamped to `trade.rangeMin + 1` ..
  `trade.rangeMax - 1`. A feed without a numeric range is not traded.

Be honest about what that ignores:

- **The market also plays the rest of the game.** Stockfish scores a move as
  if both sides then play well. Every later move is picked by a market too, so
  when the market plays badly the real expected score is lower than
  Stockfish's.
- The opponent's strength and both clocks. The 16 point swing assumes an
  opponent of equal rating, and a provisional rating moves far more.
- Every game played between this one and the mark.
- That lifting the second best move can make it the one that gets played.
- How deep the book is, and that the chosen move's stake stays out until its
  mark settles.
- One second of thinking is not much. Stockfish on a slow machine at one
  second is a strong club player, not a perfect one.

It is a floor to beat. Replace `plan()` with something better and keep the
rest.

## Settings

All by environment, all optional:

- `CHESS_FEED`: the feed, default `https://chess.167-233-147-90.nip.io/state`
- `STOCKFISH`: the engine, default `stockfish` on PATH (then `/usr/games/stockfish`)
- `THINK_SECONDS`: think time per position, default `1.0`
- `MARGIN`: rating points a price must be off the forecast before trading, default `0.5`
- `STAKE`: credits per 1.6 rating points of gap, default `5`
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
- `player`: `rating`, the current classical rating, the anchor of every
  forecast
- `call`: `value`, the main book's price, the anchor when `player.rating` is
  missing
- `cell`: the half-hour mark the books are priced on, e.g. `2026-09-19T13:00`
- `trade`: `base`, `workspaceId`, and `rangeMin` / `rangeMax` (1200 / 2000),
  the range every limit must sit strictly inside

A trade is `POST {base}/predictions/trade` with `X-Agent-Key` and
`X-Workspace-Id`, body `{ marketId, direction, amount, limit }`.

## Tests

```bash
.venv/bin/python -m pytest -q
```

No network and no Stockfish: the engine is a fake and the HTTP goes to a
recording function or a local stub server. What is tested is what costs money
when it is wrong: how an engine score becomes a game score and then a rating
forecast, which options get traded, that a limit stays inside the range, that none is traded twice, that nothing trades too close to the
decision, and that a dry run places nothing.

## Licence

MIT.
