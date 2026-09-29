from django.urls import path
from . import views, distributor_views

app_name = 'customers'

urlpatterns = [
    # Management
    path('', views.customer_list, name='list'),
    path('add/', views.customer_create, name='create'),
    path('edit/<int:pk>/', views.customer_edit, name='edit'),
    path('view/<int:pk>/', views.customer_detail, name='detail'),

    # Distributors (customers on the distributor price list)
    path('distributors/', distributor_views.distributor_list, name='distributor_list'),
    path('distributors/add/', distributor_views.distributor_create, name='distributor_create'),
    path('distributors/<int:pk>/', distributor_views.distributor_detail, name='distributor_detail'),
    path('distributors/<int:pk>/edit/', distributor_views.distributor_edit, name='distributor_edit'),
    path('distributors/<int:pk>/sell/', distributor_views.distributor_sell, name='distributor_sell'),

    # API for POS Integration
    path('api/search/', views.api_search_customers, name='api_search'),
    path('api/create/', views.api_create_customer, name='api_create'),
]
