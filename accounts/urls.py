from django.urls import path

from accounts import auth_views, views

app_name = "accounts"
urlpatterns = [
    path("login/", auth_views.login_view, name="login"),
    path("logout/", auth_views.logout_view, name="logout"),
    path("password/", auth_views.password_change_view, name="password_change"),
    path("profile/", views.profile_view, name="profile"),
    path("profile/photo/remove/", views.profile_photo_remove, name="profile_photo_remove"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_detail, name="user_detail"),
    path("users/<int:pk>/toggle/", views.user_toggle, name="user_toggle"),
    path("users/<int:pk>/reset-password/", views.user_reset_password, name="user_reset_password"),
    path("users/<int:pk>/companies/add/", views.membership_add, name="membership_add"),
    path("memberships/<int:pk>/role/", views.membership_role, name="membership_role"),
    path(
        "memberships/<int:pk>/department/",
        views.membership_department,
        name="membership_department",
    ),
    path("memberships/<int:pk>/toggle/", views.membership_toggle, name="membership_toggle"),
    path("memberships/<int:pk>/rights/", views.rights_grid_view, name="rights_grid"),
    path("memberships/<int:pk>/rights/copy/", views.rights_copy, name="rights_copy"),
]
