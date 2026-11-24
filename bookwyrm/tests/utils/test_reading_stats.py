"""testing reading statistics"""

import datetime
from unittest.mock import patch
import zoneinfo

from django.test import TestCase

from bookwyrm import models
from bookwyrm.models.readthrough import ProgressMode
from bookwyrm.utils import reading_stats


NEW_YORK = zoneinfo.ZoneInfo("America/New_York")


def local(*args):
    """an aware datetime in New York time"""
    return datetime.datetime(*args, tzinfo=NEW_YORK)


def utc(*args):
    """an aware datetime in UTC"""
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class ReadingStats(TestCase):
    """reading statistics for the calendar and stats pages"""

    @classmethod
    def setUpTestData(cls):
        """users and books"""
        with (
            patch("bookwyrm.suggested_users.rerank_suggestions_task.delay"),
            patch("bookwyrm.activitystreams.populate_stream_task.delay"),
            patch("bookwyrm.lists_stream.populate_lists_task.delay"),
        ):
            cls.user = models.User.objects.create_user(
                "mouse@local.com",
                "mouse@mouse.com",
                "mouseword",
                local=True,
                localname="mouse",
                remote_id="https://example.com/users/mouse",
                preferred_timezone="America/New_York",
            )
            cls.other_user = models.User.objects.create_user(
                "rat@local.com",
                "rat@rat.com",
                "ratword",
                local=True,
                localname="rat",
                remote_id="https://example.com/users/rat",
            )
        cls.work = models.Work.objects.create(title="Test Work")
        cls.book = models.Edition.objects.create(
            title="Long Book",
            remote_id="https://example.com/book/1",
            parent_work=cls.work,
            pages=300,
        )
        cls.no_pages_book = models.Edition.objects.create(
            title="Unknown Length",
            remote_id="https://example.com/book/2",
            parent_work=cls.work,
        )

    def finish(self, finish_date, book=None, user=None, start_date=None):
        """create a finished readthrough"""
        return models.ReadThrough.objects.create(
            user=user or self.user,
            book=book or self.book,
            start_date=start_date,
            finish_date=finish_date,
        )

    def progress(self, readthrough, progress, created, mode=ProgressMode.PAGE):
        """create a progress update at a given time"""
        update = models.ProgressUpdate.objects.create(
            user=readthrough.user, readthrough=readthrough, progress=progress, mode=mode
        )
        # created_date is set automatically, so change it afterwards
        models.ProgressUpdate.objects.filter(id=update.id).update(created_date=created)
        return update

    def reading(self, book=None):
        """an unfinished readthrough"""
        return models.ReadThrough.objects.create(
            user=self.user, book=book or self.book, start_date=local(2026, 1, 1)
        )

    def test_user_timezone(self):
        """uses the preferred timezone, or UTC if it's invalid"""
        self.assertEqual(reading_stats.user_timezone(self.user), NEW_YORK)
        self.user.preferred_timezone = "Not/A_Zone"
        self.assertEqual(
            reading_stats.user_timezone(self.user), zoneinfo.ZoneInfo("UTC")
        )

    def test_finished_readthroughs_local_day(self):
        """a book finished late evening in New York is in that local month"""
        # 28 Feb, 9pm in New York is 1 March, 2am in UTC
        late = self.finish(utc(2026, 3, 1, 2))
        march_first = self.finish(local(2026, 3, 1))
        self.finish(local(2026, 3, 1), user=self.other_user)

        february = reading_stats.finished_readthroughs(
            self.user, datetime.date(2026, 2, 1), datetime.date(2026, 2, 28)
        )
        march = reading_stats.finished_readthroughs(
            self.user, datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)
        )
        self.assertEqual(list(february), [late])
        self.assertEqual(list(march), [march_first])

    def test_finished_readthroughs_queries(self):
        """books and authors are loaded with a fixed number of queries"""
        for day in range(1, 6):
            self.finish(local(2026, 3, day))
        with self.assertNumQueries(2):
            readthroughs = reading_stats.finished_readthroughs(
                self.user, datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)
            )
            alt_texts = [r.book.alt_text for r in readthroughs]
        self.assertEqual(len(alt_texts), 5)

    def test_pages_from_progress(self):
        """estimate pages read from positions in the book"""
        pages = reading_stats.pages_from_progress
        self.assertEqual(pages(120, 50, ProgressMode.PAGE, 300), 70)
        self.assertEqual(pages(30, None, ProgressMode.PAGE, None), 30)
        self.assertEqual(pages(40, 50, ProgressMode.PAGE, 300), 0)
        self.assertEqual(pages(30, 10, ProgressMode.PERCENT, 300), 60)
        self.assertEqual(pages(30, 10, ProgressMode.PERCENT, None), 0)
        self.assertEqual(pages(150, None, ProgressMode.PERCENT, 200), 200)

    def test_progress_by_day(self):
        """updates are grouped by local day, with pages since the last update"""
        readthrough = self.reading()
        # before the range: still used as the starting point
        self.progress(readthrough, 10, local(2026, 3, 31, 12))
        self.progress(readthrough, 50, local(2026, 4, 1, 9))
        self.progress(readthrough, 80, local(2026, 4, 1, 22))
        # 11pm on 2 April in New York is 3 April in UTC
        self.progress(readthrough, 100, utc(2026, 4, 3, 3))
        # percent updates are a separate sequence: 10% of 300 pages
        self.progress(readthrough, 10, local(2026, 4, 2, 8), mode=ProgressMode.PERCENT)

        with self.assertNumQueries(1):
            result = reading_stats.progress_by_day(
                self.user, datetime.date(2026, 4, 1), datetime.date(2026, 4, 30)
            )

        self.assertEqual(
            result,
            {
                datetime.date(2026, 4, 1): (2, 70),
                datetime.date(2026, 4, 2): (2, 50),
            },
        )

    def test_daily_activity(self):
        """every day is present, with finished books and progress"""
        readthrough = self.reading(book=self.no_pages_book)
        self.progress(readthrough, 25, local(2026, 4, 2, 12))
        finished = self.finish(local(2026, 4, 3))
        self.finish(local(2026, 4, 3), user=self.other_user)

        with self.assertNumQueries(3):
            days = reading_stats.daily_activity(
                self.user, datetime.date(2026, 4, 1), datetime.date(2026, 4, 5)
            )

        self.assertEqual(
            list(days),
            [datetime.date(2026, 4, day) for day in range(1, 6)],
        )
        self.assertFalse(days[datetime.date(2026, 4, 1)].is_active)
        self.assertEqual(days[datetime.date(2026, 4, 2)].updates, 1)
        self.assertEqual(days[datetime.date(2026, 4, 2)].pages, 25)
        self.assertEqual(days[datetime.date(2026, 4, 3)].finished, [finished])
        self.assertEqual(days[datetime.date(2026, 4, 3)].level, 2)

    def test_day_activity_level(self):
        """more activity means a darker square"""
        day = reading_stats.DayActivity(date=datetime.date(2026, 1, 1))
        self.assertEqual(day.level, 0)
        day.updates = 1
        self.assertEqual(day.level, 1)
        day.updates = 3
        self.assertEqual(day.level, 3)
        day.updates = 5
        self.assertEqual(day.level, 4)

    def test_streaks(self):
        """current and best runs of active days"""
        start = datetime.date(2026, 4, 1)
        activity = {
            start + datetime.timedelta(days=offset): reading_stats.DayActivity(
                date=start + datetime.timedelta(days=offset)
            )
            for offset in range(10)
        }
        # active on 1-3 April, and 6-8 April
        for day in [1, 2, 3, 6, 7, 8]:
            activity[datetime.date(2026, 4, day)].updates = 1

        # nothing yet on 9 April, but yesterday counts
        self.assertEqual(
            reading_stats.streaks(activity, datetime.date(2026, 4, 9)), (3, 3)
        )
        # two quiet days in a row ends the streak
        self.assertEqual(
            reading_stats.streaks(activity, datetime.date(2026, 4, 10)), (0, 3)
        )
        activity[datetime.date(2026, 4, 9)].updates = 1
        self.assertEqual(
            reading_stats.streaks(activity, datetime.date(2026, 4, 9)), (4, 4)
        )

    def test_totals_by_month(self):
        """books and pages per month, counting books with no page count"""
        self.finish(local(2026, 1, 5))
        self.finish(local(2026, 1, 20), book=self.no_pages_book)
        self.finish(local(2026, 3, 1))
        self.finish(local(2025, 12, 31))
        self.finish(local(2026, 1, 5), user=self.other_user)

        with self.assertNumQueries(1):
            months = reading_stats.totals_by_month(self.user, 2026)

        self.assertEqual(len(months), 12)
        self.assertEqual(months[0], reading_stats.PeriodTotal(2, 300, 1))
        self.assertEqual(months[1], reading_stats.PeriodTotal())
        self.assertEqual(months[2], reading_stats.PeriodTotal(1, 300, 0))

    def test_totals_by_year(self):
        """all years from first to last, including empty ones"""
        self.assertEqual(reading_stats.totals_by_year(self.user), {})
        self.finish(local(2023, 6, 1))
        self.finish(local(2026, 6, 1))
        self.finish(local(2026, 7, 1), book=self.no_pages_book)

        years = reading_stats.totals_by_year(self.user)

        self.assertEqual(list(years), [2023, 2024, 2025, 2026])
        self.assertEqual(years[2023], reading_stats.PeriodTotal(1, 300, 0))
        self.assertEqual(years[2024], reading_stats.PeriodTotal())
        self.assertEqual(years[2026], reading_stats.PeriodTotal(2, 300, 1))

    def test_cumulative_books_by_month(self):
        """running totals per year, stopping at the current month"""
        self.finish(local(2025, 1, 10))
        self.finish(local(2025, 3, 10))
        self.finish(local(2026, 2, 10))
        self.finish(local(2026, 2, 11))

        result = reading_stats.cumulative_books_by_month(
            self.user, [2026, 2025], today=datetime.date(2026, 4, 15)
        )

        self.assertEqual(list(result), [2025, 2026])
        self.assertEqual(result[2025], [1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2])
        self.assertEqual(
            result[2026], [0, 2, 2, 2, None, None, None, None, None, None, None, None]
        )
        self.assertEqual(reading_stats.cumulative_books_by_month(self.user, []), {})

    def test_year_summary_current_year(self):
        """summary numbers for the year so far"""
        today = datetime.date(2026, 4, 10)
        models.AnnualGoal.objects.create(user=self.user, year=2026, goal=10)
        self.finish(local(2026, 1, 10), start_date=local(2026, 1, 1))
        self.finish(
            local(2026, 4, 9), book=self.no_pages_book, start_date=local(2026, 4, 5)
        )
        # last year: one before the same date, one after it
        self.finish(local(2025, 4, 1))
        self.finish(local(2025, 4, 20))
        readthrough = self.reading()
        self.progress(readthrough, 20, local(2026, 4, 8, 12))
        self.progress(readthrough, 40, local(2026, 4, 10, 12))

        # finished books (2: books and their authors), progress, the year's
        # finish dates, and the goal
        with self.assertNumQueries(5):
            summary = reading_stats.year_summary(self.user, 2026, today=today)

        self.assertEqual(summary.books, 2)
        self.assertEqual(summary.pages, 300)
        self.assertEqual(summary.books_without_pages, 1)
        # 300 pages over 100 days (1 January to 10 April)
        self.assertEqual(summary.pages_per_day, 3.0)
        # 9 days and 4 days
        self.assertEqual(summary.average_days_per_book, 6.5)
        self.assertEqual(summary.average_pages_per_book, 300.0)
        self.assertEqual(summary.goal, 10)
        self.assertEqual(summary.goal_percent, 20)
        self.assertEqual(summary.books_previous_year_to_date, 1)
        # 10 Jan, 8, 9 and 10 April
        self.assertEqual(summary.active_days, 4)
        self.assertEqual(summary.current_streak, 3)
        self.assertEqual(summary.best_streak, 3)

    def test_year_summary_past_and_future_years(self):
        """past years use the whole year; future years are empty"""
        today = datetime.date(2026, 4, 10)
        self.finish(local(2025, 12, 31))

        past = reading_stats.year_summary(self.user, 2025, today=today)
        self.assertEqual(past.books, 1)
        self.assertEqual(past.pages_per_day, round(300 / 365, 1))
        self.assertEqual(past.current_streak, 0)
        self.assertEqual(past.best_streak, 1)
        self.assertIsNone(past.goal)
        self.assertIsNone(past.goal_percent)
        self.assertIsNone(past.average_days_per_book)

        future = reading_stats.year_summary(self.user, 2027, today=today)
        self.assertEqual(future.books, 0)
        self.assertEqual(future.pages_per_day, 0.0)
        self.assertEqual(future.active_days, 0)
        self.assertEqual(future.books_previous_year_to_date, 0)
