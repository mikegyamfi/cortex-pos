from django.contrib.auth.views import LoginView, PasswordChangeView
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.contrib import messages

from .forms import StaffUpdateForm, StaffCreationForm, UserProfileForm
from apps.users.models import User, UserProfile


class CustomLoginView(LoginView):
    template_name = 'users/login.html'
    redirect_authenticated_user = True

    def get_success_url(self):
        user = self.request.user
        # Check if user is forced to change password
        if user.requires_password_change:
            return reverse_lazy('users:force_password_change')

        # Otherwise, standard dashboard routing
        return reverse_lazy('dashboard:index')


class ForcePasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    """
    View for users who MUST change their password (e.g. first login).
    """
    template_name = 'users/force_password_change.html'
    success_url = reverse_lazy('dashboard:index')

    def form_valid(self, form):
        # Once password is changed, flip the flag to False
        self.request.user.requires_password_change = False
        self.request.user.save()
        messages.success(self.request, "Password changed successfully. Welcome!")
        return super().form_valid(form)


@login_required
def staff_list(request):
    """
    List all staff members. Owner sees all, Manager sees their location.
    """
    if request.user.role not in ['OWNER', 'MANAGER']:
        messages.error(request, "Access denied.")
        return redirect('dashboard:index')

    if request.user.role == 'OWNER':
        staff = User.objects.all().select_related('assigned_location').order_by('-is_active', 'role')
    else:
        staff = User.objects.filter(assigned_location=request.user.assigned_location).order_by('-is_active', 'role')

    return render(request, 'users/staff_list.html', {'staff': staff})


@login_required
def staff_create(request):
    """
    Create a new staff account.
    """
    if request.user.role != 'OWNER':
        messages.error(request, "Only Owners can create new staff accounts.")
        return redirect('users:list')

    if request.method == 'POST':
        form = StaffCreationForm(request.POST)
        profile_form = UserProfileForm(request.POST, request.FILES)

        if form.is_valid() and profile_form.is_valid():
            user = form.save()
            profile = profile_form.save(commit=False)
            profile.user = user
            profile.save()
            messages.success(request,
                             f"Staff account for {user.username} created successfully. They must change their password on first login.")
            return redirect('users:list')
    else:
        form = StaffCreationForm()
        profile_form = UserProfileForm()

    return render(request, 'users/staff_form.html', {
        'form': form,
        'profile_form': profile_form,
        'title': 'Add New Staff'
    })


@login_required
def staff_edit(request, pk):
    """
    Edit an existing staff account.
    """
    if request.user.role != 'OWNER':
        messages.error(request, "Only Owners can edit staff accounts.")
        return redirect('users:list')

    staff_member = User.objects.get(pk=pk)

    # Ensure profile exists for old users who might not have one
    profile, created = UserProfile.objects.get_or_create(user=staff_member)

    if request.method == 'POST':
        form = StaffUpdateForm(request.POST, instance=staff_member)
        profile_form = UserProfileForm(request.POST, request.FILES, instance=profile)

        if form.is_valid() and profile_form.is_valid():
            form.save()
            profile_form.save()
            messages.success(request, f"Staff account for {staff_member.username} updated.")
            return redirect('users:list')
    else:
        form = StaffUpdateForm(instance=staff_member)
        profile_form = UserProfileForm(instance=profile)

    return render(request, 'users/staff_form.html', {
        'form': form,
        'profile_form': profile_form,
        'title': f'Edit Staff: {staff_member.username}'
    })





