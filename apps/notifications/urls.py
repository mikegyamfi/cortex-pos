from django.urls import path
from . import views

app_name = 'notifications'

urlpatterns = [
    path('logs/', views.notification_log, name='log'),
]




