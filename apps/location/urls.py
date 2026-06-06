from django.urls import path
from . import views

app_name = 'location'

urlpatterns = [
    path('', views.location_list, name='list'),
    path('add/', views.location_create, name='create'),
    path('edit/<int:pk>/', views.location_edit, name='edit'),
]
