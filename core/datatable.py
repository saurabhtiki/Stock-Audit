"""Reusable server-side table: a filter on each column, sort, pages and CSV download.

How to use it in a view:

    table = DataTable(
        request.GET,
        Item.objects.filter(company=company),
        [Column("code", "Item code"), Column("uom", "UoM", type=CHOICE)],
        table_id="item-table",
        default_sort=("code",),
        base_url=request.path,
    )
    if table.wants_csv:
        return table.csv_response()

Everything the user picks lives in the page URL, e.g.
``?uom=KG&uom=PCS&erp_qty_min=10&sort=-erp_value&per_page=50&page=2``,
so refresh, Back and shared links all show the same rows.
"""

import csv
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

from django.core.exceptions import FieldDoesNotExist
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.http import StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.functional import cached_property

logger = logging.getLogger(__name__)

TEXT, CHOICE, NUMBER, DATE = "text", "choice", "number", "date"
BLANK = "__blank__"  # tick-list value meaning "the cell is empty"
PAGE_SIZES = (25, 50, 100, 200)
CSV_CHUNK = 2000
# A plain number, or one with thousand separators in Indian (1,00,000) or western (100,000) style.
NUMBER_PATTERN = re.compile(r"[+-]?(\d+|\d{1,3}(,\d{2})*,\d{3})?(\.\d+)?")
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class Column:
    name: str  # model field or annotation
    label: str
    type: str = TEXT  # TEXT, CHOICE, NUMBER or DATE
    choices: dict | None = None  # value -> label shown in the tick list and the CSV
    sortable: bool = True
    filterable: bool = True
    csv: bool = True
    # Optional: how to write the cell in the CSV, given the row (e.g. a dict as readable text).
    csv_value: Callable | None = None

    @property
    def align_right(self) -> bool:
        return self.type == NUMBER

    @property
    def is_range(self) -> bool:
        return self.type in (NUMBER, DATE)

    @property
    def param_names(self) -> tuple[str, ...]:
        if self.is_range:
            return (f"{self.name}_min", f"{self.name}_max")
        return (self.name,)

    def label_for(self, value) -> str:
        if value in (None, ""):
            return "(Blank)"
        if self.choices and value in self.choices:
            return str(self.choices[value])
        return str(value)


def formula_safe(text: str) -> str:
    """Stop Excel from running a cell that starts like a formula (e.g. a name typed as =1+1)."""
    if text.startswith(FORMULA_STARTS) and not NUMBER_PATTERN.fullmatch(text):
        return "'" + text
    return text


def _csv_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")  # plain number, e.g. 12500.50, so Excel can add it up
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value)
    return formula_safe(text)


class _Echo:
    """File-like object for csv.writer that hands each line straight back (for streaming)."""

    def write(self, value):
        return value


class DataTable:
    def __init__(
        self,
        params,
        queryset,
        columns,
        *,
        table_id: str,
        default_sort=("pk",),
        search_fields=(),
        search_placeholder="Search",
        shortcuts=(),
        base_url="",
        noun="rows",
        keep=(),
        default_per_page=PAGE_SIZES[0],
        push_url=True,
        csv_params=(),
    ):
        self.params = params
        # Extra URL pairs on the CSV link only, e.g. ("part", "overdue") when one page has
        # several downloads and the view must tell them apart.
        self.csv_params = tuple(csv_params)
        self.default_per_page = default_per_page
        # False for a table inside another page (e.g. a dashboard expander): its clicks
        # must not change the browser address, or a refresh would show only the table.
        self.push_url = "true" if push_url else "false"
        # Outside settings carried in every link, e.g. the dashboard's Branch / month filters.
        self.keep = tuple(keep)
        self.base_queryset = queryset
        self.columns = list(columns)
        self.by_name = {column.name: column for column in self.columns}
        self.table_id = table_id
        self.default_sort = tuple(default_sort)
        self.search_fields = tuple(search_fields)
        self.search_placeholder = search_placeholder
        self.base_url = base_url
        self.noun = noun
        self.errors: list[str] = []
        self.search = params.get("q", "").strip() if self.search_fields else ""
        self.conditions = self._read_filters()  # column name -> Q
        self.sort = self._read_sort()
        self.per_page = self._read_per_page()
        self._shortcuts = shortcuts

    # ---------- reading the URL ----------

    def _read_filters(self) -> dict:
        conditions = {}
        for column in self.columns:
            if not column.filterable:
                continue
            try:
                condition = self._condition_for(column)
            except ValueError as error:
                logger.info(
                    "Table %s: bad filter value for %s: %s", self.table_id, column.name, error
                )
                self.errors.append(str(error))
                continue
            if condition is not None:
                conditions[column.name] = condition
        return conditions

    def _condition_for(self, column):
        name = column.name
        if column.type == TEXT:
            text = self.params.get(name, "").strip()
            return Q(**{f"{name}__icontains": text}) if text else None
        if column.type == CHOICE:
            values = self._picked(column)
            if not values:
                return None
            condition = Q(**{f"{name}__in": [value for value in values if value != BLANK]})
            if BLANK in values:
                condition |= Q(**{name: ""}) | Q(**{f"{name}__isnull": True})
            return condition
        condition = Q()
        is_datetime = isinstance(self._field(name), models.DateTimeField)
        field = name + ("__date" if is_datetime else "")
        for suffix, lookup in (("_min", "gte"), ("_max", "lte")):
            raw = self.params.get(name + suffix, "").strip()
            if raw:
                condition &= Q(**{f"{field}__{lookup}": self._parse(column, raw)})
        return condition or None

    def _picked(self, column) -> list[str]:
        return [value for value in self.params.getlist(column.name) if value != ""]

    def _parse(self, column, raw: str):
        if column.type == NUMBER:
            # The pattern also refuses NaN / Infinity and a decimal comma like 1,5 (not 15).
            if not NUMBER_PATTERN.fullmatch(raw) or not any(char.isdigit() for char in raw):
                raise ValueError(
                    f"“{raw}” is not a number, so the {column.label} filter was skipped."
                )
            return Decimal(raw.replace(",", ""))
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError(
                f"“{raw}” is not a date (use YYYY-MM-DD), so the {column.label} filter was skipped."
            )

    def _field(self, name: str):
        """The model field or annotation output field behind a column (None if unknown)."""
        annotation = self.base_queryset.query.annotations.get(name)
        if annotation is not None:
            return annotation.output_field
        try:
            return self.base_queryset.model._meta.get_field(name)
        except FieldDoesNotExist:
            return None

    def _read_sort(self) -> str:
        raw = self.params.get("sort", "")
        column = self.by_name.get(raw.lstrip("-"))
        return raw if column and column.sortable else ""

    def _read_per_page(self) -> int:
        raw = self.params.get("per_page", "")
        return int(raw) if raw.isdecimal() and int(raw) in PAGE_SIZES else self.default_per_page

    # ---------- the rows ----------

    def _search_condition(self):
        condition = Q()
        for field in self.search_fields:
            condition |= Q(**{f"{field}__icontains": self.search})
        return condition

    def _filtered_except(self, skip=None):
        queryset = self.base_queryset
        if self.search:
            queryset = queryset.filter(self._search_condition())
        for name, condition in self.conditions.items():
            if name != skip:
                queryset = queryset.filter(condition)
        return queryset

    @cached_property
    def filtered(self):
        """Rows that pass the search box and every column filter (not sorted)."""
        return self._filtered_except()

    @cached_property
    def ordered(self):
        if not self.sort:
            return self.filtered.order_by(*self.default_sort, "pk")
        name = self.sort.lstrip("-")
        descending = self.sort.startswith("-")
        is_text = isinstance(self._field(name), (models.CharField, models.TextField))
        expression = Lower(name) if is_text else models.F(name)  # A→Z ignoring case
        expression = expression.desc(nulls_last=True) if descending else expression.asc()
        return self.filtered.order_by(expression, "pk")

    @cached_property
    def page(self):
        return Paginator(self.ordered, self.per_page).get_page(self.params.get("page"))

    @cached_property
    def has_any_rows(self) -> bool:
        return self.page.paginator.count > 0 or self.base_queryset.exists()

    # ---------- links ----------

    def _state(self, drop=(), add=()) -> list[tuple[str, str]]:
        """Current table settings as URL pairs, without `drop` names, plus `add` pairs."""
        names = [*self.keep, "q", "sort", "per_page"] + [
            name for column in self.columns for name in column.param_names
        ]
        pairs = [
            (name, value)
            for name in names
            if name not in drop
            for value in self.params.getlist(name)
            if value != ""
        ]
        return pairs + list(add)

    def url(self, drop=(), add=()) -> str:
        return "?" + urlencode(self._state(drop, add))

    def hidden_fields(self, drop=()) -> list[tuple[str, str]]:
        """Settings a small form must resend so it changes only its own part."""
        return self._state(drop=set(drop) | {"page"})

    @property
    def filter_query(self) -> str:
        """Only the search and filters (no sort, page size or page), e.g. for "select all"."""
        return urlencode(self._state(drop={"sort", "per_page"}))

    @property
    def csv_url(self) -> str:
        return self.url(add=[*self.csv_params, ("export", "csv")])

    @property
    def search_hidden(self):
        return self.hidden_fields(drop={"q"})

    @property
    def per_page_hidden(self):
        return self.hidden_fields(drop={"per_page"})

    # ---------- what the templates show ----------

    @cached_property
    def header(self) -> list[dict]:
        cells = []
        for column in self.columns:
            state = None
            if self.sort.lstrip("-") == column.name:
                state = "descending" if self.sort.startswith("-") else "ascending"
            next_sort = {None: column.name, "ascending": "-" + column.name}.get(state)
            cells.append(
                {
                    "column": column,
                    "sort_state": state or "none",
                    "sort_url": self.url(
                        drop={"sort", "page"}, add=[("sort", next_sort)] if next_sort else ()
                    ),
                    "sort_tip": {
                        None: f"Sort by {column.label}, A→Z / low→high",
                        "ascending": f"Sort by {column.label}, Z→A / high→low",
                        "descending": "Back to the normal order",
                    }[state],
                    "is_filtered": column.name in self.conditions,
                    "hidden": self.hidden_fields(drop=column.param_names),
                    "clear_url": self.url(drop={*column.param_names, "page"}),
                    "value": self.params.get(column.name, ""),
                    "min": self.params.get(f"{column.name}_min", ""),
                    "max": self.params.get(f"{column.name}_max", ""),
                    "options": self._options(column) if column.type == CHOICE else [],
                }
            )
        return cells

    def _value_counts(self, column) -> dict:
        """{value key: rows with that value}, given every filter except this column's."""
        counts = (
            self._filtered_except(skip=column.name)
            .order_by()
            .values(column.name)
            # distinct: a row joined to many lines / queries must still count once
            .annotate(row_count=Count("pk", distinct=True))
        )
        merged: dict = {}
        for row in counts:
            key = _option_key(row[column.name])
            merged[key] = merged.get(key, 0) + row["row_count"]
        return merged

    def _options(self, column) -> list[dict]:
        """Tick-list values with how many rows each would show, given the other filters."""
        picked = set(self._picked(column))
        merged = self._value_counts(column)
        for key in picked - merged.keys():
            merged[key] = 0  # keep a ticked value visible even when nothing matches it now
        options = [
            {
                "value": key,
                "label": column.label_for(None if key == BLANK else key),
                "count": count,
                "checked": key in picked,
            }
            for key, count in merged.items()
        ]
        return sorted(
            options, key=lambda option: (option["value"] == BLANK, option["label"].lower())
        )

    @cached_property
    def chips(self) -> list[dict]:
        chips = []
        if self.search:
            chips.append({"text": f"Search: “{self.search}”", "url": self.url(drop={"q", "page"})})
        for column in self.columns:
            if column.name not in self.conditions:
                continue
            chips.append(
                {
                    "text": f"{column.label}: {self._chip_text(column)}",
                    "url": self.url(drop={*column.param_names, "page"}),
                }
            )
        return chips

    def _chip_text(self, column) -> str:
        if column.type == TEXT:
            return f"contains “{self.params.get(column.name, '').strip()}”"
        if column.type == CHOICE:
            values = self._picked(column)
            return ", ".join(
                column.label_for(None if value == BLANK else value) for value in values
            )
        low = self.params.get(f"{column.name}_min", "").strip()
        high = self.params.get(f"{column.name}_max", "").strip()
        if low and high:
            return f"{low} – {high}"
        return f"≥ {low}" if low else f"≤ {high}"

    @property
    def active_count(self) -> int:
        return len(self.chips)

    @property
    def clear_all_url(self) -> str:
        return self.url(drop={name for name, _ in self._state()} - {"sort", "per_page", *self.keep})

    @cached_property
    def shortcuts(self) -> list[dict]:
        """One-click buttons that set one choice filter, e.g. Items: All / New / Inactive."""
        links = []
        for label, column_name, value, tip in self._shortcuts:
            picked = self.params.getlist(column_name)
            add = [(column_name, value)] if value else []
            links.append(
                {
                    "label": label,
                    "tip": tip,
                    "url": self.url(drop={column_name, "page"}, add=add),
                    "active": picked == ([value] if value else []),
                }
            )
        return links

    @property
    def page_sizes(self):
        return PAGE_SIZES

    @cached_property
    def pager(self) -> list[dict]:
        """Page links like 1 … 4 5 6 … 25 (a gap is {"gap": True})."""
        current, last = self.page.number, self.page.paginator.num_pages
        wanted = sorted({1, last, *range(max(1, current - 2), min(last, current + 2) + 1)})
        links, previous = [], 0
        for number in wanted:
            if number - previous > 1:
                links.append({"gap": True})
            links.append(
                {"number": number, "url": self.page_url(number), "current": number == current}
            )
            previous = number
        return links

    def page_url(self, number: int) -> str:
        return self.url(drop={"page"}, add=[("page", str(number))])

    @property
    def previous_url(self) -> str:
        return self.page_url(self.page.previous_page_number()) if self.page.has_previous() else ""

    @property
    def next_url(self) -> str:
        return self.page_url(self.page.next_page_number()) if self.page.has_next() else ""

    @property
    def csv_tip(self) -> str:
        count = self.page.paginator.count
        if self.active_count:
            return (
                f"Downloads all {count:,} {self.noun} that match your filters, not just this page"
            )
        return f"Downloads all {count:,} {self.noun}, not just this page"

    # ---------- CSV ----------

    @property
    def wants_csv(self) -> bool:
        return self.params.get("export") == "csv"

    def csv_response(self, filename: str | None = None) -> StreamingHttpResponse:
        """Every matching row (all pages), streamed so a big file doesn't freeze the server."""
        columns = [column for column in self.columns if column.csv]
        writer = csv.writer(_Echo())

        def lines():
            yield "﻿"  # lets Excel read accents and ₹ correctly
            yield writer.writerow([column.label for column in columns])
            for row in self._all_rows():
                yield writer.writerow([self._csv_cell(column, row) for column in columns])

        name = filename or f"{self.table_id}_{timezone.localdate().isoformat()}.csv"
        response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response

    def _all_rows(self):
        """Every matching row in order, read from the database in chunks."""
        return self.ordered.iterator(chunk_size=CSV_CHUNK)

    @staticmethod
    def _value(row, name: str):
        return getattr(row, name)

    def _csv_cell(self, column, row) -> str:
        if column.csv_value:
            return _csv_text(column.csv_value(row))
        value = self._value(row, column.name)
        key = str(value) if isinstance(value, bool) else value  # Yes/No columns use "True" keys
        if column.choices and key in column.choices:
            return _csv_text(str(column.choices[key]))
        return _csv_text(value)


def _option_key(value) -> str:
    """Tick-list key of a cell value: BLANK for an empty cell, else the value as text."""
    return BLANK if value in (None, "") else str(value)


class RowsTable(DataTable):
    """The same table (search, column filters, sort, pages, CSV, same templates) over rows
    worked out in Python, e.g. dashboard numbers that SQL can't produce. `rows` is a list of
    dicts, already in their normal order; columns name dict keys. Fine for a few thousand rows.
    """

    def _condition_for(self, column):
        """A test function `row -> bool` for this column's filter, or None when it's off."""
        name = column.name
        if column.type == TEXT:
            text = self.params.get(name, "").strip().lower()
            if not text:
                return None
            return lambda row: text in str(row.get(name) or "").lower()
        if column.type == CHOICE:
            picked = set(self._picked(column))
            if not picked:
                return None
            return lambda row: _option_key(row.get(name)) in picked
        bounds = []
        for suffix in ("_min", "_max"):
            raw = self.params.get(name + suffix, "").strip()
            bounds.append(self._parse(column, raw) if raw else None)
        low, high = bounds
        if low is None and high is None:
            return None
        return lambda row: (
            row.get(name) is not None
            and ((low is None or row[name] >= low) and (high is None or row[name] <= high))
        )

    def _field(self, name: str):
        return None

    def _filtered_except(self, skip=None):
        tests = [test for name, test in self.conditions.items() if name != skip]
        if self.search:
            needle = self.search.lower()
            tests.append(
                lambda row: any(
                    needle in str(row.get(field) or "").lower() for field in self.search_fields
                )
            )
        return [row for row in self.base_queryset if all(test(row) for test in tests)]

    @cached_property
    def ordered(self):
        if not self.sort:
            return self.filtered
        name = self.sort.lstrip("-")

        def sort_key(row):
            value = row.get(name)
            return value.lower() if isinstance(value, str) else value

        filled = [row for row in self.filtered if row.get(name) is not None]
        empty = [row for row in self.filtered if row.get(name) is None]
        # Empty cells always last, like the database table (nulls last).
        return sorted(filled, key=sort_key, reverse=self.sort.startswith("-")) + empty

    @cached_property
    def has_any_rows(self) -> bool:
        return bool(self.base_queryset)

    def _value_counts(self, column) -> dict:
        merged: dict = {}
        for row in self._filtered_except(skip=column.name):
            key = _option_key(row.get(column.name))
            merged[key] = merged.get(key, 0) + 1
        return merged

    def _all_rows(self):
        return self.ordered

    @staticmethod
    def _value(row, name: str):
        return row.get(name)


def rows_csv_response(filename: str, headings: list[str], rows) -> StreamingHttpResponse:
    """CSV of plain rows (lists of values), with the same number / date / formula rules as
    the table CSV. Used where the rows are worked out in Python, e.g. dashboard summaries."""
    writer = csv.writer(_Echo())

    def lines():
        yield "﻿"  # lets Excel read accents and ₹ correctly
        yield writer.writerow(headings)
        for row in rows:
            yield writer.writerow([_csv_text(value) for value in row])

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


YES_NO = {"True": "Yes", "False": "No"}
ACTIVE_INACTIVE = {"True": "Active", "False": "Inactive"}


def table_response(request, table, page_template: str, table_template: str, context: dict):
    """The usual answer for a list page: the CSV, only the table (an htmx click inside it),
    or the whole page. `context` must already hold the table."""
    if table.wants_csv:
        logger.info("%s downloaded CSV of %s", request.user.username, table.table_id)
        return table.csv_response()
    is_table_click = (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
        and request.headers.get("HX-Target") == table.table_id
    )
    response = render(request, table_template if is_table_click else page_template, context)
    patch_vary_headers(response, ["HX-Request", "HX-Target"])
    return response
