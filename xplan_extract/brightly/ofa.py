"""Ongoing fee arrangements (OFA): the annual consent rules, as code (step 7.1).

The rules (ASIC INFO 286; Corporations Act Part 7.7A Div 3, as amended from 10 January 2025):

* Each year the client renews the arrangement, and consents to the fee being deducted from each
  account, in a written, signed and dated form (renewal and deduction can be one form;
  electronic signing is fine, verbal is not). Every holder of a joint account must sign.
* The consent window runs from 60 days before to 150 days after the anniversary (the
  "reference day" each year, s962H).
* No consent in the window: the arrangement ends at the end of the window (s962G(2)). Fees
  stop, and the account providers must be told within 10 business days (s962V).
* A client can end it any time. A written withdrawal must be acknowledged, and any fee taken
  after consent ended refunded, within 10 business days (s962U, s962S(9)).
* Arrangements from before 10 January 2025 moved to these rules at their first anniversary on
  or after that date.
* Keep consents, notices and disclosures for at least 5 years (reg 7.7A.11AA, s962X).

Everything here is a pure function of the arrangement, its consents and a date, so the same
answer comes out in the CRM, a report or a test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache

NEW_RULES_FROM = dt.date(2025, 1, 10)
WINDOW_BEFORE = 60
WINDOW_AFTER = 150
NOTIFY_BUSINESS_DAYS = 10
KEEP_YEARS = 5
SOON_DAYS = 45          # "due soon" when the window closes within this many days
URGENT_DAYS = 14


# ----------------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------------

def anniversary(reference_day: dt.date, year: int) -> dt.date:
    """The reference day in a given year (29 February falls on 28 February otherwise)."""
    try:
        return reference_day.replace(year=year)
    except ValueError:
        return dt.date(year, 2, 28)


def _easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return dt.date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed(day: dt.date) -> dt.date:
    """Holidays falling on a weekend move to Monday."""
    return day + dt.timedelta(days={5: 2, 6: 1}.get(day.weekday(), 0))


@lru_cache(maxsize=None)
def victorian_public_holidays(year: int) -> frozenset[dt.date]:
    """National plus Victorian public holidays (the practice is in Melbourne). The AFL Grand
    Final Friday changes every year; add it with extra_holidays when announced."""
    easter = _easter(year)
    christmas, boxing = dt.date(year, 12, 25), dt.date(year, 12, 26)
    days = {
        _observed(dt.date(year, 1, 1)),
        _observed(dt.date(year, 1, 26)),
        _nth_weekday(year, 3, 0, 2),                  # Labour Day (VIC): 2nd Monday in March
        easter - dt.timedelta(days=2), easter - dt.timedelta(days=1), easter,
        easter + dt.timedelta(days=1),
        dt.date(year, 4, 25),
        _nth_weekday(year, 6, 0, 2),                  # King's Birthday: 2nd Monday in June
        _nth_weekday(year, 11, 1, 1),                 # Melbourne Cup: 1st Tuesday in November
    }
    if christmas.weekday() == 5:                      # Sat: Mon 27 and Tue 28
        days |= {christmas + dt.timedelta(days=2), boxing + dt.timedelta(days=2)}
    elif christmas.weekday() == 6:                    # Sun: Mon 26 (Boxing) and Tue 27
        days |= {boxing, christmas + dt.timedelta(days=2)}
    elif christmas.weekday() == 4:                    # Fri: Boxing Day Sat -> Mon 28
        days |= {christmas, boxing + dt.timedelta(days=2)}
    else:
        days |= {christmas, boxing}
    return frozenset(days)


def add_business_days(start: dt.date, n: int,
                      extra_holidays: frozenset[dt.date] = frozenset()) -> dt.date:
    day = start
    while n > 0:
        day += dt.timedelta(days=1)
        if day.weekday() < 5 and day not in victorian_public_holidays(day.year) \
                and day not in extra_holidays:
            n -= 1
    return day


# ----------------------------------------------------------------------------
# Where an arrangement stands on a given day
# ----------------------------------------------------------------------------

@dataclass
class Window:
    anniversary: dt.date
    opens: dt.date
    closes: dt.date


def window(anniv: dt.date) -> Window:
    return Window(anniv, anniv - dt.timedelta(days=WINDOW_BEFORE),
                  anniv + dt.timedelta(days=WINDOW_AFTER))


@dataclass
class Status:
    state: str                 # see STATES
    label: str
    anniversary: dt.date | None = None
    opens: dt.date | None = None
    closes: dt.date | None = None
    days_left: int | None = None
    action: str = ""
    due_on: dt.date | None = None
    last_consent: dt.date | None = None
    problems: list[str] = field(default_factory=list)


STATES = {
    "covered": "Consent current",
    "upcoming": "Window opens soon",
    "open": "Consent due",
    "due_soon": "Consent due soon",
    "urgent": "Consent urgent",
    "lapsed": "Lapsed: notify providers",
    "ended": "Ended",
    "check": "Needs checking",
}


def status(reference_day: dt.date | None, consents: list[dt.date], today: dt.date, *,
           arrangement_status: str = "active", ended_on: dt.date | None = None,
           providers_notified: bool = False, has_accounts: bool = True,
           consent_history_known: bool = True) -> Status:
    """Where an arrangement stands on `today`.

    `consents` are the anniversaries a valid consent was recorded for. The arrangement needs a
    consent for every anniversary since the new rules applied to it.
    """
    problems = []
    if not has_accounts:
        problems.append("no deduction account recorded")
    if not consent_history_known:
        problems.append("last consent not confirmed (from Xplan)")
    if arrangement_status == "ended":
        return Status("ended", STATES["ended"], problems=problems)
    if reference_day is None:
        return Status("check", STATES["check"], action="Enter the arrangement's anniversary",
                      problems=problems + ["no anniversary date"])
    done = set(consents)
    last = max(done) if done else None

    # Every anniversary after the arrangement started, from when the new rules applied to it,
    # up to next year's: each needs its own consent.
    years = range(max(NEW_RULES_FROM.year, reference_day.year + 1), today.year + 2)
    for anniv in (anniversary(reference_day, y) for y in years):
        if anniv < NEW_RULES_FROM or anniv in done or (last and anniv < last):
            continue           # a later consent shows the arrangement carried on
        w = window(anniv)
        if w.closes < today and not consent_history_known:
            continue           # what happened in Xplan isn't known yet: don't call it lapsed
        if today > w.closes:
            notify_by = add_business_days(w.closes, NOTIFY_BUSINESS_DAYS)
            if providers_notified:
                return Status("ended", "Lapsed (providers told)", anniv, w.opens, w.closes, 0,
                              last_consent=last, problems=problems)
            return Status("lapsed", STATES["lapsed"], anniv, w.opens, w.closes, 0,
                          f"No consent by {w.closes:%d %b %Y}: the arrangement has ended. Stop "
                          f"fees and tell each account provider by {notify_by:%d %b %Y}.",
                          notify_by, last, problems)
        left = (w.closes - today).days
        if today < w.opens:
            if (w.opens - today).days <= 30:
                return Status("upcoming", STATES["upcoming"], anniv, w.opens, w.closes, left,
                              f"Send the consent form from {w.opens:%d %b %Y}", w.opens, last,
                              problems)
            return Status("covered", STATES["covered"], anniv, w.opens, w.closes, left,
                          last_consent=last, problems=problems)
        state = "urgent" if left <= URGENT_DAYS else "due_soon" if left <= SOON_DAYS else "open"
        return Status(state, STATES[state], anniv, w.opens, w.closes, left,
                      f"Get the signed consent by {w.closes:%d %b %Y} ({left} days left)",
                      w.closes, last, problems)
    return Status("covered", STATES["covered"], last_consent=last, problems=problems)


def consent_anniversary(reference_day: dt.date, signed_on: dt.date) -> dt.date | None:
    """Which anniversary a consent signed on `signed_on` renews, or None if it falls in no
    window (then it isn't a valid renewal)."""
    for year in (signed_on.year - 1, signed_on.year, signed_on.year + 1):
        anniv = anniversary(reference_day, year)
        w = window(anniv)
        if w.opens <= signed_on <= w.closes and anniv >= reference_day:
            return anniv
    return None


def withdrawal_deadlines(received_on: dt.date) -> dict[str, dt.date]:
    due = add_business_days(received_on, NOTIFY_BUSINESS_DAYS)
    return {"acknowledge_by": due, "refund_by": due, "notify_providers_by": due}


def keep_until(day: dt.date) -> dt.date:
    """Earliest date a consent, notice or disclosure may be destroyed (5 years)."""
    return anniversary(day, day.year + KEEP_YEARS)
