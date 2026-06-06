from django.urls import path
from django.contrib.auth.views import LogoutView, PasswordResetView, PasswordResetDoneView, PasswordResetConfirmView, \
    PasswordResetCompleteView
from . import views

app_name = 'users'

urlpatterns = [
    path('login/', views.CustomLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(next_page='users:login'), name='logout'),

    # Forced Password Change (For new accounts)
    path('force-password-change/', views.ForcePasswordChangeView.as_view(), name='force_password_change'),

    # Self-service password change (any logged-in user)
    path('password-change/', views.SelfPasswordChangeView.as_view(), name='password_change'),

    # Standard Password Reset (Forgot Password)
    path('password-reset/',
         PasswordResetView.as_view(
             template_name='users/password_reset_form.html',
             email_template_name='users/password_reset_email.html',
             subject_template_name='users/password_reset_subject.txt',
             success_url='/users/password-reset/done/'
         ),
         name='password_reset'),

    path('password-reset/done/',
         PasswordResetDoneView.as_view(template_name='users/password_reset_done.html'),
         name='password_reset_done'),

    path('reset/<uidb64>/<token>/',
         PasswordResetConfirmView.as_view(
             template_name='users/password_reset_confirm.html',
             success_url='/users/reset/done/'
         ),
         name='password_reset_confirm'),

    path('reset/done/',
         PasswordResetCompleteView.as_view(template_name='users/password_reset_complete.html'),
         name='password_reset_complete'),

    # Staff Management
    path('staff/', views.staff_list, name='list'),
    path('staff/add/', views.staff_create, name='create'),
    path('staff/edit/<int:pk>/', views.staff_edit, name='edit'),
    path('staff/reset-password/<int:pk>/', views.staff_reset_password, name='reset_password'),
]




