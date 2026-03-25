from django.urls import path
from . import views

app_name = 'products'

urlpatterns = [
    path('', views.product_list, name='product_list'),
    path('add/', views.product_create, name='product_create'),
    path('view/<int:pk>/', views.product_detail, name='product_detail'), # New
    path('edit/<int:pk>/', views.product_edit, name='product_edit'),
    path('category/add/', views.quick_category_create, name='quick_category_create'),
]