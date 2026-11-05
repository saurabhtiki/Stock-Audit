from django.urls import path

from audits import views

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
    path("items/", views.item_list, name="item_list"),
    path("items/new/", views.item_form, name="item_create"),
    path("items/review/", views.item_bulk_review, name="item_bulk_review"),
    path("items/<int:pk>/edit/", views.item_form, name="item_edit"),
    path("items/<int:pk>/toggle/", views.item_toggle, name="item_toggle"),
    path("items/<int:pk>/review/", views.item_review, name="item_review"),
]
