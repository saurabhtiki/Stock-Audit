from django.urls import path

from core import views

app_name = "core"
urlpatterns = [
    path("", views.home, name="home"),
    path("health/", views.health, name="health"),
    path("switch-company/", views.switch_company, name="switch_company"),
    path("companies/", views.company_list, name="company_list"),
    path("companies/new/", views.company_form, name="company_create"),
    path("companies/<int:pk>/edit/", views.company_form, name="company_edit"),
    path("companies/<int:pk>/toggle/", views.company_toggle, name="company_toggle"),
    path("branches/", views.branch_list, name="branch_list"),
    path("branches/new/", views.branch_form, name="branch_create"),
    path("branches/<int:pk>/edit/", views.branch_form, name="branch_edit"),
    path("branches/<int:pk>/toggle/", views.branch_toggle, name="branch_toggle"),
    path("departments/", views.department_list, name="department_list"),
    path("departments/new/", views.department_form, name="department_create"),
    path("departments/<int:pk>/edit/", views.department_form, name="department_edit"),
    path("departments/<int:pk>/toggle/", views.department_toggle, name="department_toggle"),
    path("activity/", views.activity_log, name="activity_log"),
]
