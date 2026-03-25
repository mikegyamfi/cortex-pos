from django import forms
from .models import User, UserProfile


class StaffCreationForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Temporary Password'}),
        help_text="The user will be forced to change this password on their first login."
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone_number', 'role', 'assigned_location',
                  'password']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        # Security: Force password change on first login
        user.requires_password_change = True
        if commit:
            user.save()
        return user


class StaffUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone_number', 'role', 'assigned_location',
                  'is_active']


class UserProfileForm(forms.ModelForm):
    """
    Handles HR and Profile data. All fields are strictly optional.
    """
    class Meta:
        model = UserProfile
        fields = [
            'date_of_birth', 'id_type', 'id_number', 'home_address',
            'emergency_contact_name', 'emergency_contact_phone', 'emergency_contact_relation',
            'date_joined', 'base_salary', 'id_document', 'contract_file', 'notes'
        ]
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
            'date_joined': forms.DateInput(attrs={'type': 'date'}),
            'home_address': forms.Textarea(attrs={'rows': 2}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }







