"""Phase 2.2: the reusable server-side table (django_datatable) and the two screens using it."""

from datetime import date
from decimal import Decimal

import pytest
from django.http import QueryDict
from django.urls import reverse
from django_datatable import CHOICE, DATE, NUMBER, Column, DataTable

from audits.models import Audit, AuditLine, Item

from .conftest import client_for
from .test_audits import grant, manager, priya  # noqa: F401  (fixtures)


def add_line(audit, code, description="", uom="", qty="1", rate="1"):
    item = Item.objects.create(company=audit.branch.company, code=code, uom=uom or "PCS")
    return AuditLine.objects.create(
        audit=audit,
        item=item,
        item_code=code,
        description=description,
        uom=uom,
        erp_qty=Decimal(qty),
        rate=Decimal(rate),
        erp_value=Decimal(qty) * Decimal(rate),
    )


def make_table(query: str, queryset, columns, **options):
    options.setdefault("table_id", "test")
    return DataTable(QueryDict(query), queryset, columns, **options)


@pytest.fixture
def audit(priya, branches):  # noqa: F811
    return Audit.objects.create(
        branch=branches[0],
        title="Aug26",
        audit_date=date(2026, 8, 30),
        stock_as_on=date(2026, 8, 29),
        created_by=priya,
    )


@pytest.fixture
def lines(audit):
    rows = [
        ("A100", "Steel bolt", "PCS", "100", "2.50"),
        ("A200", "Paint 1L", "LTR", "5", "120"),
        ("A300", "steel NUT", "PCS", "40", "1.25"),
        ("A400", "=cmd|calc", "", "12", "10"),
        ("A500", "Copper wire", "KG", "-3", "800"),
    ]
    return [add_line(audit, *row) for row in rows]


LINE_COLUMNS = [
    Column("item_code", "Item No"),
    Column("description", "Description"),
    Column("uom", "UoM", type=CHOICE),
    Column("erp_qty", "ERP Qty", type=NUMBER),
    Column("erp_value", "ERP value", type=NUMBER, csv=False),
]


def codes(table):
    return [line.item_code for line in table.page]


def lines_table(audit, query=""):
    return make_table(query, audit.lines.all(), LINE_COLUMNS, default_sort=("item_code",))


# ---------- filters ----------


def test_text_filter_is_contains_and_ignores_case(audit, lines):
    assert codes(lines_table(audit, "description=STEEL")) == ["A100", "A300"]


def test_choice_filter_values_are_or(audit, lines):
    assert codes(lines_table(audit, "uom=LTR&uom=KG")) == ["A200", "A500"]


def test_blank_choice_finds_empty_cells(audit, lines):
    assert codes(lines_table(audit, "uom=__blank__")) == ["A400"]
    assert codes(lines_table(audit, "uom=__blank__&uom=KG")) == ["A400", "A500"]


def test_number_range_both_ends_and_one_end(audit, lines):
    assert codes(lines_table(audit, "erp_qty_min=5&erp_qty_max=40")) == ["A200", "A300", "A400"]
    assert codes(lines_table(audit, "erp_qty_min=40")) == ["A100", "A300"]
    assert codes(lines_table(audit, "erp_qty_max=0")) == ["A500"]


def test_bad_number_is_skipped_with_a_message(audit, lines):
    table = lines_table(audit, "erp_qty_min=abc")
    assert len(codes(table)) == 5
    assert table.errors == ["“abc” is not a number, so the ERP Qty filter was skipped."]


def test_filters_on_two_columns_are_and(audit, lines):
    assert codes(lines_table(audit, "uom=PCS&erp_qty_min=50")) == ["A100"]


def test_date_range_on_date_and_datetime_fields(priya, branches):  # noqa: F811
    for title, day in [
        ("Jun", date(2026, 6, 1)),
        ("Jul", date(2026, 7, 1)),
        ("Aug", date(2026, 8, 1)),
    ]:
        Audit.objects.create(
            branch=branches[0], title=title, audit_date=day, stock_as_on=day, created_by=priya
        )
    columns = [Column("title", "Title"), Column("audit_date", "Date", type=DATE)]
    table = make_table(
        "audit_date_min=2026-06-15", Audit.objects.all(), columns, default_sort=("audit_date",)
    )
    assert [audit.title for audit in table.page] == ["Jul", "Aug"]
    table = make_table(
        "audit_date_max=2026-07-01", Audit.objects.all(), columns, default_sort=("audit_date",)
    )
    assert [audit.title for audit in table.page] == ["Jun", "Jul"]
    bad = make_table("audit_date_min=1/7/26", Audit.objects.all(), columns)
    assert "is not a date" in bad.errors[0] and bad.page.paginator.count == 3
    # created_at is a DateTimeField: the range compares the day only.
    today = date.today().isoformat()
    created = [Column("created_at", "Created", type=DATE)]
    table = make_table(
        f"created_at_min={today}&created_at_max={today}", Audit.objects.all(), created
    )
    assert table.page.paginator.count == 3


def test_tick_list_counts_use_the_other_filters(audit, lines):
    table = lines_table(audit, "uom=PCS&erp_qty_min=10")
    uom = next(cell for cell in table.header if cell["column"].name == "uom")
    options = {option["value"]: (option["count"], option["checked"]) for option in uom["options"]}
    # Qty ≥ 10 leaves A100, A300 (PCS), A400 (blank), A500 is -3, A200 is 5.
    assert options == {"PCS": (2, True), "__blank__": (1, False)}
    assert [option["label"] for option in uom["options"]][-1] == "(Blank)"


def test_search_box_looks_in_its_fields(audit, lines):
    table = make_table(
        "q=wire", audit.lines.all(), LINE_COLUMNS, search_fields=("item_code", "description")
    )
    assert codes(table) == ["A500"]
    assert table.chips[0]["text"] == "Search: “wire”"


# ---------- sort and pages ----------


def test_sort_up_down_and_back_to_normal(audit, lines):
    table = lines_table(audit, "sort=erp_qty")
    assert codes(table) == ["A500", "A200", "A400", "A300", "A100"]
    qty = next(cell for cell in table.header if cell["column"].name == "erp_qty")
    assert qty["sort_url"] == "?sort=-erp_qty"
    table = lines_table(audit, "sort=-erp_qty")
    assert codes(table)[0] == "A100"
    qty = next(cell for cell in table.header if cell["column"].name == "erp_qty")
    assert qty["sort_url"] == "?"  # third click: normal order
    assert codes(lines_table(audit, "sort=nope")) == ["A100", "A200", "A300", "A400", "A500"]


def test_text_sort_ignores_case(audit, lines):
    assert codes(lines_table(audit, "sort=description"))[:3] == ["A400", "A500", "A200"]


def test_page_size_and_page_past_the_end(audit):
    for number in range(60):
        add_line(audit, f"X{number:03}")
    assert lines_table(audit).per_page == 20
    assert lines_table(audit, "per_page=50").per_page == 50
    assert lines_table(audit, "per_page=999").per_page == 20
    last = lines_table(audit, "page=99").page
    assert last.number == 3 and codes(lines_table(audit, "page=99"))[-1] == "X059"
    pager = lines_table(audit, "page=2").pager
    assert [link.get("number") for link in pager] == [1, 2, 3]


def test_links_keep_filters_and_drop_the_page(audit, lines):
    table = lines_table(audit, "uom=PCS&page=2&sort=-erp_qty")
    assert table.page_url(3) == "?sort=-erp_qty&uom=PCS&page=3"
    assert table.chips[0]["url"] == "?sort=-erp_qty"
    assert table.clear_all_url == "?sort=-erp_qty"
    assert table.filter_query == "uom=PCS"


# ---------- CSV ----------


def csv_text(response):
    return b"".join(response.streaming_content).decode("utf-8-sig")


def test_csv_has_every_matching_row_plain_numbers_and_formula_guard(audit, lines):
    for number in range(30):
        add_line(audit, f"Z{number:02}", uom="PCS", qty="1234.5")
    table = lines_table(audit, "uom=PCS&uom=__blank__&per_page=25")
    rows = csv_text(table.csv_response()).splitlines()
    assert rows[0] == "Item No,Description,UoM,ERP Qty"  # erp_value has csv=False
    assert len(rows) == 1 + 33  # every page, not just 25
    assert "Z00,,PCS,1234.500" in rows
    assert "A400,'=cmd|calc,,12.000" in rows


# ---------- the Items and Audit lines screens ----------


@pytest.fixture
def items(company_a, company_b):
    Item.objects.create(
        company=company_a, code="RM-1", description="Raw steel", uom="KG", is_new=True
    )
    Item.objects.create(
        company=company_a, code="RM-2", description="Raw paint", uom="LTR", is_new=True
    )
    Item.objects.create(company=company_a, code="FG-1", description="Finished bolt", uom="PCS")
    Item.objects.create(company=company_a, code="OLD-1", uom="KG", is_active=False)
    Item.objects.create(company=company_b, code="RM-9", uom="KG", is_new=True)


def item_codes(response):
    return [item.code for item in response.context["table"].page]


def test_items_search_shortcut_and_column_filter_work_together(manager, items):  # noqa: F811
    client = client_for(manager)
    url = reverse("audits:item_list")
    assert item_codes(client.get(url, {"q": "raw", "status": "new"})) == ["RM-1", "RM-2"]
    assert item_codes(client.get(url, {"q": "raw", "status": "new", "uom": "KG"})) == ["RM-1"]
    assert item_codes(client.get(url, {"status": "inactive"})) == ["OLD-1"]
    text = client.get(url, {"status": "new"}, HTTP_HX_REQUEST="true").content.decode()
    assert 'id="item-table"' in text and "<html" not in text


def test_items_other_company_never_shows_in_table_or_csv(manager, items):  # noqa: F811
    client = client_for(manager)
    url = reverse("audits:item_list")
    assert "RM-9" not in item_codes(client.get(url, {"uom": "KG"}))
    rows = csv_text(client.get(url, {"export": "csv"})).splitlines()
    assert rows[0] == "Item code,Description,UoM,Status"
    assert "RM-1,Raw steel,KG,New (not reviewed)" in rows
    assert len(rows) == 5 and not any("RM-9" in row for row in rows)


def test_bulk_review_all_matching_uses_column_filters(manager, items):  # noqa: F811
    client = client_for(manager)
    listing = client.get(reverse("audits:item_list"), {"uom": "KG"}, HTTP_HX_REQUEST="true")
    assert 'data-matching="1"' in listing.content.decode()  # RM-1 only (RM-9 is company B)
    response = client.post(
        reverse("audits:item_bulk_review"), {"all_matching": "1", "filters": "uom=KG"}
    )
    assert set(Item.objects.filter(is_new=True).values_list("code", flat=True)) == {"RM-2", "RM-9"}
    assert response.url.endswith("?uom=KG")


def test_history_restore_gets_the_whole_page(manager, items):  # noqa: F811
    response = client_for(manager).get(
        reverse("audits:item_list"),
        {"status": "new"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_HISTORY_RESTORE_REQUEST="true",
    )
    assert "<html" in response.content.decode()
    assert "HX-Request" in response["Vary"]


def test_audit_lines_screen_filters_and_csv(priya, audit, lines):  # noqa: F811
    client = client_for(priya)
    url = reverse("audits:audit_detail", args=[audit.pk])
    response = client.get(url, {"uom": "PCS", "sort": "-erp_qty"})
    assert [line.item_code for line in response.context["table"].page] == ["A100", "A300"]
    text = response.content.decode()
    assert "UoM: PCS" in text and "Downloads all 2 lines that match your filters" in text
    download = client.get(url, {"uom": "PCS", "export": "csv"})
    assert download["Content-Disposition"].startswith('attachment; filename="Aug26_erp_lines_')
    assert len(csv_text(download).splitlines()) == 3


# ---------- fixes from code review ----------


@pytest.mark.parametrize("value", ["nan", "Infinity", "-inf", "1e999", "1,5", "12,34,5", ","])
def test_odd_numbers_are_refused_not_crashing(audit, lines, value):
    table = lines_table(audit, f"erp_qty_min={value}")
    assert table.page.paginator.count == 5
    assert table.errors and "is not a number" in table.errors[0]


def test_thousand_separators_are_understood(audit, lines):
    assert codes(lines_table(audit, "erp_qty_max=1,000")) == [
        "A100",
        "A200",
        "A300",
        "A400",
        "A500",
    ]
    assert lines_table(audit, "erp_qty_min=1,00,000").page.paginator.count == 0
    assert codes(lines_table(audit, "erp_qty_min=-3.5&erp_qty_max=.5")) == ["A500"]


def test_csv_guard_covers_tab_and_carriage_return(audit):
    add_line(audit, "T1", description="\t=HYPERLINK(1)")
    add_line(audit, "T2", description="-12.5")
    text = csv_text(lines_table(audit).csv_response())
    assert "'\t=HYPERLINK(1)" in text
    assert "T2,-12.5," in text  # a real number is left alone


def test_empty_choice_value_adds_no_extra_option(audit, lines):
    table = lines_table(audit, "uom=&uom=KG")
    uom = next(cell for cell in table.header if cell["column"].name == "uom")
    assert [option["label"] for option in uom["options"]].count("(Blank)") == 1
    assert table.chips[0]["text"] == "UoM: KG"


def test_items_csv_has_a_readable_name(manager, items):  # noqa: F811
    response = client_for(manager).get(reverse("audits:item_list"), {"export": "csv"})
    assert 'filename="items_alpha-traders_' in response["Content-Disposition"]


def test_number_columns_are_right_aligned():
    assert Column("qty", "Qty", type=NUMBER).align_right
    assert not Column("code", "Code").align_right


def test_no_template_comment_leaks_into_the_page(manager, items):  # noqa: F811
    text = client_for(manager).get(reverse("audits:item_list")).content.decode()
    assert "{#" not in text and "Needs `table`" not in text
