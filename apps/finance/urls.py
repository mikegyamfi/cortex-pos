from django.urls import path
from . import views

app_name = 'finance'

urlpatterns = [
    # Expenses
    path('expenses/', views.expense_list, name='expenses_list'),
    path('expenses/add/', views.expense_create, name='expense_create'),
    path('expenses/<int:pk>/approve/', views.expense_approve, name='expense_approve'),

    # Reports
    path('profit-loss/', views.profit_loss_view, name='profit_loss'),
    path('tax-reports/', views.tax_report_view, name='tax_report'),

    # Targets
    path('targets/', views.target_list, name='targets'),
    path('targets/add/', views.target_create, name='target_create'),
    path('targets/<int:pk>/delete/', views.target_delete, name='target_delete'),
]