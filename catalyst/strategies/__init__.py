"""Strategy definitions for the bake-off. Owner: strategy-analyst.

Each module exposes the same shape:

    build_candidates(...) -> list[Candidate]     # offline, from cached data
    make_signal_fn(...)   -> Callable            # (candidate, PointInTimeView) -> ResearchView

Signal functions receive a PointInTimeView and read NOTHING else at
signal time except pre-derived, point-in-time-safe event tables built
offline from cached primary sources (XBRL companyfacts filtered on
filed <= event date; SEC insider data keyed on FILING_DATE). No network
access anywhere in this package.

The three mechanisms (deliberately different, not parameter variants):
- etf_rotation      (Candidate E): cross-sectional price momentum, ETFs.
- earnings_drift    (Candidate A): fundamental surprise from XBRL,
                    post-earnings drift in single names.
- insider_cluster   (Candidate C): informed-ownership signal from
                    clustered open-market Form 4 purchases.
"""
