"""Reading statistics for one user: calendar, activity graph and charts

Everything here is computed from data BookWyrm already stores:
- ReadThrough.finish_date: the day a book was finished
- ReadThrough.start_date: the day a book was started
- ProgressUpdate: progress logged while reading (a position, not pages read)
- Edition.pages: the page count from the book's metadata

Dates are worked out in the user's own preferred timezone, because finish
dates are saved as midnight in that timezone (converted to UTC).
"""

from __future__ import annotations

import datetime
import zoneinfo
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone

from bookwyrm import models
from bookwyrm.models.readthrough import ProgressMode

if TYPE_CHECKING:
    from bookwyrm.models import User


ONE_DAY = datetime.timedelta(days=1)


@dataclass
class DayActivity:
    """what happened on one day"""

    date: datetime.date
    finished: list[models.ReadThrough] = field(default_factory=list)
    updates: int = 0
    pages: int = 0

    @property
    def is_active(self) -> bool:
        """did anything happen on this day"""
        return bool(self.finished) or self.updates > 0

    @property
    def level(self) -> int:
        """intensity from 0 (nothing) to 4 (a lot), for the activity graph"""
        score = self.updates + 2 * len(self.finished)
        if score >= 5:
            return 4
        if score >= 3:
            return 3
        return score


@dataclass
class PeriodTotal:
    """books and pages finished in a month or a year"""

    books: int = 0
    pages: int = 0
    books_without_pages: int = 0


@dataclass
class YearSummary:
    """the numbers shown in the tiles at the top of the stats page"""

    year: int
    books: int
    pages: int
    books_without_pages: int
    pages_per_day: float
    average_days_per_book: Optional[float]
    average_pages_per_book: Optional[float]
    goal: Optional[int]
    goal_percent: Optional[int]
    books_previous_year_to_date: int
    active_days: int
    current_streak: int
    best_streak: int


def user_timezone(user: User) -> zoneinfo.ZoneInfo:
    """the user's preferred timezone, falling back to UTC"""
    try:
        return zoneinfo.ZoneInfo(user.preferred_timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
        return zoneinfo.ZoneInfo("UTC")


def user_today(user: User) -> datetime.date:
    """today's date where the user lives"""
    return timezone.now().astimezone(user_timezone(user)).date()


def local_date(value: datetime.datetime, tz: zoneinfo.ZoneInfo) -> datetime.date:
    """the calendar date of a stored datetime, in the given timezone"""
    return value.astimezone(tz).date()


def date_range_bounds(
    start: datetime.date, end: datetime.date, tz: zoneinfo.ZoneInfo
) -> tuple[datetime.datetime, datetime.datetime]:
    """aware datetimes covering local days start to end, inclusive

    use as: field__gte=lower, field__lt=upper
    """
    lower = datetime.datetime.combine(start, datetime.time.min, tzinfo=tz)
    upper = datetime.datetime.combine(end + ONE_DAY, datetime.time.min, tzinfo=tz)
    return lower, upper


def finished_readthroughs(
    user: User, start: datetime.date, end: datetime.date
) -> QuerySet[models.ReadThrough]:
    """readthroughs finished between two local dates (inclusive), oldest first

    The book and its authors are loaded up front, so showing covers (whose
    alt text uses the author names) doesn't run extra queries per book.
    """
    lower, upper = date_range_bounds(start, end, user_timezone(user))
    return (
        models.ReadThrough.objects.filter(
            user=user, finish_date__gte=lower, finish_date__lt=upper
        )
        .select_related("book")
        .prefetch_related("book__authors")
        .order_by("finish_date", "id")
    )


def pages_from_progress(
    progress: int,
    previous: Optional[int],
    mode: str,
    book_pages: Optional[int],
) -> int:
    """estimate pages read in one progress update

    Progress is a position in the book (page 142, or 45%), so the pages read
    are the difference from the previous update on the same read-through.
    Percentages need the book's page count; without it we can't estimate.
    """
    delta = progress - (previous or 0)
    if delta <= 0:
        return 0
    if mode == ProgressMode.PERCENT:
        if not book_pages:
            return 0
        return round(book_pages * min(delta, 100) / 100)
    return delta


def progress_by_day(
    user: User, start: datetime.date, end: datetime.date
) -> dict[datetime.date, tuple[int, int]]:
    """{local date: (number of updates, estimated pages)} for days with updates

    One query. The previous update on the same read-through (in the same
    mode) is looked up in SQL, even when it was before the start date.
    """
    tz = user_timezone(user)
    lower, upper = date_range_bounds(start, end, tz)
    previous = (
        models.ProgressUpdate.objects.filter(
            readthrough=OuterRef("readthrough"),
            mode=OuterRef("mode"),
            created_date__lt=OuterRef("created_date"),
        )
        .order_by("-created_date")
        .values("progress")[:1]
    )
    rows = (
        models.ProgressUpdate.objects.filter(
            user=user, created_date__gte=lower, created_date__lt=upper
        )
        .annotate(previous_progress=Subquery(previous))
        .values_list(
            "created_date",
            "progress",
            "previous_progress",
            "mode",
            "readthrough__book__pages",
        )
    )

    result: dict[datetime.date, tuple[int, int]] = {}
    for created, progress, prev, mode, book_pages in rows:
        day = local_date(created, tz)
        updates, pages = result.get(day, (0, 0))
        result[day] = (
            updates + 1,
            pages + pages_from_progress(progress, prev, mode, book_pages),
        )
    return result


def daily_activity(
    user: User, start: datetime.date, end: datetime.date
) -> dict[datetime.date, DayActivity]:
    """activity for every day from start to end (inclusive), in date order

    Two queries: finished read-throughs, and progress updates.
    """
    days: dict[datetime.date, DayActivity] = {}
    day = start
    while day <= end:
        days[day] = DayActivity(date=day)
        day += ONE_DAY

    tz = user_timezone(user)
    for readthrough in finished_readthroughs(user, start, end):
        if readthrough.finish_date is None:  # pragma: no cover
            continue
        days[local_date(readthrough.finish_date, tz)].finished.append(readthrough)

    for day, (updates, pages) in progress_by_day(user, start, end).items():
        days[day].updates = updates
        days[day].pages = pages

    return days


def streaks(
    activity: dict[datetime.date, DayActivity], today: datetime.date
) -> tuple[int, int]:
    """(current streak, best streak) of consecutive active days

    The current streak still counts if today has no activity yet, as long as
    yesterday did.
    """
    best = run = 0
    for day in sorted(activity):
        run = run + 1 if activity[day].is_active else 0
        best = max(best, run)

    current = 0
    day = today
    if day in activity and not activity[day].is_active:
        day -= ONE_DAY
    while day in activity and activity[day].is_active:
        current += 1
        day -= ONE_DAY
    return current, best


def _finished_rows(
    user: User,
    start: Optional[datetime.date] = None,
    end: Optional[datetime.date] = None,
) -> list[tuple[datetime.date, Optional[int], Optional[datetime.date]]]:
    """(local finish date, page count, local start date) for finished books"""
    tz = user_timezone(user)
    queryset = models.ReadThrough.objects.filter(user=user, finish_date__isnull=False)
    if start and end:
        lower, upper = date_range_bounds(start, end, tz)
        queryset = queryset.filter(finish_date__gte=lower, finish_date__lt=upper)

    return [
        (
            local_date(finish, tz),
            pages,
            local_date(started, tz) if started else None,
        )
        for finish, pages, started in queryset.values_list(
            "finish_date", "book__pages", "start_date"
        )
        if finish is not None
    ]


def _add_to_total(total: PeriodTotal, pages: Optional[int]) -> None:
    total.books += 1
    if pages:
        total.pages += pages
    else:
        total.books_without_pages += 1


def totals_by_month(user: User, year: int) -> list[PeriodTotal]:
    """books and pages finished in each month of a year (12 entries)"""
    months = [PeriodTotal() for _ in range(12)]
    rows = _finished_rows(user, datetime.date(year, 1, 1), datetime.date(year, 12, 31))
    for finished, pages, _ in rows:
        _add_to_total(months[finished.month - 1], pages)
    return months


def totals_by_year(user: User) -> dict[int, PeriodTotal]:
    """books and pages finished in every year with activity, oldest first

    Years with nothing finished between the first and last year are included
    with zeros, so a bar chart doesn't skip them.
    """
    years: dict[int, PeriodTotal] = {}
    for finished, pages, _ in _finished_rows(user):
        _add_to_total(years.setdefault(finished.year, PeriodTotal()), pages)
    if not years:
        return {}
    return {
        year: years.get(year, PeriodTotal())
        for year in range(min(years), max(years) + 1)
    }


def cumulative_books_by_month(
    user: User, years: Iterable[int], today: Optional[datetime.date] = None
) -> dict[int, list[Optional[int]]]:
    """running total of books finished by the end of each month, per year

    Months after today are None so charts stop the line at the current month.
    """
    years = sorted(set(years))
    if not years:
        return {}
    today = today or user_today(user)
    counts = {year: [0] * 12 for year in years}
    rows = _finished_rows(
        user, datetime.date(years[0], 1, 1), datetime.date(years[-1], 12, 31)
    )
    for finished, _, _ in rows:
        if finished.year in counts:
            counts[finished.year][finished.month - 1] += 1

    result: dict[int, list[Optional[int]]] = {}
    for year, monthly in counts.items():
        running = 0
        totals: list[Optional[int]] = []
        for month, count in enumerate(monthly, start=1):
            if (year, month) > (today.year, today.month):
                totals.append(None)
                continue
            running += count
            totals.append(running)
        result[year] = totals
    return result


def _same_day_previous_year(day: datetime.date) -> datetime.date:
    """the same date one year earlier (29 Feb becomes 28 Feb)"""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def year_summary(
    user: User, year: int, today: Optional[datetime.date] = None
) -> YearSummary:
    """the summary numbers for one year

    For the current year, "to date" comparisons and pages per day use the
    days elapsed so far. For past years they use the whole year.
    """
    today = today or user_today(user)
    first = datetime.date(year, 1, 1)
    last = datetime.date(year, 12, 31)
    if year == today.year:
        period_end = today
    elif year < today.year:
        period_end = last
    else:
        period_end = first - ONE_DAY  # a future year: nothing has happened

    rows = _finished_rows(user, datetime.date(year - 1, 1, 1), last)
    this_year = [row for row in rows if row[0].year == year]
    previous_cutoff = (
        _same_day_previous_year(period_end) if period_end >= first else None
    )
    previous_to_date = [
        row
        for row in rows
        if row[0].year == year - 1
        and previous_cutoff is not None
        and row[0] <= previous_cutoff
    ]

    total = PeriodTotal()
    for _, pages, _ in this_year:
        _add_to_total(total, pages)

    durations = [
        (finished - started).days for finished, _, started in this_year if started
    ]
    with_pages = [pages for _, pages, _ in this_year if pages]
    days_elapsed = (period_end - first).days + 1 if period_end >= first else 0

    activity = daily_activity(user, first, period_end) if period_end >= first else {}
    current, best = streaks(activity, today)
    if year != today.year:
        current = 0

    goal = (
        models.AnnualGoal.objects.filter(user=user, year=year)
        .values_list("goal", flat=True)
        .first()
    )

    return YearSummary(
        year=year,
        books=total.books,
        pages=total.pages,
        books_without_pages=total.books_without_pages,
        pages_per_day=round(total.pages / days_elapsed, 1) if days_elapsed else 0.0,
        average_days_per_book=(
            round(sum(durations) / len(durations), 1) if durations else None
        ),
        average_pages_per_book=(
            round(sum(with_pages) / len(with_pages), 1) if with_pages else None
        ),
        goal=goal,
        goal_percent=int(total.books / goal * 100) if goal else None,
        books_previous_year_to_date=len(previous_to_date),
        active_days=sum(1 for day in activity.values() if day.is_active),
        current_streak=current,
        best_streak=best,
    )
