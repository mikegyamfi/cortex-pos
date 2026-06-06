from django import forms

from .models import Location


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ['name', 'location_type', 'address', 'phone_number', 'manager', 'is_active']
        widgets = {
            'address': forms.Textarea(attrs={'rows': 2}),
        }
