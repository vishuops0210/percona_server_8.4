"""
Backup retention strategy.

A single place that defines where a backup goes and how long it is kept.
Both the minio upload plugin and the cleanup plugin use this, so the
strategy is defined once, not in two places.

The strategy is GFS: Grandfather, Father, Son.
  Son: daily fulls, kept for full_retention_days (30 days).
  Father: monthly fulls, the first full of each month, kept for
    monthly_retention_months (12 months).
  Grandfather: yearly fulls, the first full of January, kept for
    yearly_retention_years (0 means forever).

Config under "strategy" in config.json:
  full_retention_days: 30
  monthly_retention_months: 12
  yearly_retention_years: 0

The strategy answers two questions:
  dests_for(date): which MinIO folders should a full backup from this date
    go to? Returns a list like ["full-backups"] or
    ["full-backups", "monthly-backups"] or
    ["full-backups", "monthly-backups", "yearly-backups"].
  should_keep(date, all_dates, cutoff): should a backup from this date be
    kept, even if it is older than the cutoff? True if it is the first full
    of its month (within monthly retention) or the first full of January
    (within yearly retention, or forever).
"""

from datetime import datetime, timedelta


def dests_for(date, existing_months=None, existing_years=None):
    """Return the MinIO folders a full backup from this date should go to.

    existing_months: a set of (year, month) tuples that already have a monthly
      backup in MinIO. If the date's month is not in there, this backup is
      the first full of its month, so it goes to monthly-backups too.
    existing_years: a set of years that already have a yearly backup in
      MinIO. If the date's year is not in there and the month is January,
      this backup is the first full of the year, so it goes to
      yearly-backups too.

    If existing_months or existing_years is None, the caller is not checking
    for duplicates, so monthly and yearly are included based on date alone.
    """
    dests = ["full-backups"]
    if existing_months is None or (date.year, date.month) not in existing_months:
        dests.append("monthly-backups")
    if date.month == 1:
        if existing_years is None or date.year not in existing_years:
            dests.append("yearly-backups")
    return dests


def should_keep_monthly(date, all_dates, monthly_cutoff):
    """True if this date is the first full of its month and within monthly retention."""
    if date < monthly_cutoff:
        return False
    month_key = (date.year, date.month)
    month_dates = [d for d in all_dates if (d.year, d.month) == month_key]
    if not month_dates:
        return False
    return date == min(month_dates)


def should_keep_yearly(date, all_dates, yearly_cutoff):
    """True if this date is the first full of January and within yearly retention."""
    if date.month != 1:
        return False
    if yearly_cutoff is not None and date < yearly_cutoff:
        return False
    year_dates = [d for d in all_dates if d.year == date.year and d.month == 1]
    if not year_dates:
        return False
    return date == min(year_dates)


def monthly_cutoff(now, monthly_retention_months):
    """Return the cutoff date for monthly retention."""
    return now - timedelta(days=monthly_retention_months * 30)


def yearly_cutoff(now, yearly_retention_years):
    """Return the cutoff date for yearly retention, or None for forever."""
    if yearly_retention_years <= 0:
        return None
    return now - timedelta(days=yearly_retention_years * 365)
