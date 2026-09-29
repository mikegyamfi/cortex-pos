from django import forms
from .models import Customer


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['first_name', 'last_name', 'phone_number', 'email', 'address', 'accepts_marketing_sms']
        widgets = {
            'address': forms.Textarea(attrs={'rows': 2}),
        }


class DistributorForm(forms.ModelForm):
    """
    A distributor is a Customer flagged is_distributor, so everything that
    already works for customers — debt, arrears, statements, SMS — works for
    them too. This form just collects the business-facing details.
    """

    class Meta:
        model = Customer
        fields = [
            'company_name', 'contact_person', 'first_name', 'last_name',
            'phone_number', 'backup_phone', 'email', 'address', 'notes',
            'accepts_marketing_sms',
        ]
        labels = {
            'company_name': 'Distributor / Business name',
            'first_name': 'First name (contact)',
            'last_name': 'Last name (contact)',
            'phone_number': 'Primary phone',
            'backup_phone': 'Backup phone',
            'address': 'Location / address',
        }
        widgets = {
            'address': forms.Textarea(attrs={'rows': 2}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['company_name'].required = True
        self.fields['phone_number'].required = True

    def clean_phone_number(self):
        """Phone is unique per shop — say so plainly instead of a 500."""
        phone = (self.cleaned_data.get('phone_number') or '').strip()
        location = getattr(self.instance, 'location', None)
        clash = Customer.objects.filter(phone_number=phone, location=location)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                "That phone number is already on file for this shop."
            )
        return phone

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.is_distributor = True
        if commit:
            obj.save()
        return obj
