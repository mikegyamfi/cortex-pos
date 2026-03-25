from django.urls import path
from . import views

app_name = 'customers'

urlpatterns = [
    # Management
    path('', views.customer_list, name='list'),
    path('add/', views.customer_create, name='create'),
    path('edit/<int:pk>/', views.customer_edit, name='edit'),
    path('view/<int:pk>/', views.customer_detail, name='detail'),

    # API for POS Integration
    path('api/search/', views.api_search_customers, name='api_search'),
    path('api/create/', views.api_create_customer, name='api_create'),
]