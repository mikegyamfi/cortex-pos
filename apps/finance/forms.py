from django import forms
from .models import Expense, ExpenseCategory


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ['category', 'amount', 'description', 'receipt_image', 'date_incurred', 'is_paid_from_till']
        widgets = {
            'date_incurred': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 2}),
            'category': forms.Select(attrs={'class': 'select2'}),
        }
        labels = {
            'is_paid_from_till': 'Paid directly from Cash Drawer?'
        }
