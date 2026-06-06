from django.urls import path
from . import views

app_name = 'sales'

urlpatterns = [
    # Main Point of Sale
    path('pos/', views.pos_view, name='pos'),
    path('pos/search/', views.product_search_api, name='product_search'),
    path('process/', views.process_sale, name='process_sale'),
    path('register/close/', views.close_register_view, name='close_register'),
    path('returns/', views.refund_list, name='returns'),
    path('arrears/', views.arrears_list, name='arrears'),
    path('arrears/log/', views.arrears_payment_log, name='arrears_log'),

    path('payment/add/<int:pk>/', views.add_payment, name='add_payment'),
    # Returns & Deliveries (NEW)
    path('refund/<int:pk>/', views.process_refund, name='refund'),
    path('deliveries/', views.delivery_management, name='deliveries'),
    path('deliveries/<int:pk>/', views.delivery_management, name='delivery_edit'),

    # History & Management
    path('history/', views.sale_list, name='list'),
    path('receipt/<int:pk>/', views.sale_detail, name='detail'),
    path('sessions/', views.session_list, name='sessions'),  # Cashier Shifts
    path('sessions/<int:pk>/', views.session_detail, name='session_detail'),

]








