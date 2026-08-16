"""Funds are not companies - and real companies are not funds.

ESCALATION-4. A Form 4 whose ticker field said SPY produced an ordinary
insider-cluster candidate, because nothing anywhere said that an index
fund has no insiders. Section 16 applies to officers, directors and 10%
owners OF AN ISSUER; there is no CFO of SPY. Such a filing is always a
symbol collision or a mis-parse, and never the signal it resembles.

THIS FILE GUARDS BOTH DIRECTIONS, and the second one matters more.

The owner has said twice, in their own words, "i dont want to be
narrowing the bots scope" and asked for "a broad range of investment
areas not just one". An exclusion rule is precisely the kind of change
that quietly does the opposite: every real company wrongly excluded is
a trade that never happens, forever, with nothing on the page to say
why. So the tests below spend more effort proving ordinary stocks still
pass than proving funds do not.
"""

import pytest

from catalyst.discovery.universe import (
    FUND_SYMBOLS, excluded_reason, is_tradeable,
)


class TestFundsAreRefused:
    @pytest.mark.parametrize("symbol", [
        "SPY", "QQQ", "IWM", "VOO", "VTI", "DIA",        # broad market
        "XLF", "XLE", "XBI", "SMH", "GDX",               # sectors
        "TQQQ", "SQQQ", "SOXL", "LABU",                  # leveraged
        "TLT", "HYG", "AGG", "SHY",                      # bonds
        "GLD", "SLV", "USO", "UNG",                      # commodities
        "VXX", "UVXY",                                   # volatility
        "EEM", "EFA", "FXI", "EWJ",                      # international
        "GBTC", "IBIT",                                  # crypto vehicles
        "ARKK",                                          # thematic
    ])
    def test_a_fund_is_never_tradeable(self, symbol):
        assert not is_tradeable(symbol)
        assert "fund" in excluded_reason(symbol)

    def test_the_reason_is_a_sentence_not_a_code(self):
        why = excluded_reason("SPY")
        assert "_" not in why, f"reads like an identifier: {why!r}"
        assert why[0].isupper() or why.startswith("SPY")
        assert len(why.split()) > 6, "too terse to act on"

    def test_case_and_whitespace_do_not_smuggle_one_through(self):
        for variant in ("spy", " SPY ", "Spy", "sPy\t"):
            assert not is_tradeable(variant), f"{variant!r} got through"


class TestRealCompaniesStillTrade:
    """The expensive direction. Every name here is an ordinary operating
    company, several chosen because they LOOK like they could be funds."""

    @pytest.mark.parametrize("symbol", [
        # ordinary large caps
        "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "JPM",
        # small and mid caps, where the insider-cluster edge actually is
        "PLUG", "RIOT", "SAVA", "OCGN", "CLOV", "BBBY", "AMC", "GME",
        # three, four and five letter names that are NOT funds
        "F", "T", "GE", "CAT", "IBM", "COST", "SBUX", "ADBE", "REGN",
        # names that resemble excluded tickers but are real companies
        "SPYR",     # not SPY
        "QQQX",     # not QQQ - a closed-end fund's neighbour in spelling
        "GOLD",     # Barrick Gold, the miner, not the metal ETF
        "OIL",      # not USO
        "BOND",     # not AGG
        "LUNR",     # Intuitive Machines, not the MOON ETF
    ])
    def test_an_ordinary_company_is_tradeable(self, symbol):
        assert is_tradeable(symbol), (
            f"{symbol} was excluded from the universe - if it is a real "
            f"company this silently costs every future trade in it: "
            f"{excluded_reason(symbol)}")

    def test_an_unknown_symbol_fails_OPEN(self):
        """A ticker nobody listed is tradeable. Missing a fund costs one
        junk candidate that research will decline; excluding a real
        company costs every trade in it and says nothing."""
        assert is_tradeable("ZZZZ")
        assert is_tradeable("WXYZ")

    def test_the_list_is_an_exclusion_list_not_a_whitelist(self):
        """If this ever inverts, the bot stops trading almost
        everything - and the funnel would call it normal attrition.

        Checked as a property rather than a comment: far more symbols
        must pass than fail, and every symbol NOT named must pass.
        """
        made_up = [f"ZQ{a}{b}" for a in "ABCD" for b in "EFGH"]
        assert all(is_tradeable(s) for s in made_up), \
            "unlisted symbols are being refused - this is a whitelist now"


class TestWarrantsAndUnits:
    @pytest.mark.parametrize("symbol", ["ABCDW", "ABCDR", "ABCDU"])
    def test_five_letter_warrant_rights_and_unit_lines_are_refused(
            self, symbol):
        assert not is_tradeable(symbol)
        assert "warrant" in excluded_reason(symbol)

    @pytest.mark.parametrize("symbol", [
        "SNOW", "CHTR", "AMCR", "LCID", "NCLH", "AAWW", "CRWD",
        "ANSW", "GLBR", "HOUR", "FOUR", "PLAU",
    ])
    def test_four_letter_names_ending_in_those_letters_are_UNTOUCHED(
            self, symbol):
        """The rule is deliberately only the 5-character case. Four-letter
        symbols ending W/R/U are ordinary companies in large numbers, and
        shortening the rule by one character would exclude a great many
        real names at once."""
        assert is_tradeable(symbol), f"{symbol} wrongly read as a warrant"


class TestTheListItself:
    def test_every_entry_is_a_plausible_symbol(self):
        for sym in FUND_SYMBOLS:
            assert sym == sym.upper(), f"{sym} is not upper case"
            assert sym.isalpha(), f"{sym} is not alphabetic"
            assert 1 <= len(sym) <= 5, f"{sym} is not a symbol length"

    def test_it_does_not_contain_famous_operating_companies(self):
        """A guard against the most damaging possible typo in this file."""
        for sym in ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL",
                    "META", "NFLX", "JPM", "BAC", "XOM", "CVX", "PFE",
                    "MRNA", "INTC", "AMD", "F", "GM", "T", "VZ"):
            assert sym not in FUND_SYMBOLS, (
                f"{sym} is an operating company and would never be traded")

    def test_empty_and_junk_are_left_to_the_symbol_filters(self):
        """This module answers "is it a company"; whether a string is a
        symbol at all is `_valid_symbol`'s job, and two filters silently
        disagreeing about the same input is worse than one gap."""
        assert excluded_reason("") is None
        assert excluded_reason(None) is None
