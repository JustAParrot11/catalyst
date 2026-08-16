"""What the bot is allowed to treat as a company.

ESCALATION-4. A Form 4 filing whose ticker field said SPY produced a
perfectly ordinary insider-cluster candidate: nothing cross-checked the
issuer against the symbol, and no rule anywhere said funds are not
companies. The candidate then went to research like any other, and a
"cluster of insiders buying SPY" is a thesis about a thing that cannot
happen.

WHY IT CANNOT HAPPEN, which is what makes this a rule and not a taste.
Section 16 of the Exchange Act applies to officers, directors and 10%
owners OF AN ISSUER. An index ETF has none: there is no CFO of SPY.
Any Form 4 that appears to describe insider buying in a fund is bad
data - a symbol collision, a mis-parse, or a filing against the fund's
sponsor that has been attributed to the fund's ticker. It is never the
signal it appears to be.

THE LIST IS AN EXCLUSION LIST, AND IT FAILS OPEN ON PURPOSE.

OWNER-ASKED, repeatedly: "i dont want to be narrowing the bots scope",
and "a broad range of investment areas not just one". So an unknown
symbol is TRADEABLE. The two errors are not symmetric:

  - Missing a fund costs one junk candidate, which research will
    almost certainly decline, and which the funnel will show.
  - Excluding a real company costs every trade in that company,
    forever, and NOTHING ON THE PAGE WOULD SAY SO. That is exactly the
    silent-refusal failure the brief was written against.

So this file only ever names things it is sure about. It is not a
whitelist of tradeable companies and must never become one.
"""

#: Funds, notes and index proxies - things with tickers that are not
#: companies. Ordered by what a US equity feed actually emits: broad
#: market, sectors, factor and thematic, leveraged and inverse, bonds,
#: commodities, volatility, and the country/region funds that most often
#: collide with real symbols.
#:
#: This does not need to be exhaustive to be useful, and deliberately
#: is not. Adding a symbol here is a decision to never trade it, so the
#: bar is "this is definitely a fund", not "this looks like one".
FUND_SYMBOLS = frozenset({
    # broad US market
    "SPY", "VOO", "IVV", "VTI", "QQQ", "QQQM", "DIA", "IWM", "IWB", "IWV",
    "RSP", "SPLG", "SCHX", "SCHB", "ITOT", "VTHR", "MDY", "IJH", "IJR",
    "VO", "VB", "VV", "SPTM", "ONEQ", "OEF", "SPMD", "SPSM",
    # style and factor
    "VUG", "VTV", "IWF", "IWD", "IWN", "IWO", "IWP", "IWS", "IWR",
    "MTUM", "QUAL", "USMV", "VLUE", "SIZE", "SPHQ", "SPLV", "SPYV",
    "SPYG", "VYM", "SCHD", "DVY", "NOBL", "SDY", "HDV", "DGRO", "VIG",
    # sector SPDRs and peers
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU",
    "XLV", "XLY", "XBI", "XOP", "XME", "XRT", "XHB", "XSD", "XPH",
    "SMH", "SOXX", "IBB", "IHI", "IYR", "IYT", "ITB", "KRE", "KBE",
    "KIE", "OIH", "GDX", "GDXJ", "URA", "TAN", "ICLN", "PBW", "LIT",
    "JETS", "HACK", "SKYY", "FDN", "IGV", "VGT", "VHT", "VFH", "VDE",
    "VNQ", "VPU", "VAW", "VIS", "VCR", "VDC", "VOX", "PSCE",
    # thematic
    "ARKK", "ARKG", "ARKQ", "ARKW", "ARKF", "ARKX", "BOTZ", "ROBO",
    "ESPO", "HERO", "MSOS", "MJ", "BLOK", "IPO",
    "MOON", "UFO", "PAVE", "GRID", "CIBR", "BUG", "WCLD", "CLOU",
    # leveraged and inverse - the ones a feed most often mangles
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SPXU", "UPRO", "SDOW", "UDOW",
    "TNA", "TZA", "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ",
    "NUGT", "DUST", "JNUG", "JDST", "YINN", "YANG", "TMF", "TMV",
    "SH", "SDS", "PSQ", "QID", "DOG", "DXD", "RWM", "TWM", "SSO",
    "QLD", "DDM", "UWM", "URTY", "SRTY", "BOIL", "KOLD", "UCO", "SCO",
    # bonds and rates
    "AGG", "BND", "BNDX", "TLT", "TLH", "IEF", "IEI", "SHY", "SHV",
    "BIL", "GOVT", "TIP", "VTIP", "SCHP", "LQD", "HYG", "JNK", "SJNK",
    "EMB", "MUB", "VCIT", "VCSH", "VGIT", "VGSH", "VGLT", "BSV", "BIV",
    "BLV", "MBB", "SPTL", "SPTS", "USFR", "SGOV", "FLOT", "ANGL",
    # commodities, metals, currency
    "GLD", "GLDM", "IAU", "SLV", "SIVR", "PPLT", "PALL", "USO", "BNO",
    "UNG", "DBA", "DBC", "DBO", "PDBC", "CORN", "WEAT", "SOYB", "COMT",
    "UUP", "UDN", "FXE", "FXY", "FXB", "FXF", "FXA", "FXC",
    # volatility
    "VXX", "VIXY", "UVXY", "SVXY", "VIXM", "TVIX", "ZIV",
    # international and country
    "EFA", "IEFA", "VEA", "VXUS", "VEU", "IXUS", "ACWI", "ACWX", "VT",
    "EEM", "IEMG", "VWO", "SCHE", "SPEM", "EWJ", "EWZ", "EWW", "EWY",
    "EWT", "EWU", "EWG", "EWC", "EWA", "EWH", "EWS", "EWL", "EWD",
    "EWP", "EWI", "EWQ", "EWN", "EWK", "EWO", "EPI", "INDA", "INDY",
    "FXI", "MCHI", "KWEB", "ASHR", "CQQQ", "GXC", "PGJ", "RSX",
    "ILF", "EZA", "TUR", "ARGT", "GREK", "EIS", "KSA", "UAE", "QAT",
    # closed-end and preferred, which trade like stock and are not
    "PFF", "PGX", "PFFD", "SPFF", "PSK",
    # crypto vehicles and trusts
    "GBTC", "ETHE", "BITO", "BITB", "IBIT", "FBTC", "ARKB", "BTF",
    "ETHA", "BITX", "BRRR", "HODL", "EZBC", "BTCO",
})

#: Suffixes and markers that identify a NON-COMMON-STOCK line even where
#: the root symbol is a real company: warrants, rights, units and
#: when-issued lines. Insider clusters and company catalysts are theses
#: about the common stock; these instruments have different economics,
#: far worse liquidity, and in the case of warrants and rights an
#: expiry that no part of this system models.
_NON_COMMON_SUFFIXES = ("W", "R", "U")


def _looks_like_a_unit_or_warrant(symbol: str) -> bool:
    """Five-letter symbols ending W/R/U are warrants, rights or units.

    Deliberately narrow: only the 5-character case, which is the
    convention for these lines. Four-letter symbols ending in those
    letters are ordinary companies in large numbers - shortening this
    rule by one character would silently exclude a great many real
    names, which is the expensive direction.
    """
    return len(symbol) == 5 and symbol[-1] in _NON_COMMON_SUFFIXES


def excluded_reason(symbol) -> str | None:
    """Why `symbol` cannot carry a company catalyst, or None if it can.

    Returns a SENTENCE, not a code: it goes on the funnel as a drop
    reason, and "no company catalyst can apply to a fund" is something
    the owner can act on where `universe_excluded` is not.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None                      # the symbol filters own this
    if sym in FUND_SYMBOLS:
        return (f"{sym} is a fund, note or index product, not a company - "
                "it has no officers or directors, so no company catalyst "
                "can apply to it")
    if _looks_like_a_unit_or_warrant(sym):
        return (f"{sym} looks like a warrant, right or unit rather than "
                "common stock - different economics, thin liquidity, and "
                "an expiry this system does not model")
    return None


def is_tradeable(symbol) -> bool:
    """True when nothing in the universe rules objects to `symbol`."""
    return excluded_reason(symbol) is None
