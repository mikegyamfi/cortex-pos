from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    # Dashboard
    path('', views.inventory_dashboard, name='dashboard'),

    # Operations
    path('receive/', views.receive_stock, name='receive_stock'),
    path('transfer/create/', views.create_transfer, name='create_transfer'),

    path('batch/<int:pk>/', views.batch_detail, name='batch_detail'),

    path('transfers/<int:pk>/view/', views.transfer_detail, name='transfer_detail'),

    # History & Processing
    path('transfers/', views.transfer_list, name='transfer_list'),
    path('transfers/<int:pk>/process/', views.process_transfer, name='process_transfer'),  # Warehouse action
    path('transfers/<int:pk>/receive/', views.receive_transfer, name='receive_transfer'),  # Shop action (New)

    # Adjustments & Alerts
    path('adjustments/', views.stock_adjustments, name='adjustments'),
    path('alerts/', views.expiry_alerts, name='expiry_alerts'),
]