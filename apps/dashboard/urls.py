from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    # The main entry point.
    # This URL decides where the user goes based on their role.
    path('', views.dashboard_router, name='index'),

    # Explicit Dashboard Views (in case they want to navigate back)
    path('analytics/', views.owner_analytics, name='analytics'),
]