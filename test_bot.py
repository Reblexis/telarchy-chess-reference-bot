"""Tests for the futarchy chess reference bot. No network, no Stockfish.

    .venv/bin/python -m pytest -q

The engine is a fake that answers like python-chess does, and the HTTP is
either a recording function or a local stub server. What is worth testing in a
bot this small is what costs money when it is wrong: how an engine opinion
becomes a Game score, WHICH options it trades, that it never trades one twice,
that it stays out when there is no time, and that a dry run places nothing.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import chess
import chess.engine as ce
import pytest

import bot

NOW = datetime(2026, 9, 13, 17, 31, 0, tzinfo=timezone.utc)
START = chess.STARTING_FEN
WORKSPACE = "097462d2-f701-4a62-8a2f-effd78df22f5"
BASE = "https://telarchy.com/api"


# ---------------------------------------------------------------- fixtures


def iso(dt: datetime) -> str:
    """The feed's timestamp shape: `2026-09-13T17:31:46.188Z`."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def opt(uci, price, market="default", san=None, reason=None):
    o = {
        "id": uci,
        "san": san or uci,
        "price": price,
        "lead": 0 if price is not None else None,
        "marketId": f"m-{uci}" if market == "default" else market,
    }
    if reason:
        o["reason"] = reason
    return o


def feed(options, *, phase="our-move", color="white", fen=START, tradeable=True,
         decide_in=30.0, proposal="p1", base=BASE):
    """A /state document shaped like the live feed."""
    decide_at = NOW + timedelta(seconds=decide_in)
    return {
        "schema": 1,
        "phase": phase,
        "game": {
            "number": 2, "id": "QmMVYc0T", "url": "https://lichess.org/QmMVYc0T",
            "color": color, "fen": fen, "moves": [],
            "clocks": {"white": 1_600_000, "black": 1_600_000},
        },
        "open": {
            "move": 1,
            "proposal": {"id": proposal, "number": 19, "url": "https://telarchy.com/chess/p/19"},
            "openedAt": iso(NOW - timedelta(seconds=10)),
            "decideAt": iso(decide_at),
            "deadline": iso(decide_at + timedelta(seconds=2)),
            "tradeable": tradeable,
            "quotesAt": iso(NOW),
            "options": options,
        },
        "trade": {
            "base": base, "endpoint": "POST /api/predictions/trade", "auth": "X-Agent-Key",
            "workspaceHeader": "X-Workspace-Id", "workspaceId": WORKSPACE,
            "rangeMin": 0, "rangeMax": 100,
        },
    }


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeEngine:
    """Answers analyse() the way python-chess does with multipv: one info per line.

    `lines` is a list of (uci or None, PovScore, PovWdl or None). Thinking can
    cost clock time, to test what happens when the window closes meanwhile.
    """

    def __init__(self, lines, clock=None, think_cost=0.0):
        self.lines = lines
        self.clock = clock
        self.think_cost = think_cost
        self.calls = []

    def analyse(self, board, limit, multipv=None):
        self.calls.append({"fen": board.fen(), "time": limit.time, "multipv": multipv})
        if self.clock is not None:
            self.clock.advance(self.think_cost)
        out = []
        for uci, score, wdl in self.lines:
            info = {"score": score, "multipv": len(out) + 1}
            if uci is not None:
                info["pv"] = [chess.Move.from_uci(uci)]
            if wdl is not None:
                info["wdl"] = wdl
            out.append(info)
        return out


class FakePost:
    def __init__(self, status=201, body=None, raises=None):
        self.calls = []
        self.status = status
        self.body = body if body is not None else {"shares": 2.5, "cost": 3.0, "consensus": 12.0, "tradeId": "t1"}
        self.raises = raises

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "headers": dict(headers), "body": dict(body)})
        if self.raises:
            raise self.raises
        return self.status, dict(self.body)


def white(cp):
    return ce.PovScore(ce.Cp(cp), chess.WHITE)


def rated(uci, pct, color=chess.WHITE):
    """An engine line whose WDL says exactly `pct` for `color`."""
    wins = round(pct * 10)
    return (uci, ce.PovScore(ce.Cp(0), color), ce.PovWdl(ce.Wdl(wins, 0, 1000 - wins), color))


def make_bot(lines, *, key=None, live=False, post=None, think_cost=0.0, **cfg):
    clock = Clock()
    engine = FakeEngine(lines, clock, think_cost)
    post = post if post is not None else FakePost()
    printed: list[str] = []
    b = bot.Bot(bot.Config(**cfg), engine, key=key, live=live, post=post, clock=clock, out=printed.append)
    return b, engine, post, printed


# ------------------------------------- engine opinion -> expected Game score


def test_a_certain_win_by_wdl_is_100():
    wdl = ce.PovWdl(ce.Wdl(1000, 0, 0), chess.WHITE)
    assert bot.expected_score(white(0), chess.WHITE, wdl=wdl) == pytest.approx(100)


def test_a_certain_draw_by_wdl_is_50():
    wdl = ce.PovWdl(ce.Wdl(0, 1000, 0), chess.WHITE)
    assert bot.expected_score(white(0), chess.WHITE, wdl=wdl) == pytest.approx(50)


def test_a_certain_loss_by_wdl_is_0():
    wdl = ce.PovWdl(ce.Wdl(0, 0, 1000), chess.WHITE)
    assert bot.expected_score(white(0), chess.WHITE, wdl=wdl) == pytest.approx(0)


def test_wdl_is_read_from_telarchybots_side_not_the_side_to_move():
    wdl = ce.PovWdl(ce.Wdl(700, 200, 100), chess.WHITE)
    assert bot.expected_score(white(0), chess.WHITE, wdl=wdl) == pytest.approx(80)
    assert bot.expected_score(white(0), chess.BLACK, wdl=wdl) == pytest.approx(20)


def test_centipawns_without_wdl_follow_a_logistic_centred_on_50():
    assert bot.cp_to_expected(0) == pytest.approx(50)
    assert 50 < bot.cp_to_expected(100) < bot.cp_to_expected(300) < 100
    assert bot.cp_to_expected(300) + bot.cp_to_expected(-300) == pytest.approx(100)


def test_huge_centipawn_scores_stay_inside_0_to_100():
    assert 99 < bot.cp_to_expected(100_000) <= 100
    assert 0 <= bot.cp_to_expected(-100_000) < 1


def test_centipawns_are_read_from_telarchybots_side():
    assert bot.expected_score(white(300), chess.WHITE) == pytest.approx(bot.cp_to_expected(300))
    assert bot.expected_score(white(300), chess.BLACK) == pytest.approx(bot.cp_to_expected(-300))


def test_a_mate_for_telarchybot_is_100_and_a_mate_against_it_is_0():
    assert bot.expected_score(ce.PovScore(ce.Mate(3), chess.WHITE), chess.WHITE) == 100
    assert bot.expected_score(ce.PovScore(ce.Mate(-3), chess.WHITE), chess.WHITE) == 0
    assert bot.expected_score(ce.PovScore(ce.Mate(-2), chess.BLACK), chess.WHITE) == 100
    assert bot.expected_score(ce.PovScore(ce.Mate(3), chess.WHITE), chess.BLACK) == 0


def test_mate_already_given_is_100_for_the_winner_and_0_for_the_loser():
    assert bot.expected_score(ce.PovScore(ce.MateGiven, chess.WHITE), chess.WHITE) == 100
    assert bot.expected_score(ce.PovScore(ce.MateGiven, chess.WHITE), chess.BLACK) == 0


def test_a_mate_score_wins_over_a_hedged_wdl():
    wdl = ce.PovWdl(ce.Wdl(600, 300, 100), chess.WHITE)
    assert bot.expected_score(ce.PovScore(ce.Mate(5), chess.WHITE), chess.WHITE, wdl=wdl) == 100


# ------------------------------------------------------------ evaluate()


def test_evaluate_asks_for_every_legal_move_with_the_configured_think_time():
    engine = FakeEngine([("e2e4", white(40), None), ("g1f3", white(30), None), ("a2a3", white(-20), None)])
    evals = bot.evaluate(engine, START, chess.WHITE, think=1.0)
    assert engine.calls == [{"fen": START, "time": 1.0, "multipv": 20}]
    assert set(evals) == {"e2e4", "g1f3", "a2a3"}
    assert evals["e2e4"] > evals["g1f3"] > 50 > evals["a2a3"]


def test_evaluate_prefers_wdl_to_centipawns_when_the_engine_reports_it():
    engine = FakeEngine([("e2e4", white(300), ce.PovWdl(ce.Wdl(0, 1000, 0), chess.WHITE))])
    assert bot.evaluate(engine, START, chess.WHITE, 1.0)["e2e4"] == pytest.approx(50)


def test_evaluate_as_black_reads_scores_from_blacks_side():
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    engine = FakeEngine([("e7e5", ce.PovScore(ce.Cp(-30), chess.WHITE), None)])
    assert bot.evaluate(engine, fen, chess.BLACK, 1.0)["e7e5"] == pytest.approx(bot.cp_to_expected(30))


def test_evaluate_skips_a_line_without_a_move():
    engine = FakeEngine([(None, white(10), None), ("e2e4", white(40), None)])
    assert set(bot.evaluate(engine, START, chess.WHITE, 1.0)) == {"e2e4"}


# ----------------------------------------------------------------- plan()


def test_a_move_worth_more_than_its_price_by_over_the_margin_is_bought_higher():
    o = feed([opt("e2e4", 40.0)])["open"]
    trades = bot.plan(o, {"e2e4": 52.0}, set(), bot.Config())
    assert [(t.option, t.direction, t.market_id) for t in trades] == [("e2e4", "higher", "m-e2e4")]
    assert trades[0].expected == 52.0 and trades[0].price == 40.0


def test_a_gap_exactly_at_the_margin_is_not_traded():
    o = feed([opt("e2e4", 40.0), opt("d2d4", 40.0)])["open"]
    trades = bot.plan(o, {"e2e4": 45.0, "d2d4": 45.01}, set(), bot.Config())
    assert [t.option for t in trades] == ["d2d4"]


def test_the_margin_is_configurable():
    o = feed([opt("e2e4", 40.0)])["open"]
    assert bot.plan(o, {"e2e4": 45.0}, set(), bot.Config(margin=4.0))
    assert not bot.plan(o, {"e2e4": 45.0}, set(), bot.Config(margin=6.0))


def test_a_move_priced_near_its_engine_value_is_left_alone():
    o = feed([opt("e2e4", 50.0)])["open"]
    assert bot.plan(o, {"e2e4": 52.0}, set(), bot.Config()) == []


def test_an_option_without_a_price_is_skipped():
    o = feed([opt("e2e4", None, reason="not polled yet")])["open"]
    assert bot.plan(o, {"e2e4": 60.0}, set(), bot.Config()) == []


def test_an_option_without_a_market_id_is_skipped():
    no_id = opt("e2e4", 10.0, market=None)
    no_key = {"id": "d2d4", "san": "d4", "price": 10.0, "lead": 0}
    o = feed([no_id, no_key])["open"]
    assert bot.plan(o, {"e2e4": 60.0, "d2d4": 60.0}, set(), bot.Config()) == []


def test_an_option_the_engine_did_not_rate_is_skipped():
    o = feed([opt("e2e4", 10.0)])["open"]
    assert bot.plan(o, {}, set(), bot.Config()) == []


def test_the_leading_option_priced_over_its_engine_value_is_bought_lower():
    o = feed([opt("a2a3", 70.0), opt("e2e4", 40.0)])["open"]
    trades = bot.plan(o, {"a2a3": 30.0, "e2e4": 42.0}, set(), bot.Config())
    assert [(t.option, t.direction) for t in trades] == [("a2a3", "lower")]


def test_an_overpriced_option_that_is_not_leading_is_not_bought_lower():
    o = feed([opt("a2a3", 60.0), opt("e2e4", 70.0)])["open"]
    assert bot.plan(o, {"a2a3": 20.0, "e2e4": 68.0}, set(), bot.Config()) == []


def test_options_tied_at_the_top_price_all_count_as_leading():
    o = feed([opt("a2a3", 60.0), opt("h2h3", 60.0)])["open"]
    trades = bot.plan(o, {"a2a3": 20.0, "h2h3": 58.0}, set(), bot.Config())
    assert [(t.option, t.direction) for t in trades] == [("a2a3", "lower")]


def test_buying_lower_can_be_switched_off():
    o = feed([opt("a2a3", 70.0), opt("e2e4", 40.0)])["open"]
    assert bot.plan(o, {"a2a3": 30.0, "e2e4": 42.0}, set(), bot.Config(buy_lower=False)) == []


def test_the_stake_grows_with_the_gap_and_is_capped():
    cfg = bot.Config()
    assert bot.stake_for(6.0, cfg) == 3.0
    assert bot.stake_for(20.0, cfg) == 10.0
    assert bot.stake_for(50.0, cfg) == 20.0
    o = feed([opt("e2e4", 40.0), opt("d2d4", 0.1)])["open"]
    amounts = {t.option: t.amount for t in bot.plan(o, {"e2e4": 46.0, "d2d4": 55.0}, set(), cfg)}
    assert amounts == {"e2e4": 3.0, "d2d4": 20.0}


def test_at_most_max_trades_per_proposal_best_engine_moves_first():
    o = feed([opt(u, 0.1) for u in ("a2a3", "d2d4", "e2e4", "g1f3")])["open"]
    evals = {"e2e4": 55.0, "g1f3": 54.0, "a2a3": 30.0, "d2d4": 53.0}
    trades = bot.plan(o, evals, set(), bot.Config(max_trades=3))
    assert [t.option for t in trades] == ["e2e4", "g1f3", "d2d4"]


def test_a_lower_trade_on_the_leader_comes_before_higher_trades():
    o = feed([opt("a2a3", 70.0), opt("e2e4", 10.0)])["open"]
    trades = bot.plan(o, {"a2a3": 30.0, "e2e4": 55.0}, set(), bot.Config(max_trades=1))
    assert [(t.option, t.direction) for t in trades] == [("a2a3", "lower")]


def test_the_cap_counts_trades_already_made_in_this_proposal():
    o = feed([opt("e2e4", 0.1), opt("d2d4", 0.1)])["open"]
    trades = bot.plan(o, {"e2e4": 55.0, "d2d4": 54.0}, {"b1c3", "g1h3"}, bot.Config(max_trades=3))
    assert [t.option for t in trades] == ["e2e4"]


def test_NEVER_TWICE_an_option_already_traded_in_this_proposal_is_not_planned_again():
    o = feed([opt("e2e4", 0.1)])["open"]
    assert bot.plan(o, {"e2e4": 55.0}, {"e2e4"}, bot.Config()) == []


# ---------------------------------------------------------- Bot.cycle()


def test_nothing_happens_when_it_is_not_telarchybots_move():
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    b.cycle(feed([opt("e2e4", 0.1)], phase="their-move"))
    assert engine.calls == [] and post.calls == []


def test_nothing_happens_when_the_proposal_is_not_tradeable():
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    b.cycle(feed([opt("e2e4", 0.1)], tradeable=False))
    assert engine.calls == [] and post.calls == []


def test_nothing_happens_when_no_proposal_is_open():
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    state = feed([opt("e2e4", 0.1)])
    state["open"] = None
    b.cycle(state)
    assert engine.calls == [] and post.calls == []


@pytest.mark.parametrize("seconds", [3.0, 2.0, 0.0, -5.0])
def test_nothing_happens_with_three_seconds_or_less_to_decide(seconds):
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    b.cycle(feed([opt("e2e4", 0.1)], decide_in=seconds))
    assert engine.calls == [] and post.calls == []


def test_a_trade_goes_ahead_with_just_over_three_seconds_to_decide():
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    b.cycle(feed([opt("e2e4", 0.1)], decide_in=3.5))
    assert len(engine.calls) == 1 and len(post.calls) == 1


def test_no_trade_when_thinking_used_up_the_time_left():
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True, think_cost=1.5)
    b.cycle(feed([opt("e2e4", 0.1)], decide_in=4.0))
    assert len(engine.calls) == 1 and post.calls == []


def test_DRY_RUN_without_a_key_places_nothing_and_says_what_it_would_do():
    b, _, post, printed = make_bot([rated("e2e4", 55)])
    b.cycle(feed([opt("e2e4", 0.1, san="e4")]))
    assert post.calls == []
    assert any("would buy higher" in line and "e4" in line for line in printed)


def test_DRY_RUN_with_a_key_only_asks_for_a_quote():
    b, _, post, printed = make_bot([rated("e2e4", 55)], key="k", live=False)
    b.cycle(feed([opt("e2e4", 0.1)]))
    assert len(post.calls) == 1
    assert post.calls[0]["body"]["dryRun"] is True
    assert any("quote" in line for line in printed)


def test_live_posts_the_trade_to_the_feeds_endpoint_with_the_agent_key_and_workspace():
    b, _, post, printed = make_bot([rated("e2e4", 55)], key="secret", live=True)
    b.cycle(feed([opt("e2e4", 0.1)]))
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == "https://telarchy.com/api/predictions/trade"
    assert call["headers"]["X-Agent-Key"] == "secret"
    assert call["headers"]["X-Workspace-Id"] == WORKSPACE
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Idempotency-Key"] == "chess-p1-e2e4-higher"
    assert call["body"] == {"marketId": "m-e2e4", "direction": "higher", "amount": 20.0, "limit": 55.0}
    assert any("traded" in line for line in printed)


def test_a_base_with_a_trailing_slash_still_builds_one_url():
    b, _, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    b.cycle(feed([opt("e2e4", 0.1)], base="https://telarchy.com/beta/api/"))
    assert post.calls[0]["url"] == "https://telarchy.com/beta/api/predictions/trade"


def test_NEVER_TWICE_polling_the_same_proposal_again_does_not_trade_again():
    b, _, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    state = feed([opt("e2e4", 0.1)])
    b.cycle(state)
    b.cycle(state)
    assert len(post.calls) == 1


def test_NEVER_TWICE_a_dry_run_reports_an_option_once_per_proposal():
    b, _, _, printed = make_bot([rated("e2e4", 55)])
    state = feed([opt("e2e4", 0.1)])
    b.cycle(state)
    b.cycle(state)
    assert sum("would buy" in line for line in printed) == 1


def test_the_engine_runs_once_per_position_not_every_poll():
    b, engine, _, _ = make_bot([rated("e2e4", 55)])
    state = feed([opt("e2e4", 0.1)])
    b.cycle(state)
    b.cycle(state)
    assert len(engine.calls) == 1


def test_a_new_proposal_may_trade_the_same_move_again():
    b, _, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    b.cycle(feed([opt("e2e4", 0.1)], proposal="p1"))
    b.cycle(feed([opt("e2e4", 0.1)], proposal="p2"))
    assert [c["headers"]["Idempotency-Key"] for c in post.calls] == ["chess-p1-e2e4-higher", "chess-p2-e2e4-higher"]


def test_a_refused_trade_is_reported_and_not_retried():
    refusal = FakePost(409, {"code": "price_moved", "consensus": 58, "limit": 55})
    b, _, post, printed = make_bot([rated("e2e4", 55)], key="k", live=True, post=refusal)
    state = feed([opt("e2e4", 0.1)])
    b.cycle(state)
    b.cycle(state)
    assert len(post.calls) == 1
    assert any("price_moved" in line for line in printed)


def test_a_post_that_raises_is_reported_and_the_loop_survives():
    broken = FakePost(raises=OSError("connection reset"))
    b, _, post, printed = make_bot([rated("e2e4", 55)], key="k", live=True, post=broken)
    b.cycle(feed([opt("e2e4", 0.1)]))
    assert len(post.calls) == 1
    assert any("failed" in line and "connection reset" in line for line in printed)


def test_a_feed_with_null_prices_and_missing_market_ids_does_not_crash():
    options = [
        opt("e2e4", None, market=None, reason="not polled yet"),
        {"id": "g1f3", "san": "Nf3", "price": None, "lead": None, "marketId": None, "reason": "no price"},
        {"id": "d2d4", "san": "d4", "price": 12.0, "lead": 0},
    ]
    b, _, post, _ = make_bot([rated("e2e4", 55), rated("g1f3", 55), rated("d2d4", 55)], key="k", live=True)
    b.cycle(feed(options))
    assert post.calls == []


def test_a_feed_without_a_game_or_a_trade_block_is_skipped():
    b, engine, post, _ = make_bot([rated("e2e4", 55)], key="k", live=True)
    no_game = feed([opt("e2e4", 0.1)])
    no_game["game"] = None
    no_trade = feed([opt("e2e4", 0.1)])
    no_trade["trade"] = None
    b.cycle(no_game)
    b.cycle(no_trade)
    assert post.calls == []


# ------------------------------------------------------- config and main()


def test_config_reads_every_knob_from_the_environment():
    cfg = bot.Config.from_env({
        "CHESS_FEED": "http://x/state", "STOCKFISH": "/sf", "THINK_SECONDS": "0.5",
        "MARGIN": "7", "STAKE": "2", "MAX_STAKE": "9", "MAX_TRADES": "4",
        "BUY_LOWER": "0", "POLL_SECONDS": "1",
    })
    assert (cfg.feed, cfg.stockfish, cfg.think, cfg.margin, cfg.stake, cfg.max_stake,
            cfg.max_trades, cfg.buy_lower, cfg.poll) == ("http://x/state", "/sf", 0.5, 7.0, 2.0, 9.0, 4, False, 1.0)


def test_config_defaults_match_the_readme():
    cfg = bot.Config.from_env({})
    assert cfg.feed == "https://chess.167-233-147-90.nip.io/state"
    assert (cfg.stockfish, cfg.think, cfg.margin, cfg.stake, cfg.max_stake,
            cfg.max_trades, cfg.buy_lower, cfg.poll) == ("stockfish", 1.0, 5.0, 5.0, 20.0, 3, True, 2.0)


def test_live_without_a_key_refuses_to_start(capsys):
    assert bot.main(["--live", "--once"], env={}) == 2
    assert "TELARCHY_KEY" in capsys.readouterr().err


def test_a_missing_stockfish_stops_the_bot_with_a_clear_message(capsys, monkeypatch):
    monkeypatch.setattr(bot, "fetch_feed", lambda *a, **k: pytest.fail("read the feed without an engine"))
    assert bot.main(["--once"], env={"STOCKFISH": "/nonexistent/stockfish"}) == 2
    err = capsys.readouterr().err
    assert "Stockfish" in err and "/nonexistent/stockfish" in err


def test_find_stockfish_looks_on_path_then_where_debian_installs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.shutil, "which", lambda name: None)
    exe = tmp_path / "stockfish"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(bot, "STOCKFISH_FALLBACKS", [str(exe)])
    assert bot.find_stockfish("stockfish") == str(exe)
    monkeypatch.setattr(bot, "STOCKFISH_FALLBACKS", [])
    assert bot.find_stockfish("stockfish") is None


def test_an_explicit_stockfish_path_is_used_as_given_or_not_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.shutil, "which", lambda name: None)
    monkeypatch.setattr(bot, "STOCKFISH_FALLBACKS", ["/usr/games/stockfish"])
    exe = tmp_path / "sf"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert bot.find_stockfish(str(exe)) == str(exe)
    assert bot.find_stockfish(str(tmp_path / "missing")) is None


# ------------------------------------------------------------- real HTTP


class Stub(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        Stub.seen.append({"method": "GET", "path": self.path})
        self._json(200, {"schema": 1, "phase": "seeking"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        headers = {k.lower(): v for k, v in self.headers.items()}
        Stub.seen.append({"method": "POST", "path": self.path, "headers": headers, "body": body})
        if body.get("direction") == "lower":
            return self._json(409, {"code": "price_moved", "consensus": 58, "limit": 55})
        self._json(201, {"shares": 2.5, "cost": 3.0, "consensus": 12.0})


@pytest.fixture
def stub():
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Stub.seen.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_post_sends_json_with_the_given_headers(stub):
    status, body = bot.http_post(
        f"{stub}/api/predictions/trade",
        {"X-Agent-Key": "k", "X-Workspace-Id": "w", "Content-Type": "application/json"},
        {"marketId": "m", "direction": "higher", "amount": 3.0},
    )
    assert status == 201 and body["shares"] == 2.5
    seen = Stub.seen[0]
    assert seen["path"] == "/api/predictions/trade"
    assert seen["headers"]["x-agent-key"] == "k" and seen["headers"]["x-workspace-id"] == "w"
    assert seen["body"] == {"marketId": "m", "direction": "higher", "amount": 3.0}


def test_http_post_returns_a_refusal_instead_of_raising(stub):
    status, body = bot.http_post(f"{stub}/api/predictions/trade", {}, {"direction": "lower"})
    assert status == 409 and body["code"] == "price_moved"


def test_fetch_feed_reads_the_json(stub):
    assert bot.fetch_feed(f"{stub}/state")["phase"] == "seeking"


def test_a_live_trade_goes_over_real_http_to_the_feeds_base(stub):
    clock = Clock()
    engine = FakeEngine([rated("e2e4", 55)], clock)
    printed: list[str] = []
    b = bot.Bot(bot.Config(), engine, key="k", live=True, post=bot.http_post, clock=clock, out=printed.append)
    b.cycle(feed([opt("e2e4", 0.1)], base=f"{stub}/api"))
    posts = [s for s in Stub.seen if s["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["path"] == "/api/predictions/trade"
    assert posts[0]["headers"]["x-agent-key"] == "k"
    assert posts[0]["body"]["marketId"] == "m-e2e4"
    assert any("traded" in line for line in printed)
