from django.urls import path

from audits import count_views, query_views, views

app_name = "audits"
urlpatterns = [
    path("audits/", views.audit_list, name="audit_list"),
    path("audits/new/", views.audit_create, name="audit_create"),
    path("audits/erp-template/", views.erp_template, name="erp_template"),
    path("audits/<int:pk>/", views.audit_detail, name="audit_detail"),
    path("audits/<int:pk>/edit/", views.audit_edit, name="audit_edit"),
    path("audits/<int:pk>/upload/", views.erp_upload, name="erp_upload"),
    path("audits/<int:pk>/freeze/", views.audit_freeze, name="audit_freeze"),
    path("audits/<int:pk>/cancel/", views.audit_cancel, name="audit_cancel"),
    path("uploads/<int:pk>/", views.upload_preview, name="upload_preview"),
    path("audits/<int:pk>/count-stats/", count_views.count_stats, name="count_stats"),
    path("audits/<int:pk>/count-sheet/", count_views.count_sheet, name="count_sheet"),
    path(
        "audits/<int:pk>/count-sheet/upload/",
        count_views.count_sheet_upload,
        name="count_sheet_upload",
    ),
    path("count-sheets/<int:pk>/", count_views.count_sheet_preview, name="count_sheet_preview"),
    path("audits/<int:pk>/remarks/", count_views.remark_panel, name="remark_panel"),
    path("audits/<int:pk>/checking-done/", count_views.checking_done, name="checking_done"),
    path("lines/<int:pk>/count/", count_views.line_count, name="line_count"),
    path("remarks/<int:pk>/edit/", count_views.remark_edit, name="remark_edit"),
    path("remarks/<int:pk>/delete/", count_views.remark_delete, name="remark_delete"),
    path("queries/", query_views.query_list, name="query_list"),
    path("queries/<int:pk>/", query_views.query_detail, name="query_detail"),
    path("queries/<int:pk>/reply/", query_views.query_reply, name="query_reply"),
    path("queries/<int:pk>/reply/edit/", query_views.query_reply_edit, name="query_reply_edit"),
    path("queries/<int:pk>/actions/", query_views.action_add, name="action_add"),
    path("queries/<int:pk>/close/", query_views.query_close, name="query_close"),
    path("queries/<int:pk>/reopen/", query_views.query_reopen, name="query_reopen"),
    path("actions/<int:pk>/<str:step>/", query_views.action_step, name="action_step"),
    path("items/", views.item_list, name="item_list"),
    path("items/new/", views.item_form, name="item_create"),
    path("items/review/", views.item_bulk_review, name="item_bulk_review"),
    path("items/<int:pk>/edit/", views.item_form, name="item_edit"),
    path("items/<int:pk>/toggle/", views.item_toggle, name="item_toggle"),
    path("items/<int:pk>/review/", views.item_review, name="item_review"),
]
