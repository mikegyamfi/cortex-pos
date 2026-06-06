from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q, Sum, Count
from django.core.paginator import Paginator

from .models import Customer
from .forms import CustomerForm


@login_required
def customer_list(request):
    """
    List of all customers with search functionality.
    """
    query = request.GET.get('q', '')
    customers = Customer.objects.all()

    # Multi-tenant: a shop only sees its own customers. Owners see all.
    if request.user.role != 'OWNER':
        customers = customers.filter(location=request.user.assigned_location)

    customers = customers.order_by('-last_visit_date')

    if query:
        customers = customers.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(phone_number__icontains=query) |
            Q(email__icontains=query)
        )

    paginator = Paginator(customers, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'customers/customer_list.html', {
        'page_obj': page_obj,
        'query': query
    })


@login_required
def customer_create(request):
    if request.method == 'POST':
        form = CustomerForm(request.POST)
        # Bind to the creator's shop before validation so the per-shop phone
        # uniqueness check runs against the right shop.
        form.instance.location = request.user.assigned_location
        if form.is_valid():
            customer = form.save()
            messages.success(request, f"Customer {customer.get_display_name} added successfully.")
            return redirect('customers:list')
    else:
        form = CustomerForm()
    return render(request, 'customers/customer_form.html', {'form': form, 'title': 'Add Customer'})


@login_required
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if request.user.role != 'OWNER' and customer.location_id != request.user.assigned_location_id:
        messages.error(request, "That customer belongs to another shop.")
        return redirect('customers:list')
    if request.method == 'POST':
        form = CustomerForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            messages.success(request, f"Customer {customer.get_display_name} updated.")
            return redirect('customers:list')
    else:
        form = CustomerForm(instance=customer)
    return render(request, 'customers/customer_form.html', {'form': form, 'title': 'Edit Customer'})


@login_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if request.user.role != 'OWNER' and customer.location_id != request.user.assigned_location_id:
        messages.error(request, "That customer belongs to another shop.")
        return redirect('customers:list')
    # Get purchase history (reverse relation from Sale model)
    purchases = customer.purchases.all().order_by('-created_at')[:20]

    # Calculate lifetime stats
    stats = customer.purchases.aggregate(
        total_spent=Sum('total_amount'),
        visit_count=Count('id')
    )

    return render(request, 'customers/customer_detail.html', {
        'customer': customer,
        'purchases': purchases,
        'stats': stats
    })


@login_required
def api_search_customers(request):
    """
    API Endpoint for POS to search customers by name or phone.
    Returns JSON list of matching customers.
    """
    query = request.GET.get('q', '')
    if len(query) < 2:
        return JsonResponse({'results': []})

    # Scoped to the cashier's shop — search never returns another shop's customers.
    customers = Customer.objects.filter(
        Q(first_name__icontains=query) |
        Q(last_name__icontains=query) |
        Q(phone_number__icontains=query)
    )
    if request.user.role != 'OWNER':
        customers = customers.filter(location=request.user.assigned_location)
    customers = customers[:10]  # Limit to top 10 results for performance

    results = []
    for c in customers:
        results.append({
            'id': c.id,
            'name': c.get_display_name,
            'phone': c.phone_number,
            'email': c.email
        })

    return JsonResponse({'results': results})


@login_required
def api_create_customer(request):
    """
    Quick API Endpoint for POS to create a customer on the fly.
    """
    if request.method == 'POST':
        import json
        data = json.loads(request.body)
        phone = data.get('phone')
        name = data.get('name', 'Guest')

        # Simple validation
        if not phone:
            return JsonResponse({'success': False, 'message': 'Phone number required'})

        # Customer belongs to the cashier's shop; phone is unique per shop.
        customer, created = Customer.objects.get_or_create(
            phone_number=phone,
            location=request.user.assigned_location,
            defaults={'first_name': name}
        )

        return JsonResponse({
            'success': True,
            'customer': {
                'id': customer.id,
                'name': customer.get_display_name,
                'phone': customer.phone_number
            }
        })
    return JsonResponse({'success': False, 'message': 'Invalid method'})